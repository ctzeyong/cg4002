using TMPro;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.UI;

namespace BridgeRush.Networking
{
    public class MatchCountdown : MonoBehaviour
    {
        private const string CountdownTopic = "bridgerush/match/countdown";

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private MatchState matchState;
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private Canvas canvas;
        [SerializeField] private float countdownSeconds = 3f;
        [SerializeField] private float fightHoldSeconds = 0.6f;

        private GameObject countdownRoot;
        private TextMeshProUGUI countdownText;
        private bool countingDown;
        private float countdownRemaining;

        public bool HasRequestedStart { get; private set; }

        public void ResetCountdownGate()
        {
            HasRequestedStart = false;
            countingDown = false;
            if (countdownRoot != null) countdownRoot.SetActive(false);
        }

        private void Awake()
        {
            BuildCountdownUI();
        }

        private void Start()
        {
            if (mqttConnection == null) return;
            mqttConnection.Subscribe(CountdownTopic, OnCountdownMessage);
        }

        public void RequestStart()
        {
            if (HasRequestedStart) return;

            // The board engine owns the start: the press goes to it, and it is re-sent until it counts (EngineLink).
            if (matchState.IsBoardClient)
            {
                HasRequestedStart = matchState.BoardPressStart();
                if (!HasRequestedStart) Debug.LogWarning("MatchCountdown: no game/match from the engine yet, START not sent.");
                return;
            }

            // VS PLAYER is the board's game only. A phone must never start or host one on its own.
            if (matchSetup.Mode == MatchSetup.GameMode.VsPlayer)
            {
                Debug.LogWarning("MatchCountdown: VS PLAYER needs the board engine; the phone will not start the match itself.");
                return;
            }
            HasRequestedStart = true;

            bool isVsCpu = matchSetup.Mode == MatchSetup.GameMode.VsCpu;
            if (isVsCpu)
            {
                BeginCountdown();
            }
            else if (mqttConnection != null && mqttConnection.IsConnected)
            {
                mqttConnection.Publish(CountdownTopic, "{}");
            }
            else
            {
                Debug.LogWarning("MatchCountdown: MQTT not connected, starting countdown locally only.");
                BeginCountdown();
            }
        }

        private void OnCountdownMessage(string topic, string payload)
        {
            if (matchState.IsBoardClient) return; // retired topic: the board's game/match carries the countdown
            BeginCountdown();
        }

        private void BeginCountdown()
        {
            if (countingDown) return;
            countingDown = true;
            countdownRemaining = countdownSeconds;
        }

        // The 3-2-1 is drawn from the board's countdown_ms and starts nothing: MatchState starts the match when `running` arrives.
        private void UpdateBoardCountdown()
        {
            if (countdownRoot == null) return;
            int ms = matchState.BoardCountdownMs;
            if (ms < 0)
            {
                if (countdownRoot.activeSelf) countdownRoot.SetActive(false);
                return;
            }
            countdownRoot.SetActive(true);
            countdownText.text = ms > 0 ? Mathf.CeilToInt(ms / 1000f).ToString() : "FIGHT!";
        }

        private void Update()
        {
            if (matchState.IsBoardClient)
            {
                UpdateBoardCountdown();
                return;
            }
            if (!countingDown) return;

            countdownRemaining -= Time.deltaTime;

            if (countdownRoot != null)
            {
                countdownRoot.SetActive(true);
                if (countdownRemaining > 0f)
                {
                    countdownText.text = Mathf.CeilToInt(countdownRemaining).ToString();
                }
                else if (countdownRemaining > -fightHoldSeconds)
                {
                    countdownText.text = "FIGHT!";
                }
            }

            if (countdownRemaining > -fightHoldSeconds) return;

            countingDown = false;
            if (countdownRoot != null) countdownRoot.SetActive(false);
            matchState.StartMatch();
        }

        private void BuildCountdownUI()
        {
            if (canvas == null) return;

            countdownRoot = UIFactory.CreateRect("MatchCountdown", canvas.transform);
            var rect = countdownRoot.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 0.5f);
            rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.sizeDelta = new Vector2(300f, 200f);

            countdownText = UIFactory.CreateText(countdownRoot.transform, string.Empty, 96, FontStyles.Bold, HudPalette.Gold);
            UIFactory.StretchFull(countdownText.rectTransform);
            countdownRoot.SetActive(false);
        }
    }
}
