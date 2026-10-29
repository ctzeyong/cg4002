using UnityEngine;

namespace BridgeRush.Gameplay
{
    // One-shot summon effect built at runtime from code (no assets): a ring of dust kicked out along the ground,
    // a few soft smoke puffs that rise, and sparks in the side's colour. Sized from the unit's height.
    public static class SpawnEffect
    {
        private const float LifetimeCleanupSeconds = 2.5f;
        private static Material material;

        public static void Play(Vector3 groundPosition, float unitHeight, Color accent)
        {
            var root = new GameObject("SpawnEffect");
            root.transform.position = groundPosition;
            Object.Destroy(root, LifetimeCleanupSeconds);

            var dust = new Color(0.78f, 0.74f, 0.68f, 0.55f);
            var smoke = new Color(0.86f, 0.86f, 0.88f, 0.4f);
            var spark = new Color(accent.r, accent.g, accent.b, 1f);

            // Ground-hugging ring: circle shape laid flat, particles pushed outward.
            Burst(root.transform, "Dust", Quaternion.Euler(-90f, 0f, 0f), ParticleSystemShapeType.Circle, unitHeight * 0.35f,
                  count: 14, lifetime: 0.8f, speed: unitHeight * 1.1f, size: unitHeight * 0.6f, rise: 0.05f, dust, grow: 2.2f);

            // Soft puffs that climb past the unit and thin out.
            Burst(root.transform, "Smoke", Quaternion.Euler(-90f, 0f, 0f), ParticleSystemShapeType.Circle, unitHeight * 0.2f,
                  count: 7, lifetime: 1.2f, speed: unitHeight * 0.25f, size: unitHeight * 0.9f, rise: 0.12f, smoke, grow: 1.8f);

            // Sparks thrown upward.
            Burst(root.transform, "Sparks", Quaternion.Euler(-90f, 0f, 0f), ParticleSystemShapeType.Cone, unitHeight * 0.15f,
                  count: 12, lifetime: 0.6f, speed: unitHeight * 3f, size: unitHeight * 0.14f, rise: -0.4f, spark, grow: 0.3f);
        }

        private static void Burst(Transform parent, string name, Quaternion rotation, ParticleSystemShapeType shapeType, float shapeRadius,
                                  int count, float lifetime, float speed, float size, float rise, Color color, float grow)
        {
            var go = new GameObject(name);
            // Inactive while configured: duration and loop cannot be changed on a system that is already playing.
            go.SetActive(false);
            go.transform.SetParent(parent, false);
            go.transform.localRotation = rotation;

            var ps = go.AddComponent<ParticleSystem>();
            var main = ps.main;
            main.playOnAwake = false;
            main.loop = false;
            main.duration = lifetime;
            main.simulationSpace = ParticleSystemSimulationSpace.World;
            main.maxParticles = count;
            main.startLifetime = new ParticleSystem.MinMaxCurve(lifetime * 0.7f, lifetime);
            main.startSpeed = new ParticleSystem.MinMaxCurve(speed * 0.6f, speed);
            main.startSize = new ParticleSystem.MinMaxCurve(size * 0.7f, size * 1.2f);
            main.startRotation = new ParticleSystem.MinMaxCurve(0f, Mathf.PI * 2f);
            main.startColor = color;
            main.gravityModifier = -rise;

            var emission = ps.emission;
            emission.rateOverTime = 0f;
            emission.SetBursts(new[] { new ParticleSystem.Burst(0f, (short)count) });

            var shape = ps.shape;
            shape.enabled = true;
            shape.shapeType = shapeType;
            shape.radius = shapeRadius;
            if (shapeType == ParticleSystemShapeType.Circle) shape.radiusThickness = 0f;
            else shape.angle = 22f;

            var fade = new Gradient();
            fade.SetKeys(
                new[] { new GradientColorKey(Color.white, 0f), new GradientColorKey(Color.white, 1f) },
                new[] { new GradientAlphaKey(0f, 0f), new GradientAlphaKey(1f, 0.12f), new GradientAlphaKey(0f, 1f) });
            var colorOverLife = ps.colorOverLifetime;
            colorOverLife.enabled = true;
            colorOverLife.color = fade;

            var sizeOverLife = ps.sizeOverLifetime;
            sizeOverLife.enabled = true;
            sizeOverLife.size = new ParticleSystem.MinMaxCurve(1f, new AnimationCurve(
                new Keyframe(0f, 0.35f), new Keyframe(1f, 0.35f * grow + 0.65f)));

            var renderer = go.GetComponent<ParticleSystemRenderer>();
            renderer.sharedMaterial = GetMaterial();
            renderer.renderMode = ParticleSystemRenderMode.Billboard;
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;

            go.SetActive(true);
            ps.Play();
        }

        private static Material GetMaterial()
        {
            if (material != null) return material;

            const int size = 64;
            var texture = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear
            };
            float half = size * 0.5f;
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float d = Mathf.Sqrt((x - half) * (x - half) + (y - half) * (y - half)) / half;
                    float a = Mathf.Clamp01(1f - d);
                    texture.SetPixel(x, y, new Color(1f, 1f, 1f, a * a * (3f - 2f * a)));
                }
            }
            texture.Apply();

            material = new Material(Shader.Find("Sprites/Default")) { mainTexture = texture };
            return material;
        }
    }
}
