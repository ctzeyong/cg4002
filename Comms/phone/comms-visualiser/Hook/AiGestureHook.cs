using System;
using System.Collections.Generic;
using BridgeRush.Networking;
using BridgeRush.Tracking;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// Shows each change in the gloves' status from status/gloves and echoes every gesture on
    /// viz/echo/{player} for latency. It doesn't act on gestures, since the app's
    /// GloveGestureReceiver owns game/gesture/{player}.
    /// </summary>
    public class AiGestureHook : MonoBehaviour
    {
        private const float LineSeconds = 5f;
        private const float ReferenceWidth = 800f;

        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private MatchSetup matchSetup;

        [Tooltip("Where the hook's one-line messages sit, in reference pixels from the top. " +
                 "The default clears the tunnel's collapsed status line.")]
        [SerializeField] private float lineTop = 64f;

        // Each glove's last shown state, so only a change is shown.
        private string[] _gloves;

        private string _line;
        private float _lineUntil;

        private void Awake()
        {
            if (mqttConnection == null || matchSetup == null)
            {
                Debug.LogError($"{nameof(AiGestureHook)}: needs an Mqtt Connection and a Match Setup.", this);
                enabled = false;
            }
        }

        private void Start()
        {
            // One topic for both gloves, whichever player this phone is. It is retained, so
            // the current state arrives as soon as the phone subscribes.
            try
            {
                mqttConnection.Subscribe(GloveStatusParser.Topic, OnGloveStatus);
            }
            catch (Exception exc)
            {
                Show($"subscribe to {GloveStatusParser.Topic} failed, retried on reconnect: {exc.Message}");
            }

            try
            {
                mqttConnection.Subscribe(GestureEcho.GestureFilter, OnGestureForEcho);
            }
            catch (Exception exc)
            {
                Show($"subscribe to {GestureEcho.GestureFilter} failed, retried on reconnect: {exc.Message}");
            }
        }

        /// <summary>1 or 2 for the player this phone selected, or 0 before a choice.</summary>
        private int CurrentPlayer()
        {
            if (!matchSetup.HasSelected) return 0;

            switch (matchSetup.Selected)
            {
                case MatchSetup.SelectedPlayer.Player1: return 1;
                case MatchSetup.SelectedPlayer.Player2: return 2;
                default: return 0;
            }
        }

        /// <summary>Echo any player's gesture, at QoS 0 since a lost echo is only one lost sample. Never throws.</summary>
        private void OnGestureForEcho(string topic, string payload)
        {
            try
            {
                var player = CurrentPlayer();
                if (player == 0) return;

                if (GestureEcho.TryBuild(payload, out var echo, out var reason))
                    mqttConnection.Publish(GestureEcho.Topic(player), echo, qos: 0);
                else
                    Show(reason, "echo");
            }
            catch (Exception exc)
            {
                Show($"hook error on {topic}: {exc.GetType().Name}: {exc.Message}", "echo");
            }
        }

        /// <summary>
        /// Shows the gloves whose state changed, on one line. Never throws, like OnGestureForEcho.
        /// Gets the envelope's inner payload, since MqttConnection removes the envelope first.
        /// </summary>
        private void OnGloveStatus(string topic, string payload)
        {
            try
            {
                if (!GloveStatusParser.TryRead(payload, out var gloves, out var reason))
                {
                    Show(reason, "gloves");
                    return;
                }

                var changed = new List<string>();
                for (var i = 0; i < gloves.Length; i++)
                    if (_gloves == null || _gloves[i] != gloves[i])
                        changed.Add($"player {i + 1} {gloves[i]}");
                _gloves = gloves;

                if (changed.Count > 0) Show(string.Join(", ", changed), "gloves");
            }
            catch (Exception exc)
            {
                Show($"hook error on {topic}: {exc.GetType().Name}: {exc.Message}", "gloves");
            }
        }

        private void Show(string message, string label = "AI")
        {
            _line = $"{label}: {message}";
            _lineUntil = Time.realtimeSinceStartup + LineSeconds;
            Debug.Log($"[ai-hook] {_line}");
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
