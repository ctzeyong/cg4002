using Comms.Visualiser;
using Newtonsoft.Json.Linq;
using UnityEngine;
using BridgeRush.Gameplay;
using BridgeRush.Input;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    /// <summary>
    /// Receives this player's glove gestures from the comms pipeline and plays them as game
    /// actions. The pipeline publishes the classifier's AI event on game/gesture/{player}
    /// (comms/notes/mqtt_topics.md); the payload carries a "gesture" name such as "circle".
    ///
    /// Each phone listens only to its own player's topic and applies the gesture to itself,
    /// through the same path as the touch pad. On the Player 2 (client) phone that means the
    /// existing request-to-host route still carries the aim, so nothing downstream changes.
    ///
    /// Builds itself at startup, like the comms visualiser, so the scene needs no wiring.
    /// </summary>
    public class GloveGestureReceiver : MonoBehaviour
    {
        private MqttConnection mqtt;
        private MatchSetup setup;
        private MatchState match;
        private SpellCaster spells;

        private int subscribedPlayer;

        /// <summary>Last glove gesture name received, for on-screen debugging.</summary>
        public string LastGesture { get; private set; } = "";
        public int Received { get; private set; }

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Bootstrap()
        {
            if (FindAnyObjectByType<GloveGestureReceiver>() != null) return;
            new GameObject(nameof(GloveGestureReceiver)).AddComponent<GloveGestureReceiver>();
        }

        private void Update()
        {
            if (mqtt == null) mqtt = FindAnyObjectByType<MqttConnection>();
            if (setup == null) setup = FindAnyObjectByType<MatchSetup>();
            if (match == null) match = FindAnyObjectByType<MatchState>();
            if (spells == null) spells = FindAnyObjectByType<SpellCaster>();
            if (mqtt == null || setup == null || match == null) return;

            // Re-subscribe if the player choice changes (debug reset, or picking a side late).
            int player = LocalPlayer();
            if (player == subscribedPlayer) return;

            if (subscribedPlayer != 0) mqtt.Unsubscribe(MqttConfig.GestureTopic(subscribedPlayer));
            if (player != 0) mqtt.Subscribe(MqttConfig.GestureTopic(player), OnGesture, MqttConfig.QosGameGesture);
            subscribedPlayer = player;
        }

        // 1 or 2 once known; 0 until the player has chosen. Vs CPU has one human, player 1.
        private int LocalPlayer()
        {
            switch (setup.Selected)
            {
                case MatchSetup.SelectedPlayer.Player1: return 1;
                case MatchSetup.SelectedPlayer.Player2: return 2;
                default:
                    return setup.HasChosenMode && setup.Mode == MatchSetup.GameMode.VsCpu ? 1 : 0;
            }
        }

        private void OnGesture(string topic, string payload)
        {
            string name;
            try
            {
                name = JObject.Parse(payload).Value<string>("gesture");
            }
            catch (System.Exception e)
            {
                Debug.LogWarning($"GloveGestureReceiver: unreadable payload on {topic}: {e.Message}");
                return;
            }

            Received++;
            LastGesture = name ?? "";

            // The board engine joins this gesture with the aim it streams and performs it itself (game/event reports back).
            if (match.Role == MatchState.NetworkRole.BoardClient) return;

            var action = GestureBindings.FromGlove(name);
            if (action == GameAction.None)
            {
                Debug.LogWarning($"GloveGestureReceiver: no action bound to glove gesture '{name}'");
                return;
            }

            bool done = GestureBindings.Perform(action, match, spells);
            Debug.Log($"GloveGestureReceiver: '{name}' -> {action} " +
                      (done ? "performed" : "rejected (no mana, no valid aim, or match not started)"));
        }

        private void OnDestroy()
        {
            if (mqtt != null && subscribedPlayer != 0) mqtt.Unsubscribe(MqttConfig.GestureTopic(subscribedPlayer));
        }
    }
}
