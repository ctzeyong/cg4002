using UnityEngine;

namespace BridgeRush.Gameplay
{
    // A glowing bolt with a short trail that flies from a ranged unit to its target and bursts on arrival.
    // Built at runtime from code (no assets). Purely visual: damage is applied by MatchState.
    public class RangedBolt : MonoBehaviour
    {
        private const float ImpactSeconds = 0.14f;
        private static Material material;

        private Transform target;
        private Vector3 targetOffset;
        private Vector3 lastPoint;
        private float speed;
        private float size;
        private float impactElapsed = -1f;
        private Material bodyMaterial;

        public static void Fire(Vector3 from, Transform target, Vector3 targetOffset, Vector3 fallbackPoint, Color color, float size, float speed)
        {
            var go = new GameObject("RangedBolt");
            go.transform.position = from;

            var body = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            Destroy(body.GetComponent<Collider>());
            body.transform.SetParent(go.transform, false);
            body.transform.localScale = Vector3.one * size;

            var bolt = go.AddComponent<RangedBolt>();
            bolt.bodyMaterial = new Material(GetMaterial()) { color = Color.Lerp(color, Color.white, 0.5f) };
            body.GetComponent<Renderer>().sharedMaterial = bolt.bodyMaterial;
            bolt.target = target;
            bolt.targetOffset = targetOffset;
            bolt.lastPoint = target != null ? target.position + targetOffset : fallbackPoint;
            bolt.speed = Mathf.Max(0.1f, speed);
            bolt.size = size;

            var trail = go.AddComponent<TrailRenderer>();
            trail.material = GetMaterial();
            trail.time = 0.16f;
            trail.startWidth = size * 0.8f;
            trail.endWidth = 0f;
            trail.startColor = new Color(color.r, color.g, color.b, 0.9f);
            trail.endColor = new Color(color.r, color.g, color.b, 0f);
            trail.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            trail.receiveShadows = false;
        }

        private void Update()
        {
            if (impactElapsed >= 0f)
            {
                impactElapsed += Time.deltaTime;
                float t = Mathf.Clamp01(impactElapsed / ImpactSeconds);
                transform.localScale = Vector3.one * Mathf.Lerp(1f, 3f, t);
                if (bodyMaterial != null) bodyMaterial.color = new Color(bodyMaterial.color.r, bodyMaterial.color.g, bodyMaterial.color.b, 1f - t);
                if (t >= 1f) Destroy(gameObject);
                return;
            }

            // Follows a unit that is still alive; a dead unit's last point is used so the bolt still lands.
            if (target != null) lastPoint = target.position + targetOffset;

            Vector3 toTarget = lastPoint - transform.position;
            float step = speed * Time.deltaTime;
            if (toTarget.magnitude <= step)
            {
                transform.position = lastPoint;
                impactElapsed = 0f;
                return;
            }
            transform.position += toTarget.normalized * step;
        }

        private void OnDestroy()
        {
            if (bodyMaterial != null) Destroy(bodyMaterial);
        }

        private static Material GetMaterial()
        {
            if (material != null) return material;

            const int texSize = 32;
            var texture = new Texture2D(texSize, texSize, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear
            };
            float half = texSize * 0.5f;
            for (int y = 0; y < texSize; y++)
            {
                for (int x = 0; x < texSize; x++)
                {
                    float d = Mathf.Sqrt((x - half) * (x - half) + (y - half) * (y - half)) / half;
                    texture.SetPixel(x, y, new Color(1f, 1f, 1f, Mathf.Clamp01(1.2f - d)));
                }
            }
            texture.Apply();

            material = new Material(Shader.Find("Sprites/Default")) { mainTexture = texture };
            return material;
        }
    }
}
