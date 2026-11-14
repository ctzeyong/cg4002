using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    public class MatchNetworkSync : MonoBehaviour
    {
        // game/state is contracted in comms/notes/mqtt_topics.md (whole snapshot every tick, QoS 0, not retained).
        private const string StateTopic = "game/state";
        // Phone-to-phone only: the client asks the host to spawn/cast. Not in the comms contract yet.
        // Deliberately NOT game/gesture/{player}, which is the pipeline's glove event going to the phones.
        private const string RequestTopic = "bridgerush/game/request";

        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchState matchState;
        [SerializeField] private MqttConnection mqttConnection;
        [Tooltip("VS PLAYER always runs on the Ultra96 engine. Wire EngineLink here and in MatchState's Engine Link (left empty, it is found by type). Without one VS PLAYER does not start: a phone never hosts the game.")]
        [SerializeField] private EngineLink engineLink;

        // VS PLAYER always uses the Ultra96 engine. There is no switch and no phone-hosted fallback.
        private bool UseBoardEngine => engineLink != null && matchState.BoardEngineAvailable;

        // VS PLAYER was chosen and this build has no EngineLink, so no role was assigned and the match cannot start.
        public bool BoardMissing { get; private set; }

        private bool roleAssigned;
        private bool subscribed;

        private void Awake()
        {
            if (matchSetup == null || matchState == null || mqttConnection == null)
            {
                Debug.LogError($"{nameof(MatchNetworkSync)}: needs a Match Setup, a Match State and an Mqtt Connection.", this);
                enabled = false;
                return;
            }
            if (engineLink == null) engineLink = FindAnyObjectByType<EngineLink>();
        }

        private void OnEnable()
        {
            if (matchState != null) matchState.StateTicked += OnStateTicked;
        }

        private void OnDisable()
        {
            if (matchState != null) matchState.StateTicked -= OnStateTicked;
        }

        private void Update()
        {
            if (!roleAssigned) TryAssignRole();
            if (!subscribed) TrySubscribe();
        }

        private void TryAssignRole()
        {
            if (!matchSetup.HasChosenMode || matchSetup.Mode != MatchSetup.GameMode.VsPlayer) return;
            if (!matchSetup.HasSelected) return;

            // Never Host or Client: the phone does not run the game. Without the board's engine, VS PLAYER just waits.
            if (!UseBoardEngine)
            {
                if (!BoardMissing) Debug.LogError($"{nameof(MatchNetworkSync)}: VS PLAYER needs the board engine, but there is no EngineLink in the scene. Add one (integration_update.md section 6). The phone will not host the game.", this);
                BoardMissing = true;
                return;
            }

            BoardMissing = false;
            matchState.SetNetworkRole(MatchState.NetworkRole.BoardClient);
            engineLink.Activate();
            roleAssigned = true;
        }

        public void DebugReset()
        {
            roleAssigned = false;
            subscribed = false;
            BoardMissing = false;
            if (engineLink != null) engineLink.Deactivate();
            matchState.SetNetworkRole(MatchState.NetworkRole.Local);
        }

        private void TrySubscribe()
        {
            switch (matchState.Role)
            {
                case MatchState.NetworkRole.Host:
                    mqttConnection.Subscribe(RequestTopic, OnGestureMessage);
                    subscribed = true;
                    break;
                case MatchState.NetworkRole.Client:
                    mqttConnection.Subscribe(StateTopic, OnStateMessage, 0);
                    subscribed = true;
                    break;
                case MatchState.NetworkRole.BoardClient:
                    subscribed = true; // EngineLink has game/state, game/match and game/event
                    break;
            }
        }

        private void OnStateTicked()
        {
            if (matchState.Role != MatchState.NetworkRole.Host) return;
            if (!mqttConnection.IsConnected) return;

            var snapshot = matchState.BuildStateSnapshot();
            mqttConnection.Publish(StateTopic, JsonUtility.ToJson(snapshot), false, 0);
        }

        private void OnStateMessage(string topic, string payload)
        {
            var snapshot = JsonUtility.FromJson<StateSnapshot>(payload);
            matchState.ApplyRemoteState(snapshot);
        }

        private void OnGestureMessage(string topic, string payload)
        {
            var request = JsonUtility.FromJson<GestureRequest>(payload);
            matchState.ApplyRemoteGestureRequest(request);
        }

        public void PublishGestureRequest(string action, string unitType, string spell, bool hasAim = false, float along = 0f, float lat = 0f)
        {
            if (!mqttConnection.IsConnected) return;
            var request = new GestureRequest { action = action, unitType = unitType, spell = spell, hasAim = hasAim, along = along, lat = lat };
            mqttConnection.Publish(RequestTopic, JsonUtility.ToJson(request));
        }
    }
}
