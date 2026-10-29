using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    public class TowerLockSync : MonoBehaviour
    {
        private const string LockTopicPlayer1 = "bridgerush/lock/player1";
        private const string LockTopicPlayer2 = "bridgerush/lock/player2";

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private MatchState matchState;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchSetup matchSetup;

        private string myTopic;
        private string opponentTopic;
        private bool subscribed;
        private bool lastPublishedLockState;
        private bool wasConnected;

        private void Update()
        {
            if (!subscribed) TrySubscribe();

            bool locked = matchState.OwnTowerLocked;
            bool connected = mqttConnection.IsConnected;
            bool justConnected = connected && !wasConnected;
            wasConnected = connected;

            if (locked == lastPublishedLockState && !justConnected) return;

            lastPublishedLockState = locked;
            if (connected && myTopic != null)
            {
                mqttConnection.Publish(myTopic, locked ? "{\"locked\":true}" : "{\"locked\":false}");
            }
        }

        private void TrySubscribe()
        {
            if (matchSetup.Selected == MatchSetup.SelectedPlayer.None) return;

            bool isPlayer1 = matchSetup.Selected == MatchSetup.SelectedPlayer.Player1;
            myTopic = isPlayer1 ? LockTopicPlayer1 : LockTopicPlayer2;
            opponentTopic = isPlayer1 ? LockTopicPlayer2 : LockTopicPlayer1;

            mqttConnection.Subscribe(opponentTopic, OnOpponentLockMessage);
            subscribed = true;
        }

        private void OnOpponentLockMessage(string topic, string payload)
        {
            bool locked = payload.Contains("true");
            arenaFrame.SetOpponentLocked(locked);
        }
    }
}
