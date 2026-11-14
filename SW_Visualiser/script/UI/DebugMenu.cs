using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Gameplay;
using BridgeRush.Input;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Networking;
using BridgeRush.Tutorial;
using Comms.Visualiser;

namespace BridgeRush.UI
{
    public class DebugMenu : MonoBehaviour
    {
        private const string HeartbeatTopic = "debug/heartbeat";
        private const int MaxLogLines = 4;

        [SerializeField] private MatchState matchState;
        [SerializeField] private ArenaFrame arenaFrame;
        [Tooltip("Optional. Lets the Tracking category tune the tag size live.")]
        [SerializeField] private AprilTagTracker aprilTagTracker;
        private TrackingDebugOverlay laneOverlay; // found at runtime, no scene wiring
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
        private bool menuExpanded;

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

        // The frame rate during a match: it starts fresh with each match and keeps counting while this menu is closed (open it
        // afterwards to read it, because drawing the menu itself costs a little).
        private readonly FrameRateMeter frameMeter = new FrameRateMeter();
        private bool meterWasRunning;

        private void Update()
        {
            bool matchRunning = matchState != null && matchState.MatchStarted;
            if (matchRunning && !meterWasRunning) frameMeter.Reset();
            meterWasRunning = matchRunning;
            if (matchRunning) frameMeter.Tick(Time.unscaledDeltaTime);

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

        private MatchHudUI hud;

        private float UiScale
        {
            get
            {
                float scale = canvas != null ? canvas.scaleFactor : 1f;
                if (hud != null && hud.DebugButtonGuiRect.width > 1f) scale = hud.DebugButtonGuiRect.width / MatchHudUI.DebugButtonWidth;
                return scale * Mathf.Max(0.5f, menuScale);
            }
        }

        public bool MenuExpanded => menuExpanded;
        public void ToggleMenu() => menuExpanded = !menuExpanded;
        public void SetMenuOpen(bool open) => menuExpanded = open;

        private enum Category { None, Match, Opponent, Gestures, Tracking, Comms }

        private Category openCategory = Category.None;
        private Haptics.Strength testPulse = Haptics.Strength.Light;
        private float contentHeight;

        private void OnGUI()
        {
            float s = UiScale;

            // The settings window (gear button, MatchHudUI) opens this menu. MatchHudUI keeps an invisible anchor rectangle
            // under the gear; this draws the panel under it, at the same scale, so it lines up whatever the canvas scale is.
            if (!menuExpanded) return;
            float cs = canvas != null ? canvas.scaleFactor : 1f;
            var debugButtonRect = new Rect(Screen.width - 240f * cs, 170f * cs, 220f * cs, 56f * cs);
            if (hud == null) hud = FindAnyObjectByType<MatchHudUI>();
            if (hud != null && hud.DebugButtonGuiRect.width > 1f) debugButtonRect = hud.DebugButtonGuiRect;

            float width = PanelWidth * s;
            float top = debugButtonRect.yMax + 8f * (debugButtonRect.width / MatchHudUI.DebugButtonWidth);
            float left = Screen.width - width;
            float rowWidth = width - 20f * s;

            var panelStyle = new GUIStyle(GUI.skin.box)
            {
                normal = { background = Texture2D.grayTexture },
                alignment = TextAnchor.UpperLeft,
                padding = new RectOffset(10, 10, 8, 10)
            };
            float height = contentHeight + 16f * s;
            GUI.Box(new Rect(Screen.width - width - 10f * s, top, width, height), string.Empty, panelStyle);

            float start = top + 6f * s;
            float y = start;

            // The menu is opened from the settings window (gear), so it carries its own way out.
            if (Row("Close debug menu  x", left, ref y, rowWidth, new Color(1f, 0.6f, 0.55f))) menuExpanded = false;

            bool godModeAvailable = matchState.GodModeAvailable;
            string godModeLabel = !godModeAvailable
                ? "God mode: N/A (client)"
                : $"God mode: {(matchState.GodMode ? "ON" : "OFF")}{(matchState.IsBoardClient ? " (board)" : string.Empty)}";
            if (Row(godModeLabel, left, ref y, rowWidth, matchState.GodMode ? new Color(1f, 0.85f, 0.3f) : new Color(1f, 0.95f, 0.7f)) && godModeAvailable)
            {
                matchState.SetGodMode(!matchState.GodMode);
            }

            string fpsLabel = frameMeter.Frames < 100
                ? "FPS: play a match to measure"
                : $"FPS {frameMeter.Fps:0}   avg {frameMeter.AverageFps:0.0}\n1% low {frameMeter.OnePercentLowFps:0} - {frameMeter.Seconds:0} s - tap resets";
            if (Row(fpsLabel, left, ref y, rowWidth, new Color(0.7f, 1f, 0.8f), 2)) frameMeter.Reset();

            var latency = matchState.SummonLatency;
            string latencyLabel = latency.Count == 0
                ? "Summon -> drawn: needs a\nVS PLAYER match on the board"
                : $"Summon->drawn avg {latency.AverageMs:0} ms\nmin {latency.MinMs:0} max {latency.MaxMs:0} n {latency.Count} - tap resets";
            if (Row(latencyLabel, left, ref y, rowWidth, new Color(0.7f, 1f, 0.8f), 2)) latency.Reset();

            var coach = TutorialCoach.Instance;
            if (coach != null && coach.IsRunning && Row("Tutorial: skip this step", left, ref y, rowWidth, new Color(0.6f, 0.85f, 1f)))
            {
                coach.SkipStep();
            }

            y = DrawCategory(Category.Match, "Match", left, y, rowWidth, s, DrawMatch);
            y = DrawCategory(Category.Opponent, "Opponent", left, y, rowWidth, s, DrawOpponent);
            y = DrawCategory(Category.Gestures, "Gestures", left, y, rowWidth, s, DrawGestures);
            y = DrawCategory(Category.Tracking, "Tracking", left, y, rowWidth, s, DrawTracking);
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

        // A row of buttons' height; `lines` above 1 makes it taller and wraps the label, for a readout that does not fit on one line.
        private bool Row(string label, float left, ref float y, float rowWidth, Color color, int lines = 1)
        {
            float s = UiScale;
            var style = new GUIStyle(GUI.skin.button)
            {
                fontSize = Mathf.RoundToInt(15f * s),
                wordWrap = lines > 1,
                normal = { textColor = color }
            };
            float height = (RowHeight + (lines - 1) * 20f) * s;
            bool pressed = GUI.Button(new Rect(left, y, rowWidth, height), label, style);
            y += height + RowGap * s;
            return pressed;
        }

        private float DrawMatch(float left, float y, float rowWidth)
        {
            GUI.Label(new Rect(left, y, rowWidth, 44f * UiScale),
                $"Arena scale {matchState.ArenaScale:0.00}  (lane {matchState.TotalLaneLength:0.00} m)\nNotice {matchState.EffectiveNoticeRangeMeters:0.00} m, melee hit {matchState.EffectiveHitReachMeters:0.00} m",
                new GUIStyle(GUI.skin.label) { fontSize = Mathf.RoundToInt(14f * UiScale), normal = { textColor = new Color(0.8f, 1f, 0.8f) } });
            y += 48f * UiScale;
            if (Row("Force Start (skip tracking)", left, ref y, rowWidth, new Color(0.6f, 0.85f, 1f)))
            {
                matchState.StartMatch();
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

            // The cyan line between the two towers. It was a tracking aid; it is hidden unless switched on here.
            if (laneOverlay == null) laneOverlay = FindAnyObjectByType<TrackingDebugOverlay>();
            if (laneOverlay != null && Row($"Lane line: {(laneOverlay.ShowLine ? "shown" : "hidden")}", left, ref y, rowWidth, Color.white))
            {
                laneOverlay.ShowLine = !laneOverlay.ShowLine;
            }

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
                // If a centred tag lines up but a tag off-centre lands a quarter turn away (iPad), tap to turn the tag's position.
                if (Row($"Tag turn: {aprilTagTracker.TagFrameQuarterTurns * 90} deg (tap to turn 90)", left, ref y, rowWidth, Color.white))
                {
                    aprilTagTracker.TagFrameQuarterTurns++;
                }
            }

            // For a device (an iPad on iPadOS 26 and later) whose 3D world comes out turned a quarter turn against the room: tap to
            // turn the camera 90 degrees at a time until the towers stand upright. 0 leaves the device as it is; remembered per device.
            if (Row($"Camera roll: {CameraRollCorrection.Degrees} deg (tap to turn 90)", left, ref y, rowWidth, Color.white))
            {
                CameraRollCorrection.Cycle();
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
    }
}
