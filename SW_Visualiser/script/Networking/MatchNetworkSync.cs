using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    public class MatchNetworkSync : MonoBehaviour
    {
        private const string StateTopic = "bridgerush/game/state";
        private const string GestureTopic = "bridgerush/game/gesture";

        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchState matchState;
        [SerializeField] private MqttConnection mqttConnection;

        private bool roleAssigned;
        private bool subscribed;

        private void Awake()
        {
            if (matchSetup == null || matchState == null || mqttConnection == null)
            {
                Debug.LogError($"{nameof(MatchNetworkSync)}: needs a Match Setup, a Match State and an Mqtt Connection.", this);
                enabled = false;
            }
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
            matchState.SetNetworkRole(isPlayer1 ? MatchState.NetworkRole.Host : MatchState.NetworkRole.Client);
            roleAssigned = true;
        }

        public void DebugReset()
        {
            roleAssigned = false;
            subscribed = false;
            matchState.SetNetworkRole(MatchState.NetworkRole.Local);
        }

        private void TrySubscribe()
        {
            switch (matchState.Role)
            {
                case MatchState.NetworkRole.Host:
                    mqttConnection.Subscribe(GestureTopic, OnGestureMessage);
                    subscribed = true;
                    break;
                case MatchState.NetworkRole.Client:
                    mqttConnection.Subscribe(StateTopic, OnStateMessage);
                    subscribed = true;
                    break;
            }
        }

        private void OnStateTicked()
        {
            if (matchState.Role != MatchState.NetworkRole.Host) return;
            if (!mqttConnection.IsConnected) return;

            var snapshot = matchState.BuildStateSnapshot();
            mqttConnection.Publish(StateTopic, JsonUtility.ToJson(snapshot));
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
            mqttConnection.Publish(GestureTopic, JsonUtility.ToJson(request));
        }
    }
}
