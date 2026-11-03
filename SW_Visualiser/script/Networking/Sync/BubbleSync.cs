using UnityEngine;
using BridgeRush.Gameplay;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    // Everything the bubble mechanic sends or receives.
    //
    //  1. bubble_captured -> Ultra96. The phone only REPORTS which bubble was captured. The Ultra96 is authoritative and
    //     awards the mana; the phone never adds mana itself (except in the offline mock below).
    //  2. lock progress -> opponent's phone, so the defender can be warned and move.
    //
    // TODO(comms): the Ultra96 / pipeline side must handle `bubble_captured`:
    //   - Subscribe to viz/bubble_captured/+ (topic name is this project's proposal; agree it in comms/notes/mqtt_topics.md).
    //   - Payload: {"event":"bubble_captured","player":<1|2>,"bubble_id":<int>}, inside the usual five-field envelope, QoS 1.
    //   - Validate: the match is running and the player is real. Optionally rate-limit per bubble_id against the respawn cooldown.
    //     QoS 1 can deliver twice; the envelope event_id is de-duplicated on the phone, do the same on the board.
    //   - Award that player's reward (1 mana for now; later a buff) in its game state, then publish the result so the phones show
    //     it. Until the board feeds mana back, the phone's mana only changes through the host phone's own snapshot.
    //   - The phone does not tell the board the reward; the board owns the reward table.
    public class BubbleSync : MonoBehaviour
    {
        // Proposed contract topic, same shape as viz/ready/{player} (phone -> pipeline).
        private const string CapturedTopicPrefix = "viz/bubble_captured/";
        // Phone-to-phone, like bridgerush/lock/playerN. Not in the comms contract yet (stub).
        private const string LockTopicPlayer1 = "bridgerush/bubble/player1";
        private const string LockTopicPlayer2 = "bridgerush/bubble/player2";

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private BubbleCaptureManager captureManager;
        [SerializeField] private MatchState matchState;
        [SerializeField] private MatchSetup matchSetup;

        [Header("Streaming")]
        [Tooltip("Most lock-progress messages per second while progress changes.")]
        [SerializeField] private float progressPublishHz = 5f;
        [Tooltip("The warning clears if no lock message arrives for this long (opponent disconnected or stopped).")]
        [SerializeField] private float incomingStaleSeconds = 1.5f;

        [Header("Testing")]
        [Tooltip("Offline mode, no Ultra96 or second phone: a capture awards its reward locally and your own lock progress is looped back as the incoming warning. Leave OFF in a real match.")]
        [SerializeField] private bool mockUltra96Offline = false;

        [System.Serializable]
        private class LockMessage
        {
            public bool targeted;
            public float progress;
        }

        public bool MockOffline => mockUltra96Offline;
        public void SetMockOffline(bool on) => mockUltra96Offline = on;

        // Someone is locking onto this player's tower. Read by BubbleWarningUI.
        public bool IncomingTargeted { get; private set; }
        public float IncomingProgress { get; private set; }

        private string myLockTopic;
        private string opponentLockTopic;
        private bool subscribed;
        private float lastPublishTime;
        private bool lastPublishedTargeting;
        private float lastPublishedProgress = -1f;
        private float lastIncomingTime;

        private void Awake()
        {
            if (captureManager == null || matchState == null || matchSetup == null)
            {
                Debug.LogError($"{nameof(BubbleSync)}: needs a Bubble Capture Manager, a Match State and a Match Setup.", this);
                enabled = false;
            }
        }

        private void OnEnable()
        {
            if (captureManager != null) captureManager.OnBubbleCaptured += HandleCaptured;
        }

        private void OnDisable()
        {
            if (captureManager != null) captureManager.OnBubbleCaptured -= HandleCaptured;
        }

        private int LocalPlayer => matchSetup.Selected == MatchSetup.SelectedPlayer.Player2 ? 2 : 1;

        private void Update()
        {
            if (mockUltra96Offline)
            {
                // Loopback: show the warning for our own lock so the UI can be tested on one phone.
                IncomingTargeted = captureManager.IsTargeting;
                IncomingProgress = captureManager.LockProgress;
                return;
            }

            if (!subscribed) TrySubscribe();

            if (IncomingTargeted && Time.time - lastIncomingTime > incomingStaleSeconds)
            {
                IncomingTargeted = false;
                IncomingProgress = 0f;
            }

            PublishProgress();
        }

        private void TrySubscribe()
        {
            if (mqttConnection == null || matchSetup.Selected == MatchSetup.SelectedPlayer.None) return;

            bool isPlayer1 = matchSetup.Selected == MatchSetup.SelectedPlayer.Player1;
            myLockTopic = isPlayer1 ? LockTopicPlayer1 : LockTopicPlayer2;
            opponentLockTopic = isPlayer1 ? LockTopicPlayer2 : LockTopicPlayer1;
            mqttConnection.Subscribe(opponentLockTopic, OnOpponentLockMessage, 0);
            subscribed = true;
        }

        // Stub for the outgoing stream: {"targeted":bool,"progress":0..1} on bridgerush/bubble/{me}. QoS 0, the next message corrects a lost one.
        private void PublishProgress()
        {
            if (mqttConnection == null || !mqttConnection.IsConnected || myLockTopic == null) return;

            bool targeting = captureManager.IsTargeting;
            float progress = Mathf.Round(captureManager.LockProgress * 100f) / 100f;
            bool flagChanged = targeting != lastPublishedTargeting;
            bool progressChanged = !Mathf.Approximately(progress, lastPublishedProgress);
            if (!flagChanged && !progressChanged) return;
            if (!flagChanged && Time.time - lastPublishTime < 1f / Mathf.Max(0.1f, progressPublishHz)) return;

            lastPublishTime = Time.time;
            lastPublishedTargeting = targeting;
            lastPublishedProgress = progress;
            string p = progress.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture);
            mqttConnection.Publish(myLockTopic, $"{{\"targeted\":{(targeting ? "true" : "false")},\"progress\":{p}}}", false, 0);
        }

        private void OnOpponentLockMessage(string topic, string payload)
        {
            var message = JsonUtility.FromJson<LockMessage>(payload);
            if (message == null) return;
            IncomingTargeted = message.targeted || message.progress > 0f;
            IncomingProgress = message.progress;
            lastIncomingTime = Time.time;
        }

        private void HandleCaptured(int bubbleId)
        {
            // VS CPU is a local game with no Ultra96 behind it, so the reward is applied here. A two-player match leaves it to the board.
            bool localGame = mockUltra96Offline || (matchSetup.HasChosenMode && matchSetup.Mode == MatchSetup.GameMode.VsCpu);
            if (localGame)
            {
                ApplyRewardLocally(captureManager.Reward);
                return;
            }

            if (mqttConnection == null) return;
            int player = LocalPlayer;
            mqttConnection.Publish(CapturedTopicPrefix + player,
                $"{{\"event\":\"bubble_captured\",\"player\":{player},\"bubble_id\":{bubbleId}}}");
        }

        // Mock only. The reward handling lives here so a new BubbleRewardType is one new case.
        private void ApplyRewardLocally(BubbleReward reward)
        {
            switch (reward.Type)
            {
                case BubbleRewardType.Mana:
                    matchState.MockAwardMana(reward.Amount);
                    break;
                default:
                    Debug.Log($"BubbleSync (mock): reward {reward} has no local effect yet.");
                    break;
            }
        }
    }
}
