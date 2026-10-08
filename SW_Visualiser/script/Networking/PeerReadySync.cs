using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    public class PeerReadySync : MonoBehaviour
    {
        private const string ReadyTopicPlayer1 = "bridgerush/ready/player1";
        private const string ReadyTopicPlayer2 = "bridgerush/ready/player2";

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private MatchState matchState;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchSetup matchSetup;

        private string myTopic;
        private string opponentTopic;
        private bool subscribed;
        private bool wasConnected;

        // VS CPU has no real peer to wait for, so lock-in doesn't apply there.
        public bool RequiresLockIn => matchState.Role != MatchState.NetworkRole.Local;
        public bool CanLockIn => RequiresLockIn && !HasLockedIn && arenaFrame.HasLane;
        public bool HasLockedIn { get; private set; }

        public bool PeerReady { get; private set; } = true;

        // Only callable once the opponent's marker has actually been seen - see CanLockIn.
        public void LockIn()
        {
            if (!CanLockIn) return;

            HasLockedIn = true;
            PublishReady();
        }

        public void DebugReset()
        {
            HasLockedIn = false;
            PeerReady = true;
            subscribed = false;
            myTopic = null;
            opponentTopic = null;
        }

        private void Update()
        {
            if (matchState.Role == MatchState.NetworkRole.Local)
            {
                PeerReady = true;
                return;
            }

            if (!subscribed) TrySubscribe();

            bool connected = mqttConnection.IsConnected;
            // Republish on reconnect - a lock-in sent before MQTT finished connecting would otherwise never reach the peer.
            if (connected && !wasConnected && HasLockedIn) PublishReady();
            wasConnected = connected;
        }

        private void PublishReady()
        {
            if (mqttConnection.IsConnected && myTopic != null)
            {
                mqttConnection.Publish(myTopic, "{\"ready\":true}");
            }
        }

        private void TrySubscribe()
        {
            if (matchSetup.Selected == MatchSetup.SelectedPlayer.None) return;

            bool isPlayer1 = matchSetup.Selected == MatchSetup.SelectedPlayer.Player1;
            myTopic = isPlayer1 ? ReadyTopicPlayer1 : ReadyTopicPlayer2;
            opponentTopic = isPlayer1 ? ReadyTopicPlayer2 : ReadyTopicPlayer1;

            PeerReady = false;
            mqttConnection.Subscribe(opponentTopic, OnOpponentReadyMessage);
            subscribed = true;
        }

        private void OnOpponentReadyMessage(string topic, string payload)
        {
            PeerReady = payload.Contains("true");
        }
    }
}
