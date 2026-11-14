using UnityEngine;

namespace BridgeRush.Gameplay
{
    // One capture bubble: a sphere collider on the opponent's tower, a translucent orb and a progress ring.
    // It only shows state. BubbleCaptureManager decides progress, cooldown and where it sits.
    public class CaptureBubble : MonoBehaviour
    {
        private const int RingSegments = 40;
        private const float MinOrbAlpha = 0.55f;

        private Camera viewCamera;
        private float radius;
        private SphereCollider sphereCollider;
        private Material orbMaterial;
        private LineRenderer track;
        private LineRenderer fill;
        private Color idleColor;
        private Color lockingColor;
        private Color fullColor;

        public int Id { get; private set; }
        // 0..1.
        public float Progress { get; set; }
        public bool IsLocking { get; set; }
        public bool IsActive { get; private set; }

        public void Build(int id, float radiusMeters, Camera camera, Color idle, Color locking, Color full, int layer)
        {
            Id = id;
            radius = radiusMeters;
            viewCamera = camera;
            idleColor = idle;
            lockingColor = locking;
            fullColor = full;
            if (layer >= 0) gameObject.layer = layer;

            sphereCollider = gameObject.AddComponent<SphereCollider>();
            sphereCollider.radius = radiusMeters;
            sphereCollider.isTrigger = true;

            var orb = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            orb.name = "Orb";
            Destroy(orb.GetComponent<Collider>());
            orb.transform.SetParent(transform, false);
            orb.transform.localScale = Vector3.one * radiusMeters * 2f;
            orb.layer = gameObject.layer;
            var orbRenderer = orb.GetComponent<MeshRenderer>();
            orbRenderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            orbRenderer.receiveShadows = false;
            orbMaterial = new Material(Shader.Find("Sprites/Default"));
            orbRenderer.material = orbMaterial;

            // Real-world objects never hide a bubble: a depth-clear disc (see ArDepthClear) a little wider than the progress ring.
            ArDepthClear.AddSphere(transform, radiusMeters * 3.1f, gameObject.layer);

            track = BuildRing("Track", 0.012f, RingSegments + 1);
            fill = BuildRing("Fill", 0.03f, RingSegments + 1);
            SetActive(false);
        }

        public void SetActive(bool active)
        {
            IsActive = active;
            sphereCollider.enabled = active;
            foreach (var r in GetComponentsInChildren<Renderer>(true)) r.enabled = active;
            if (!active)
            {
                Progress = 0f;
                IsLocking = false;
            }
        }

        private LineRenderer BuildRing(string objectName, float width, int points)
        {
            var go = new GameObject(objectName);
            go.transform.SetParent(transform, false);
            go.layer = gameObject.layer;
            var line = go.AddComponent<LineRenderer>();
            line.useWorldSpace = true;
            line.positionCount = points;
            line.widthMultiplier = width;
            line.material = new Material(Shader.Find("Sprites/Default"));
            line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            line.receiveShadows = false;
            return line;
        }

        private void LateUpdate()
        {
            if (!IsActive || viewCamera == null) return;

            float pulse = IsLocking ? 0.85f + 0.15f * Mathf.Sin(Time.time * 12f) : 1f;
            Color orbColor = IsLocking ? Color.Lerp(lockingColor, fullColor, Progress) : idleColor;
            orbColor.a = Mathf.Max(orbColor.a, MinOrbAlpha) * pulse;   // never so faint it is lost against the room
            orbMaterial.color = orbColor;

            // Ring faces the camera: points are laid out on the camera's right/up plane around the bubble.
            Vector3 right = viewCamera.transform.right;
            Vector3 up = viewCamera.transform.up;
            float ringRadius = radius * 1.35f;
            int filled = Mathf.Clamp(Mathf.RoundToInt(Progress * RingSegments), 0, RingSegments);

            track.startColor = track.endColor = new Color(1f, 1f, 1f, 0.5f);
            fill.startColor = fill.endColor = Color.Lerp(lockingColor, fullColor, Progress);
            fill.enabled = filled > 0;
            fill.positionCount = Mathf.Max(filled + 1, 2);
            for (int i = 0; i <= RingSegments; i++)
            {
                // Starts at the top and runs clockwise as seen by the player.
                float angle = Mathf.PI * 0.5f - i / (float)RingSegments * Mathf.PI * 2f;
                Vector3 point = transform.position + (right * Mathf.Cos(angle) + up * Mathf.Sin(angle)) * ringRadius;
                track.SetPosition(i, point);
                if (i <= filled && i < fill.positionCount) fill.SetPosition(i, point);
            }
        }
    }
}
