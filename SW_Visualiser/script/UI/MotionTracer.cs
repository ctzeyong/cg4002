using UnityEngine;
using UnityEngine.UI;

namespace BridgeRush.UI
{
    // A small glowing dot with a short fading tail that follows the card's motion path exactly, so the card shows how the
    // gesture is performed, which way it goes and how fast. A jab snaps out and rests; a punch winds up and drives through.
    public class MotionTracer : MonoBehaviour
    {
        private const int TrailDots = 4;

        private Vector2[][] paths;
        private float[][] cumulative;
        private float[] lengths;
        private RectTransform area;
        private RectTransform[] dots;
        private Image[] dotImages;
        private Color color;
        private float cycleSeconds;
        private float travelSeconds;
        private float trailSpacing;
        private UITextureFactory.MotionEase ease;

        public static void Attach(RectTransform iconRect, string motion, Color dotColor)
        {
            var tracer = iconRect.gameObject.AddComponent<MotionTracer>();
            tracer.area = iconRect;
            tracer.color = dotColor;
            UITextureFactory.GetMotionTiming(motion, out tracer.cycleSeconds, out tracer.travelSeconds, out tracer.ease);
            tracer.trailSpacing = tracer.ease == UITextureFactory.MotionEase.Snap ? 0.09f : 0.05f;
            tracer.SetPaths(UITextureFactory.GetMotionPath(motion));

            tracer.dots = new RectTransform[TrailDots + 1];
            tracer.dotImages = new Image[TrailDots + 1];
            for (int i = 0; i <= TrailDots; i++)
            {
                var go = UIFactory.CreateRect(i == 0 ? "MotionDot" : "MotionTrail" + i, iconRect);
                tracer.dots[i] = (RectTransform)go.transform;
                float size = Mathf.Lerp(15f, 6f, i / (float)TrailDots);
                tracer.dots[i].sizeDelta = new Vector2(size, size);
                var image = go.AddComponent<Image>();
                image.sprite = UITextureFactory.CreateGlowDot();
                image.color = new Color(dotColor.r, dotColor.g, dotColor.b, 0f);
                image.raycastTarget = false;
                tracer.dotImages[i] = image;
            }
        }

        private void SetPaths(Vector2[][] motionPaths)
        {
            paths = motionPaths;
            cumulative = new float[paths.Length][];
            lengths = new float[paths.Length];
            for (int i = 0; i < paths.Length; i++)
            {
                var path = paths[i];
                cumulative[i] = new float[path.Length];
                for (int j = 1; j < path.Length; j++) cumulative[i][j] = cumulative[i][j - 1] + Vector2.Distance(path[j - 1], path[j]);
                lengths[i] = path.Length > 0 ? cumulative[i][path.Length - 1] : 0f;
            }
        }

        private float Eased(float t)
        {
            switch (ease)
            {
                case UITextureFactory.MotionEase.Snap: return 1f - (1f - t) * (1f - t) * (1f - t);
                case UITextureFactory.MotionEase.Drive: return t * t * (1.6f - 0.6f * t);
                default: return Mathf.SmoothStep(0f, 1f, t);
            }
        }

        private void Update()
        {
            if (paths == null || paths.Length == 0 || dotImages == null) return;

            float slot = cycleSeconds / paths.Length;
            float time = Time.unscaledTime % cycleSeconds;
            int index = Mathf.Min(paths.Length - 1, (int)(time / slot));
            float local = time - index * slot;
            if (local > travelSeconds || lengths[index] < 0.0001f)
            {
                SetAllAlpha(0f);
                return;
            }

            float t = local / travelSeconds;
            float distance = Eased(t) * lengths[index];

            Rect rect = area.rect;
            float side = Mathf.Min(rect.width, rect.height);
            float fade = Mathf.Clamp01(Mathf.Min(t, 1f - t) * 8f + 0.2f);
            for (int i = 0; i <= TrailDots; i++)
            {
                float d = Mathf.Max(0f, distance - i * trailSpacing);
                dots[i].anchoredPosition = (PointAt(index, d) - new Vector2(0.5f, 0.5f)) * side;
                float tail = 1f - i / (float)(TrailDots + 1);
                dotImages[i].color = new Color(color.r, color.g, color.b, fade * tail * tail);
            }
        }

        private void SetAllAlpha(float alpha)
        {
            foreach (var image in dotImages) image.color = new Color(color.r, color.g, color.b, alpha);
        }

        private Vector2 PointAt(int pathIndex, float distance)
        {
            var path = paths[pathIndex];
            var along = cumulative[pathIndex];
            for (int i = 1; i < path.Length; i++)
            {
                if (distance > along[i]) continue;
                float segment = along[i] - along[i - 1];
                float t = segment > 0.0001f ? (distance - along[i - 1]) / segment : 0f;
                return Vector2.Lerp(path[i - 1], path[i], t);
            }
            return path[path.Length - 1];
        }
    }
}
