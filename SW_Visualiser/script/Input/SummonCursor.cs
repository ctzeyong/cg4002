using TMPro;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.UI;

namespace BridgeRush.Input
{
    public class SummonCursor : MonoBehaviour
    {
        [SerializeField] private Camera aimCamera;
        [SerializeField] private MatchState matchState;
        [SerializeField] private Canvas canvas;

        [Header("Look")]
        [SerializeField] private float maxAimDistanceMeters = 4f;
        [SerializeField] private float ringRadiusMeters = 0.05f;
        [SerializeField] private Color validColor = new(0.3f, 1f, 0.4f, 1f);
        [SerializeField] private Color invalidColor = new(1f, 0.3f, 0.25f, 1f);

        private const int RingSegments = 48;
        private LineRenderer ring;
        private LineRenderer glow;
        private TextMeshProUGUI crosshair;

        public Vector3 AimedWorldPosition { get; private set; }
        public bool IsAimValid { get; private set; }

        private void Awake()
        {
            if (aimCamera == null) aimCamera = Camera.main;

            ring = BuildRing("SummonRing", 0.014f);
            glow = BuildRing("SummonGlow", 0.07f);
            BuildCrosshair();
            SetVisible(false);
        }

        private void Update()
        {
            bool playing = matchState.MatchStarted;
            if (!playing)
            {
                IsAimValid = false;
                SetVisible(false);
                return;
            }

            Aim();
            UpdateCursor();
        }

        // look to aim mechanic , raycast from the center of the screen to find a point on the ground,
        //  check if it is inside the player's territory
        private void Aim()
        {
            IsAimValid = false;

            float groundY = matchState.GroundY;
            var floor = new Plane(Vector3.up, new Vector3(0f, groundY, 0f));
            var ray = aimCamera.ViewportPointToRay(new Vector3(0.5f, 0.5f, 0f));
            if (!floor.Raycast(ray, out float distance))
            {
                SetVisible(false);
                return;
            }

            Vector3 hit = ray.GetPoint(distance);
            if (Vector3.Distance(aimCamera.transform.position, hit) > maxAimDistanceMeters)
            {
                SetVisible(false);
                return;
            }

            AimedWorldPosition = hit;
            IsAimValid = matchState.IsInsideOwnTerritory(hit);
        }

        // set the color red or green depending on the aimed position
        private void UpdateCursor()
        {
            SetVisible(true);
            Color color = IsAimValid ? validColor : invalidColor;
            if (crosshair != null) crosshair.color = color;

            float pulse = 0.6f + 0.4f * Mathf.PingPong(Time.time * 1.6f, 1f);
            SetRing(ring, AimedWorldPosition, matchState.GroundY, ringRadiusMeters, new Color(color.r, color.g, color.b, 1f));
            SetRing(glow, AimedWorldPosition, matchState.GroundY, ringRadiusMeters, new Color(color.r, color.g, color.b, 0.28f * pulse));
        }

        private void SetVisible(bool visible)
        {
            if (crosshair != null) crosshair.gameObject.SetActive(visible);
            ring.enabled = visible;
            glow.enabled = visible;
        }

        private LineRenderer BuildRing(string objectName, float width)
        {
            var go = new GameObject(objectName);
            go.transform.SetParent(transform, false);

            var line = go.AddComponent<LineRenderer>();
            line.useWorldSpace = true;
            line.loop = true;
            line.positionCount = RingSegments;
            line.widthMultiplier = width;
            line.material = new Material(Shader.Find("Sprites/Default"));
            line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            line.receiveShadows = false;
            return line;
        }

        private static void SetRing(LineRenderer line, Vector3 center, float groundY, float radiusMeters, Color color)
        {
            line.startColor = line.endColor = color;
            for (int i = 0; i < RingSegments; i++)
            {
                float angle = i / (float)RingSegments * Mathf.PI * 2f;
                Vector3 point = center + new Vector3(Mathf.Cos(angle) * radiusMeters, 0f, Mathf.Sin(angle) * radiusMeters);
                point.y = groundY + 0.012f;
                line.SetPosition(i, point);
            }
        }

        private void BuildCrosshair()
        {
            if (canvas == null) return;

            crosshair = UIFactory.CreateText(canvas.transform, "+", 72f, FontStyles.Bold, validColor);
            var rect = crosshair.rectTransform;
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = Vector2.zero;
            rect.sizeDelta = new Vector2(120f, 120f);
            crosshair.raycastTarget = false;
        }
    }
}
