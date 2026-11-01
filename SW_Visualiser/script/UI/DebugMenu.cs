using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Gameplay;
using BridgeRush.Input;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Networking;
using Comms.Visualiser;

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
        [Tooltip("Optional. Lets the Tracking category tune the tag size live.")]
        [SerializeField] private AprilTagTracker aprilTagTracker;
        [Tooltip("Optional. Lets the Gestures category trigger glove gestures without the hardware.")]
        [SerializeField] private GesturePad gesturePad;
        [Tooltip("Optional. Shows how the floor height was found, in the Tracking category.")]
        [SerializeField] private FloorHeightProvider floorHeightProvider;
        [SerializeField] private Canvas canvas;
        [Tooltip("Makes the whole debug menu bigger or smaller. 1 is the original size.")]
        [SerializeField] private float menuScale = 1.5f;

        [Header("Comms")]
        [Tooltip("Opens the SSH tunnel to the Ultra96 broker. Its credential form is the panel at the top of the screen.")]
        [SerializeField] private CommsTunnelBridge commsBridge;
        [Tooltip("The one shared connection, used for the heartbeat check.")]
        [SerializeField] private MqttConnection mqttConnection;
        [SerializeField] private float mqttHeartbeatIntervalSeconds = 1f;

        private float timeSinceLastHeartbeat;
        private int sentSeq;
        private readonly List<string> receivedLog = new();
            private bool menuExpanded = true;

        private void Awake()
        {
            if (matchState == null)
            {
                Debug.LogError($"{nameof(DebugMenu)}: needs a Match State.", this);
                enabled = false;
            }
        }

        private void Start()
        {
            mqttConnection?.Subscribe(HeartbeatTopic, OnMqttMessage);
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
        private const float DetailsReferenceWidth = 800f;

        private float UiScale => (canvas != null ? canvas.scaleFactor : 1f) * Mathf.Max(0.5f, menuScale);

        private enum Category { None, Match, Opponent, Gestures, Tracking, Comms }

        private Category openCategory = Category.None;
        private Haptics.Strength testPulse = Haptics.Strength.Light;
        private float contentHeight;

        private void OnGUI()
        {
            float s = UiScale;

            DrawPausedTag(s);

            float width = PanelWidth * s;
            float top = 340f * s;
            float left = Screen.width - width;
            float rowWidth = width - 20f * s;
            float rowStep = (RowHeight + RowGap) * s;

            var panelStyle = new GUIStyle(GUI.skin.box)
            {
                normal = { background = Texture2D.grayTexture },
                alignment = TextAnchor.UpperLeft,
                padding = new RectOffset(10, 10, 8, 10)
            };
            float height = TitleHeight * s + (menuExpanded ? contentHeight + 10f * s : 10f * s);
            GUI.Box(new Rect(Screen.width - width - 10f * s, top, width, height), string.Empty, panelStyle);

            var titleStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(17f * s),
                fontStyle = FontStyle.Bold,
                normal = { textColor = new Color(1f, 0.55f, 0.25f) }
            };
            string titleArrow = menuExpanded ? "▼" : "▶";
            if (GUI.Button(new Rect(left, top + 4f * s, rowWidth, TitleHeight * s - 8f * s), $"{titleArrow} DEBUG", titleStyle))
            {
                menuExpanded = !menuExpanded;
            }

            if (!menuExpanded) return;

            float start = top + TitleHeight * s;
            float y = start;

            bool godModeAvailable = matchState.Role == MatchState.NetworkRole.Local || matchState.Role == MatchState.NetworkRole.Host;
            string godModeLabel = !godModeAvailable
                ? (matchState.IsBoardClient ? "God mode: N/A (board)" : "God mode: N/A (client)")
                : $"God mode: {(matchState.GodMode ? "ON" : "OFF")}";
            if (Row(godModeLabel, left, ref y, rowWidth, matchState.GodMode ? new Color(1f, 0.85f, 0.3f) : new Color(1f, 0.95f, 0.7f)) && godModeAvailable)
            {
                matchState.GodMode = !matchState.GodMode;
            }

            if (matchNetworkSync != null && matchNetworkSync.BoardEngineWired)
            {
                bool board = matchNetworkSync.UseBoardEngine;
                if (Row($"VS PLAYER engine: {(board ? "BOARD" : "HOST PHONE")} (next match)", left, ref y, rowWidth,
                        board ? new Color(0.6f, 0.9f, 1f) : Color.white))
                    matchNetworkSync.UseBoardEngine = !board;
            }

            y = DrawCategory(Category.Match, "Match", left, y, rowWidth, s, DrawMatch);
            y = DrawCategory(Category.Opponent, "Opponent", left, y, rowWidth, s, DrawOpponent);
            y = DrawCategory(Category.Gestures, "Gestures", left, y, rowWidth, s, DrawGestures);
            if (trackingDebugOverlay != null)
            {
                y = DrawCategory(Category.Tracking, "Tracking", left, y, rowWidth, s, DrawTracking);
            }
            y = DrawCategory(Category.Comms, "Comms", left, y, rowWidth, s, DrawComms);

            contentHeight = y - start;
        }

        private float DrawCategory(Category category, string label, float left, float y, float rowWidth, float s,
            System.Func<float, float, float, float> drawBody)
        {
            float rowStep = (RowHeight + RowGap) * s;
            bool open = openCategory == category;

            var headerStyle = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                fontStyle = FontStyle.Bold,
                normal = { textColor = new Color(0.7f, 1f, 0.7f) }
            };
            if (GUI.Button(new Rect(left, y, rowWidth, RowHeight * s), $"{(open ? "▼" : "▶")} {label}", headerStyle))
            {
                openCategory = open ? Category.None : category;
            }
            y += rowStep;

            return open ? drawBody(left, y, rowWidth) : y;
        }

        private bool Row(string label, float left, ref float y, float rowWidth, Color color)
        {
            float s = UiScale;
            var style = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                normal = { textColor = color }
            };
            bool pressed = GUI.Button(new Rect(left, y, rowWidth, RowHeight * s), label, style);
            y += (RowHeight + RowGap) * s;
            return pressed;
        }

        private float DrawMatch(float left, float y, float rowWidth)
        {
            GUI.Label(new Rect(left, y, rowWidth, 44f * UiScale),
                $"Arena scale {matchState.ArenaScale:0.00}  (lane {matchState.TotalLaneLength:0.00} m)\nNotice {matchState.EffectiveNoticeRangeMeters:0.00} m, melee hit {matchState.EffectiveHitReachMeters:0.00} m",
                new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * UiScale), normal = { textColor = new Color(0.8f, 1f, 0.8f) } });
            y += 48f * UiScale;
            if (Row("Restart Game", left, ref y, rowWidth, new Color(1f, 0.7f, 0.6f)))
            {
                RestartGame();
            }
            if (Row(matchState.Paused ? "Resume" : "Pause", left, ref y, rowWidth, matchState.Paused ? Color.yellow : Color.white))
            {
                matchState.TogglePause();
            }
            if (Row("Force Start (skip tracking)", left, ref y, rowWidth, new Color(0.6f, 0.85f, 1f)))
            {
                matchState.StartMatch();
            }
            if (Row($"Haptics: {(Haptics.Enabled ? "ON" : "OFF")} (tap to switch)", left, ref y, rowWidth, new Color(1f, 0.85f, 0.7f)))
            {
                Haptics.Enabled = !Haptics.Enabled;
            }
            if (Row($"Test pulse: {testPulse}", left, ref y, rowWidth, new Color(1f, 0.85f, 0.7f)))
            {
                Haptics.Pulse(testPulse);
                testPulse = testPulse == Haptics.Strength.Heavy ? Haptics.Strength.Light : testPulse + 1;
            }
            if (Row("Jump to last minute (test 2x mana)", left, ref y, rowWidth, new Color(1f, 0.9f, 0.5f)))
            {
                matchState.DebugSetTimeRemaining(matchState.DoubleManaLastSeconds - 1f);
            }
            if (Row($"Music: {(GameMusic.Enabled ? "ON" : "OFF")}", left, ref y, rowWidth, new Color(0.8f, 0.9f, 1f)))
            {
                GameMusic.Enabled = !GameMusic.Enabled;
            }
            if (Row($"Sound: {(GameAudio.Muted ? "OFF" : "ON")}", left, ref y, rowWidth, new Color(0.9f, 0.8f, 1f)))
            {
                GameAudio.Muted = !GameAudio.Muted;
            }
            if (Row($"Bridges: {(matchState.TwoBridges ? 2 : 1)} (tap, then Restart)", left, ref y, rowWidth, new Color(0.8f, 0.9f, 0.6f)))
            {
                matchState.TwoBridges = !matchState.TwoBridges;
            }
            return y;
        }

        private float DrawOpponent(float left, float y, float rowWidth)
        {
            if (Row("Spawn Enemy (2m away)", left, ref y, rowWidth, new Color(1f, 0.6f, 0.9f)))
            {
                arenaFrame?.SetFakeOpponent(2f);
            }
            if (Row("Spawn Enemy Unit", left, ref y, rowWidth, new Color(1f, 0.85f, 0.5f)))
            {
                var types = (UnitType[])System.Enum.GetValues(typeof(UnitType));
                matchState.TrySpawnEnemyUnit(types[Random.Range(0, types.Length)]);
            }
            return y;
        }

        private float DrawGestures(float left, float y, float rowWidth)
        {
            if (gesturePad == null)
            {
                GUI.Label(new Rect(left, y, rowWidth, 24f * UiScale), "Gesture Pad not assigned",
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * UiScale), normal = { textColor = Color.gray } });
                return y + 26f * UiScale;
            }

            foreach (var name in GestureBindings.GloveGestures)
            {
                string unit = GestureBindings.ActionName(GestureBindings.FromGlove(name));
                if (Row($"{name} -> {unit}", left, ref y, rowWidth, Color.white))
                {
                    gesturePad.PerformGloveGesture(name, "test");
                }
            }
            return y;
        }

        private float DrawTracking(float left, float y, float rowWidth)
        {
            float s = UiScale;
            string reading = arenaFrame != null && arenaFrame.HasLane
                ? $"Tag distance: {arenaFrame.Lane.Length:0.00} m{(arenaFrame.IsOpponentTracked ? string.Empty : " (not in view)")}"
                : "Tag distance: no tag yet";
            GUI.Label(new Rect(left, y, rowWidth, 26f * s), reading,
                new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * s), normal = { textColor = Color.white } });
            y += 28f * s;

            if (aprilTagTracker != null)
            {
                GUI.Label(new Rect(left, y, rowWidth, 26f * s), $"Tag size: {aprilTagTracker.TagSizeMeters:0.000} m",
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * s), normal = { textColor = Color.white } });
                y += 28f * s;

                var stepStyle = new GUIStyle(GUI.skin.button) { fontSize = Mathf.RoundToInt(15f * s) };
                float quarter = (rowWidth - 18f * s) * 0.25f;
                float[] steps = { -0.01f, -0.001f, 0.001f, 0.01f };
                string[] labels = { "-.01", "-.001", "+.001", "+.01" };
                for (int i = 0; i < steps.Length; i++)
                {
                    if (GUI.Button(new Rect(left + i * (quarter + 6f * s), y, quarter, RowHeight * s), labels[i], stepStyle))
                    {
                        aprilTagTracker.TagSizeMeters += steps[i];
                    }
                }
                y += (RowHeight + RowGap) * s;
            }

            if (aprilTagTracker != null)
            {
                GUI.Label(new Rect(left, y, rowWidth, 40f * s), aprilTagTracker.DebugInfo,
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(11f * s), wordWrap = true, normal = { textColor = Color.gray } });
                y += 42f * s;

                if (Row($"Rotate: {(aprilTagTracker.RotateClockwise ? "clockwise" : "counter-clockwise")}", left, ref y, rowWidth, Color.white))
                {
                    aprilTagTracker.RotateClockwise = !aprilTagTracker.RotateClockwise;
                }
                if (Row($"Mirror Y: {(aprilTagTracker.MirrorY ? "on" : "off")}", left, ref y, rowWidth, Color.white))
                {
                    aprilTagTracker.MirrorY = !aprilTagTracker.MirrorY;
                }
            }

            if (floorHeightProvider != null)
            {
                string floor = !floorHeightProvider.HasDetectedFloor
                    ? "Floor: NOT found (using 0)"
                    : $"Floor: y {floorHeightProvider.FloorY:0.00} ({(floorHeightProvider.FromFallback ? "lowest plane" : "below phone")})";
                GUI.Label(new Rect(left, y, rowWidth, 26f * s), floor,
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(13f * s), normal = { textColor = floorHeightProvider.HasDetectedFloor ? Color.white : Color.yellow } });
                y += 28f * s;

                GUI.Label(new Rect(left, y, rowWidth, 26f * s), $"Floor offset: {floorHeightProvider.FloorOffsetMeters:+0.00;-0.00;0.00} m (- lowers)",
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(13f * s), normal = { textColor = Color.white } });
                y += 28f * s;

                var floorStepStyle = new GUIStyle(GUI.skin.button) { fontSize = Mathf.RoundToInt(15f * s) };
                float quarter = (rowWidth - 18f * s) * 0.25f;
                float[] floorSteps = { -0.05f, -0.01f, 0.01f, 0.05f };
                string[] floorLabels = { "-.05", "-.01", "+.01", "+.05" };
                for (int i = 0; i < floorSteps.Length; i++)
                {
                    if (GUI.Button(new Rect(left + i * (quarter + 6f * s), y, quarter, RowHeight * s), floorLabels[i], floorStepStyle))
                    {
                        floorHeightProvider.FloorOffsetMeters += floorSteps[i];
                    }
                }
                y += (RowHeight + RowGap) * s;
            }

            if (Row(SummonCursor.ShowDiagnostic ? "Hide Aim Readout" : "Show Aim Readout", left, ref y, rowWidth,
                SummonCursor.ShowDiagnostic ? Color.white : Color.gray))
            {
                SummonCursor.ShowDiagnostic = !SummonCursor.ShowDiagnostic;
            }

            bool shown = trackingDebugOverlay.ShowLine;
            if (Row(shown ? "Hide Lane Line" : "Show Lane Line", left, ref y, rowWidth, shown ? Color.white : Color.gray))
            {
                trackingDebugOverlay.ShowLine = !shown;
            }
            return y;
        }

        private float DrawComms(float left, float y, float rowWidth)
        {
            float s = UiScale;

            if (commsBridge == null)
            {
                GUI.Label(new Rect(left, y, rowWidth, 24f * s), "Comms bridge not assigned",
                    new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * s), normal = { textColor = Color.gray } });
                return y + 26f * s;
            }

            bool mqttUp = mqttConnection != null && mqttConnection.IsConnected;
            var statusStyle = new GUIStyle(GUI.skin.label)
            {
                fontSize = Mathf.RoundToInt(14f * s),
                wordWrap = true,
                normal = { textColor = mqttUp ? Color.green : Color.gray }
            };
            GUI.Label(new Rect(left, y, rowWidth, 40f * s), mqttUp ? "CONNECTED" : commsBridge.Status, statusStyle);
            y += 42f * s;

            if (Row(commsBridge.IsRunning ? "Disconnect" : "Connect", left, ref y, rowWidth, Color.white))
            {
                if (commsBridge.IsRunning) commsBridge.Disconnect();
                else commsBridge.Connect();
            }
            DrawCommsDetails(left, s);

            var logStyle = new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(12f * s), wordWrap = true };
            GUI.Label(new Rect(left, y, rowWidth, 20f * s), $"Sent {sentSeq}, last {MaxLogLines}:", logStyle);
            y += 22f * s;
            foreach (var line in receivedLog)
            {
                GUI.Label(new Rect(left, y, rowWidth, 20f * s), line, logStyle);
                y += 20f * s;
            }
            return y;
        }

        private void DrawCommsDetails(float menuLeft, float s)
        {
            float scale = Screen.width / DetailsReferenceWidth;
            float areaWidth = (menuLeft - 30f * s) / scale;
            if (areaWidth < 200f) return;

            var previous = GUI.matrix;
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));
            GUILayout.BeginArea(new Rect(10f / scale, 10f / scale, areaWidth, Screen.height / scale * 0.7f), GUI.skin.box);
            commsBridge.DrawDetails();
            GUILayout.EndArea();
            GUI.matrix = previous;
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
