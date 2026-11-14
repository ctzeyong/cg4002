using UnityEngine;

namespace BridgeRush.Tracking
{
    [RequireComponent(typeof(LineRenderer))]
    public class TrackingDebugOverlay : MonoBehaviour
    {
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private float markerGizmoRadius = 0.05f;
        [SerializeField] private Color trackedColor = Color.cyan;
        [SerializeField] private Color staleColor = new(0.5f, 0.5f, 0.5f, 0.5f);
        [SerializeField] private float lineWidth = 0.02f;
        [SerializeField] private bool showDebugText;

        // Hidden by default: the line between the towers was a tracking aid. The debug menu's Tracking section switches it on.
        public bool ShowLine { get; set; } = false;

        private LineRenderer lineRenderer;
        private GUIStyle textStyle;

        private void Awake()
        {
            if (arenaFrame == null)
            {
                Debug.LogError($"{nameof(TrackingDebugOverlay)}: needs an Arena Frame.", this);
                enabled = false;
                return;
            }

            lineRenderer = GetComponent<LineRenderer>();
            lineRenderer.positionCount = 2;
            lineRenderer.enabled = false;
            lineRenderer.widthCurve = AnimationCurve.Constant(0f, 1f, lineWidth);
            lineRenderer.material = new Material(Shader.Find("Sprites/Default"));
        }

        private void Update()
        {
            if (!arenaFrame.HasLane || !ShowLine)
            {
                lineRenderer.enabled = false;
                return;
            }

            lineRenderer.enabled = true;
            var lane = arenaFrame.Lane;
            lineRenderer.SetPosition(0, lane.Origin);
            lineRenderer.SetPosition(1, lane.Origin + lane.Direction * lane.Length);

            var color = arenaFrame.IsOpponentTracked ? trackedColor : staleColor;
            lineRenderer.startColor = color;
            lineRenderer.endColor = color;
        }

        private void OnGUI()
        {
            if (!showDebugText) return;

            textStyle ??= new GUIStyle(GUI.skin.label) { fontSize = 32, normal = { textColor = Color.white } };

            string status = !arenaFrame.HasLane
                ? "searching for opponent marker..."
                : arenaFrame.IsOpponentTracked
                    ? $"TRACKED  distance={arenaFrame.Lane.Length:F2}m"
                    : $"STALE (opponent out of view)  last distance={arenaFrame.Lane.Length:F2}m";

            GUI.Label(new Rect(20, 60, 800, 60), status, textStyle);
        }

        private void OnDrawGizmos()
        {
            if (arenaFrame == null || !arenaFrame.HasLane) return;

            var lane = arenaFrame.Lane;
            Gizmos.color = arenaFrame.IsOpponentTracked ? trackedColor : staleColor;
            Gizmos.DrawSphere(lane.Origin, markerGizmoRadius);
            Gizmos.DrawSphere(lane.Origin + lane.Direction * lane.Length, markerGizmoRadius);
        }
    }
}
