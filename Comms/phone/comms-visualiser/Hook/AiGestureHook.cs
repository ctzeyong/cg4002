using System;
using System.Collections.Generic;
using BridgeRush.Input;
using BridgeRush.Networking;
using BridgeRush.Tracking;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// Makes an AI event on game/gesture/{player} act like a stroke drawn on this phone:
    /// subscribes to this phone's topic, reads each event with GestureEventParser, maps the
    /// name onto the game's GestureSymbol and calls GesturePad.PerformGesture.
    ///
    /// Works the same under configurations A and B: it needs only a connected MqttConnection,
    /// however that connection reaches the broker. notes/integration/vis_integration.md 4.4.
    /// </summary>
    public class AiGestureHook : MonoBehaviour
    {
        private const float LineSeconds = 5f;
        private const float ReferenceWidth = 800f;

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private GesturePad gesturePad;

        [Tooltip("Where the hook's one-line messages sit, in reference pixels from the top. " +
                 "The default clears the tunnel's collapsed status line.")]
        [SerializeField] private float lineTop = 64f;

        private readonly GestureEventParser _parser = new GestureEventParser();

        // Topics already handed to MqttConnection. It has no unsubscribe, so a topic stays
        // subscribed for the life of the app, and the parser drops any but the current one.
        private readonly HashSet<string> _subscribed = new HashSet<string>();

        // The topic the last Update acted on, so each change of player is shown once.
        private string _actingOn;

        private string _line;
        private float _lineUntil;

        public int Acted { get; private set; }
        public int Dropped { get; private set; }

        private void Awake()
        {
            if (mqttConnection == null || matchSetup == null || gesturePad == null)
            {
                Debug.LogError($"{nameof(AiGestureHook)}: needs an Mqtt Connection, a Match Setup and a Gesture Pad.", this);
                enabled = false;
            }
        }

        private void Update()
        {
            // His dispatch matches topics exactly, so subscribe to this player's own topic,
            // never game/gesture/+ (which would receive every event and dispatch none).
            var topic = CurrentTopic();
            if (topic == _actingOn) return;
            _actingOn = topic;

            // Switching back to a topic already subscribed sends nothing, so say it here, or
            // the switch looks like it failed.
            if (topic == null)
            {
                Show("no player selected: events are ignored");
                return;
            }

            if (!_subscribed.Add(topic))
            {
                Show($"now acting on {topic}");
                return;
            }

            try
            {
                mqttConnection.Subscribe(topic, OnMessage);
                Show($"listening on {topic}");
            }
            catch (Exception exc)
            {
                // Subscribe records the topic before it touches the client, so the next
                // Connect() subscribes it anyway.
                Show($"subscribe to {topic} failed, retried on reconnect: {exc.Message}");
            }
        }

        /// <summary>game/gesture/1 or /2 for the player this phone selected, or null before a choice.</summary>
        private string CurrentTopic()
        {
            if (!matchSetup.HasSelected) return null;

            switch (matchSetup.Selected)
            {
                case MatchSetup.SelectedPlayer.Player1: return MqttConfig.GestureTopic(1);
                case MatchSetup.SelectedPlayer.Player2: return MqttConfig.GestureTopic(2);
                default: return null;
            }
        }

        /// <summary>
        /// Called from MqttConnection.Update, inside its dequeue loop. Never throws: an
        /// exception would leave that loop and hold every other topic's messages a frame.
        /// </summary>
        private void OnMessage(string topic, string json)
        {
            try
            {
                Handle(topic, json);
            }
            catch (Exception exc)
            {
                Dropped++;
                Show($"hook error on {topic}: {exc.GetType().Name}: {exc.Message}");
            }
        }

        private void Handle(string topic, string json)
        {
            var expected = CurrentTopic();
            if (expected == null)
            {
                Dropped++;
                Show($"ignored {topic}: no player selected");
                return;
            }

            if (!_parser.TryRead(topic, json, expected, out var name, out var reason))
            {
                Dropped++;
                Show(reason);
                return;
            }

            if (reason != null) Show(reason);

            if (!Enum.TryParse(name, true, out GestureSymbol symbol) ||
                !Enum.IsDefined(typeof(GestureSymbol), symbol))
            {
                Dropped++;
                Show($"unknown gesture '{name}'");
                return;
            }

            // Accepted or rejected, the result shows in GesturePad's own feedback label.
            gesturePad.PerformGesture(symbol, "glove");
            Acted++;
        }

        private void Show(string message)
        {
            _line = "AI: " + message;
            _lineUntil = Time.realtimeSinceStartup + LineSeconds;
            Debug.Log($"[ai-hook] {message}");
        }

        private void OnGUI()
        {
            if (_line == null || Time.realtimeSinceStartup > _lineUntil) return;

            var scale = Screen.width / ReferenceWidth;
            var previous = GUI.matrix;
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));
            GUI.Box(new Rect(10f, lineTop, ReferenceWidth - 20f, 30f), _line);
            GUI.matrix = previous;
        }
    }
}
