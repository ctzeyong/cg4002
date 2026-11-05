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
        [Tooltip("Wire EngineLink (and MatchState's Engine Link) to play VS PLAYER on the Ultra96 engine instead of the host phone.")]
        [SerializeField] private EngineLink engineLink;
        [SerializeField] private bool useBoardEngine;

        private const string BoardEnginePref = "use_board_engine";

        // Takes effect when the next match's role is assigned (after the mode and player are chosen). Saved on the phone.
        public bool UseBoardEngine
        {
            get => useBoardEngine && engineLink != null && matchState.BoardEngineAvailable;
            set
            {
                useBoardEngine = value;
                PlayerPrefs.SetInt(BoardEnginePref, value ? 1 : 0);
            }
        }
        public bool BoardEngineWired => engineLink != null && matchState != null && matchState.BoardEngineAvailable;

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
            if (PlayerPrefs.HasKey(BoardEnginePref)) useBoardEngine = PlayerPrefs.GetInt(BoardEnginePref) == 1;
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

            bool isPlayer1 = matchSetup.Selected == MatchSetup.SelectedPlayer.Player1;
            if (UseBoardEngine)
            {
                matchState.SetNetworkRole(MatchState.NetworkRole.BoardClient);
                engineLink.Activate();
            }
            else
            {
                matchState.SetNetworkRole(isPlayer1 ? MatchState.NetworkRole.Host : MatchState.NetworkRole.Client);
            }
            roleAssigned = true;
        }

        public void DebugReset()
        {
            roleAssigned = false;
            subscribed = false;
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
