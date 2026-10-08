using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Networking;

namespace BridgeRush.UI
{
    public class DebugMenu : MonoBehaviour
    {
        private const string HeartbeatTopic = "debug/heartbeat";
        private const int MaxLogLines = 4;

        [SerializeField] private MatchState matchState;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchCountdown matchCountdown;
        [SerializeField] private MatchNetworkSync matchNetworkSync;
        [SerializeField] private PeerReadySync peerReadySync;
        [SerializeField] private TrackingDebugOverlay trackingDebugOverlay;
        [SerializeField] private Canvas canvas;

        [Header("MQTT")]
        [Tooltip("The one shared connection. Broker address is configured on that component.")]
        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private float mqttHeartbeatIntervalSeconds = 1f;

        private float timeSinceLastHeartbeat;
        private int sentSeq;
        private readonly List<string> receivedLog = new();
        private bool mqttExpanded;
        private bool menuExpanded = true;

        private void Awake()
        {
            if (matchState == null)
            {
                Debug.LogError($"{nameof(DebugMenu)}: needs a Match State.", this);
                enabled = false;
            }
        }

        private void Update()
        {
            if (mqttConnection == null || !mqttConnection.IsConnected) return;

            timeSinceLastHeartbeat += Time.deltaTime;
            if (timeSinceLastHeartbeat < mqttHeartbeatIntervalSeconds) return;

            timeSinceLastHeartbeat = 0f;
            sentSeq++;
            string json = $"{{\"seq\":{sentSeq},\"from\":\"{SystemInfo.deviceName}\"}}";
            mqttConnection.Publish(HeartbeatTopic, json);
        }

        private void OnMqttMessage(string topic, string payload)
        {
            receivedLog.Add($"{topic}: {payload}");
            if (receivedLog.Count > MaxLogLines) receivedLog.RemoveAt(0);
        }

        private const float RowHeight = 40f;
        private const float RowGap = 6f;
        private const float TitleHeight = 44f;
        private const float PanelWidth = 280f;

        private void OnGUI()
        {
            float s = canvas != null ? canvas.scaleFactor : 1f;

            DrawPausedTag(s);

            float width = PanelWidth * s;
            float top = 340f * s;
            float rowStep = (RowHeight + RowGap) * s;

            int bodyRows = 5 + (trackingDebugOverlay != null ? 1 : 0);
            float bodyHeight = menuExpanded ? rowStep * (bodyRows + 1) : 0f;
            float mqttSectionHeight = menuExpanded && mqttExpanded ? (rowStep * 2 + (22f + 4 * 20f) * s) : 0f;
            float height = TitleHeight * s + bodyHeight + mqttSectionHeight + 10f * s;

            var panelStyle = new GUIStyle(GUI.skin.box)
            {
                normal = { background = Texture2D.grayTexture },
                alignment = TextAnchor.UpperLeft,
                padding = new RectOffset(10, 10, 8, 10)
            };
            GUI.Box(new Rect(Screen.width - width - 10f * s, top, width, height), string.Empty, panelStyle);

            var titleStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(17f * s),
                fontStyle = FontStyle.Bold,
                normal = { textColor = new Color(1f, 0.55f, 0.25f) }
            };
            string titleArrow = menuExpanded ? "▼" : "▶";
            if (GUI.Button(new Rect(Screen.width - width, top + 4f * s, width - 20f * s, TitleHeight * s - 8f * s), $"{titleArrow} DEBUG", titleStyle))
            {
                menuExpanded = !menuExpanded;
            }

            if (!menuExpanded) return;

            float y = top + TitleHeight * s;

            var restartStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = new Color(1f, 0.7f, 0.6f) }
            };
            if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), "Restart Game", restartStyle))
            {
                RestartGame();
            }
            y += rowStep;

            var pauseStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = matchState.Paused ? Color.yellow : Color.white }
            };
            string pauseLabel = matchState.Paused ? "Resume" : "Pause";
            if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), pauseLabel, pauseStyle))
            {
                matchState.TogglePause();
            }
            y += rowStep;

            var forceStartStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = new Color(0.6f, 0.85f, 1f) }
            };
            if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), "Force Start (skip tracking)", forceStartStyle))
            {
                matchState.StartMatch();
            }
            y += rowStep;

            var spawnEnemyStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = new Color(1f, 0.6f, 0.9f) }
            };
            if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), "Spawn Enemy (2m away)", spawnEnemyStyle))
            {
                arenaFrame?.SetFakeOpponent(2f);
            }
            y += rowStep;

            var spawnEnemyUnitStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = new Color(1f, 0.85f, 0.5f) }
            };
            if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), "Spawn Enemy Unit", spawnEnemyUnitStyle))
            {
                var types = (UnitType[])System.Enum.GetValues(typeof(UnitType));
                matchState.TrySpawnEnemyUnit(types[Random.Range(0, types.Length)]);
            }
            y += rowStep;

            if (trackingDebugOverlay != null)
            {
                var lineToggleStyle = new GUIStyle(GUI.skin.button)
                {
                    fontSize = Mathf.RoundToInt(15f * s),
                    normal = { textColor = trackingDebugOverlay.ShowLine ? Color.white : Color.gray }
                };
                string lineLabel = trackingDebugOverlay.ShowLine ? "Hide Lane Line" : "Show Lane Line";
                if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s), lineLabel, lineToggleStyle))
                {
                    trackingDebugOverlay.ShowLine = !trackingDebugOverlay.ShowLine;
                }
                y += rowStep;
            }

            DrawMqttSection(width, y, s);
        }

        private void DrawMqttSection(float width, float sectionTop, float s)
        {
            float rowStep = (RowHeight + RowGap) * s;

            var toggleStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = new Color(0.7f, 1f, 0.7f) }
            };
            string arrow = mqttExpanded ? "▼" : "▶";
            if (GUI.Button(new Rect(Screen.width - width, sectionTop, width - 20f * s, RowHeight * s), $"{arrow} MQTT", toggleStyle))
            {
                mqttExpanded = !mqttExpanded;
            }

            if (!mqttExpanded) return;

            if (mqttConnection == null) return;

            float y = sectionTop + rowStep;
            mqttConnection.BrokerHost = GUI.TextField(new Rect(Screen.width - width, y, width - 20f * s, 30f * s), mqttConnection.BrokerHost,
                new GUIStyle(GUI.skin.textField) { fontSize = Mathf.RoundToInt(14f * s) });
            y += 34f * s;

            bool connected = mqttConnection.IsConnected;
            var statusStyle = new GUIStyle(GUI.skin.label)
            {
                fontSize = Mathf.RoundToInt(14f * s),
                normal = { textColor = connected ? Color.green : Color.gray }
            };
            GUI.Label(new Rect(Screen.width - width, y, width - 20f * s, 24f * s), connected ? "CONNECTED" : "disconnected", statusStyle);
            y += 26f * s;

            if (!connected)
            {
                bool connecting = mqttConnection.IsConnecting;
                GUI.enabled = !connecting;
                if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s),
                    connecting ? "Connecting..." : "Connect", new GUIStyle(GUI.skin.button) { fontSize = Mathf.RoundToInt(15f * s) }))
                {
                    mqttConnection.Connect();
                    mqttConnection.Subscribe(HeartbeatTopic, OnMqttMessage);
                }
                GUI.enabled = true;
            }
            else if (GUI.Button(new Rect(Screen.width - width, y, width - 20f * s, RowHeight * s),
                "Disconnect", new GUIStyle(GUI.skin.button) { fontSize = Mathf.RoundToInt(15f * s) }))
            {
                mqttConnection.Disconnect();
            }
            y += rowStep;

            var logStyle = new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(12f * s), wordWrap = true };
            GUI.Label(new Rect(Screen.width - width, y, width - 20f * s, 20f * s), $"Sent {sentSeq}, last {MaxLogLines}:", logStyle);
            y += 22f * s;
            foreach (var line in receivedLog)
            {
                GUI.Label(new Rect(Screen.width - width, y, width - 20f * s, 20f * s), line, logStyle);
                y += 20f * s;
            }
        }

        private void RestartGame()
        {
            matchState.ResetMatch();
            arenaFrame?.DebugForgetOpponent();
            matchSetup?.DebugReset();
            matchCountdown?.ResetCountdownGate();
            matchNetworkSync?.DebugReset();
            peerReadySync?.DebugReset();
        }

        private void DrawPausedTag(float s)
        {
            if (!matchState.Paused) return;

            var style = new GUIStyle(GUI.skin.label)
            {
                fontSize = Mathf.RoundToInt(16f * s),
                fontStyle = FontStyle.Bold,
                alignment = TextAnchor.MiddleCenter,
                normal = { textColor = Color.yellow }
            };
            GUI.Label(new Rect(Screen.width / 2f - 80f * s, 62f * s, 160f * s, 30f * s), "❚❚ PAUSED", style);
        }
    }
}
