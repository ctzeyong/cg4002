using UnityEngine;
using BridgeRush.UI;

namespace BridgeRush.Gameplay
{
    // Fireball visuals built from code (no assets): the flying ball with a trail, and the embers rising off a burning patch.
    public static class FireEffects
    {
        private static Material material;
        private static Material flatMaterial;

        public static GameObject CreateBall(float size, Color color)
        {
            var ball = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            ball.name = "FireballBall";
            Object.Destroy(ball.GetComponent<Collider>());
            ball.transform.localScale = Vector3.one * size;
            var renderer = ball.GetComponent<Renderer>();
            renderer.sharedMaterial = GetFlatMaterial();
            var block = new MaterialPropertyBlock();
            block.SetColor("_Color", Color.Lerp(color, Color.white, 0.45f));
            renderer.SetPropertyBlock(block);
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;

            var trail = ball.AddComponent<TrailRenderer>();
            trail.sharedMaterial = GetFlatMaterial();
            trail.time = 0.28f;
            trail.startWidth = size * 0.9f;
            trail.endWidth = 0f;
            trail.startColor = new Color(color.r, color.g, color.b, 0.9f);
            trail.endColor = new Color(1f, 0.15f, 0.05f, 0f);
            trail.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            trail.receiveShadows = false;
            return ball;
        }

        // Rising embers over the burning patch. Call Stop on the returned system when the burn ends; it cleans itself up.
        public static ParticleSystem CreateEmbers(Vector3 center, float radius, float scale, Color color)
        {
            var go = new GameObject("FireballEmbers");
            go.SetActive(false);
            go.transform.position = center;
            go.transform.rotation = Quaternion.Euler(-90f, 0f, 0f);

            var ps = go.AddComponent<ParticleSystem>();
            var main = ps.main;
            main.playOnAwake = false;
            main.loop = true;
            main.simulationSpace = ParticleSystemSimulationSpace.World;
            main.maxParticles = 80;
            main.startLifetime = new ParticleSystem.MinMaxCurve(0.5f, 1f);
            main.startSpeed = new ParticleSystem.MinMaxCurve(0.05f * scale, 0.14f * scale);
            main.startSize = new ParticleSystem.MinMaxCurve(0.018f * scale, 0.04f * scale);
            main.startColor = new ParticleSystem.MinMaxGradient(color, new Color(1f, 0.85f, 0.3f));

            var emission = ps.emission;
            emission.rateOverTime = 40f;

            var shape = ps.shape;
            shape.enabled = true;
            shape.shapeType = ParticleSystemShapeType.Circle;
            shape.radius = radius * 0.9f;
            shape.radiusThickness = 1f;

            var fade = new Gradient();
            fade.SetKeys(
                new[] { new GradientColorKey(new Color(1f, 0.85f, 0.35f), 0f), new GradientColorKey(new Color(1f, 0.3f, 0.1f), 0.6f), new GradientColorKey(new Color(0.4f, 0.05f, 0.02f), 1f) },
                new[] { new GradientAlphaKey(0f, 0f), new GradientAlphaKey(1f, 0.15f), new GradientAlphaKey(0f, 1f) });
            var colorOverLife = ps.colorOverLifetime;
            colorOverLife.enabled = true;
            colorOverLife.color = fade;

            var sizeOverLife = ps.sizeOverLifetime;
            sizeOverLife.enabled = true;
            sizeOverLife.size = new ParticleSystem.MinMaxCurve(1f, new AnimationCurve(new Keyframe(0f, 1f), new Keyframe(1f, 0.2f)));

            var renderer = go.GetComponent<ParticleSystemRenderer>();
            renderer.sharedMaterial = GetMaterial();
            renderer.renderMode = ParticleSystemRenderMode.Billboard;
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;

            go.SetActive(true);
            ps.Play();
            return ps;
        }

        private static Material GetFlatMaterial()
        {
            if (flatMaterial == null) flatMaterial = new Material(Shader.Find("Sprites/Default"));
            return flatMaterial;
        }

        private static Material GetMaterial()
        {
            if (material != null) return material;
            material = new Material(Shader.Find("Sprites/Default")) { mainTexture = UITextureFactory.CreateGlowDot(64).texture };
            return material;
        }
    }
}
