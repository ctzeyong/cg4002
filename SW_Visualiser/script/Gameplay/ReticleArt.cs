using UnityEngine;

namespace BridgeRush.Gameplay
{
    // Clean, anti-aliased reticle sprites drawn from code (no assets): the summon marker, the spell area, a plain ring
    // and the screen crosshair. Ground markers are SpriteRenderers laid flat on the floor.
    public static class ReticleArt
    {
        private const int Size = 256;
        private const float Edge = 0.012f;
        // The screen crosshair has a black outline so it stays readable over a bright bubble or a pale wall. The shape is drawn
        // a little smaller to leave room for the outline inside the texture.
        private const float ScreenShapeScale = 0.88f;
        private const int ScreenOutlinePixels = 8;

        private static Sprite summon;
        private static Sprite area;
        private static Sprite ring;
        private static Sprite screen;
        private static Sprite dotted;

        public static Sprite Summon => summon != null ? summon : summon = Build(SummonPixel);
        public static Sprite Area => area != null ? area : area = Build(AreaPixel);
        public static Sprite Ring => ring != null ? ring : ring = Build(RingPixel);
        public static Sprite Dotted => dotted != null ? dotted : dotted = Build(DottedPixel);
        public static Sprite Screen => screen != null ? screen : screen = BuildOutlined(ScreenPixel, ScreenShapeScale, ScreenOutlinePixels);

        public static SpriteRenderer CreateGroundMarker(string objectName, Sprite sprite, Transform parent = null)
        {
            var go = new GameObject(objectName);
            if (parent != null) go.transform.SetParent(parent, false);
            go.transform.rotation = Quaternion.Euler(90f, 0f, 0f);
            var renderer = go.AddComponent<SpriteRenderer>();
            renderer.sprite = sprite;
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;
            return renderer;
        }

        // Turns a ground marker slowly around its centre, so the dots of a dotted ring seem to march.
        public static void Spin(SpriteRenderer marker, float degrees) => marker.transform.Rotate(0f, 0f, degrees, Space.Self);

        // The sprite is one world unit wide, so the scale is the diameter.
        public static void Place(SpriteRenderer marker, Vector3 center, float groundY, float radius, Color color)
        {
            marker.transform.position = new Vector3(center.x, groundY, center.z);
            marker.transform.localScale = new Vector3(radius * 2f, radius * 2f, 1f);
            marker.color = color;
        }

        // Summon marker: crisp ring, four inner ticks, a centre dot and a faint fill.
        private static float SummonPixel(float x, float y, float d)
        {
            float ringBand = Band(d, 0.90f, 1f);
            float ax = Mathf.Abs(x), ay = Mathf.Abs(y);
            float tickX = Band(ax, 0.55f, 0.78f) * Band(ay, 0f, 0.04f, 0.012f);
            float tickY = Band(ay, 0.55f, 0.78f) * Band(ax, 0f, 0.04f, 0.012f);
            float dot = 1f - Smooth(0.06f, 0.06f + Edge, d);
            float fill = (1f - Smooth(0.88f, 0.9f, d)) * 0.16f * (1f - d);
            return Mathf.Max(Mathf.Max(ringBand, Mathf.Max(tickX, tickY)), Mathf.Max(dot, fill));
        }

        // Spell area: a clean outline (no fill) and a small centre dot.
        private static float AreaPixel(float x, float y, float d)
        {
            float ringBand = Band(d, 0.95f, 1f);
            float dot = (1f - Smooth(0.03f, 0.03f + Edge, d)) * 0.7f;
            return Mathf.Max(ringBand, dot);
        }

        // Fireball area: a ring made of 28 round dots.
        private static float DottedPixel(float x, float y, float d)
        {
            const int dots = 28;
            float step = Mathf.PI * 2f / dots;
            float angle = Mathf.Atan2(y, x);
            float nearest = Mathf.Round(angle / step) * step;
            float dx = x - 0.93f * Mathf.Cos(nearest);
            float dy = y - 0.93f * Mathf.Sin(nearest);
            float distance = Mathf.Sqrt(dx * dx + dy * dy);
            return 1f - Smooth(0.045f, 0.045f + Edge, distance);
        }

        private static float RingPixel(float x, float y, float d) => Band(d, 0.94f, 1f);

        // Screen crosshair: four diagonal arcs, four short ticks on the axes and a centre dot.
        private static float ScreenPixel(float x, float y, float d)
        {
            float ax = Mathf.Abs(x), ay = Mathf.Abs(y);
            float arcs = Band(d, 0.66f, 0.76f) * Smooth(0.14f, 0.18f, Mathf.Min(ax, ay));
            float tickX = Band(ax, 0.82f, 1f) * Band(ay, 0f, 0.05f, 0.012f);
            float tickY = Band(ay, 0.82f, 1f) * Band(ax, 0f, 0.05f, 0.012f);
            float dot = 1f - Smooth(0.07f, 0.07f + Edge, d);
            return Mathf.Max(Mathf.Max(arcs, Mathf.Max(tickX, tickY)), dot);
        }

        private static float Smooth(float a, float b, float v) => Mathf.SmoothStep(0f, 1f, Mathf.Clamp01((v - a) / Mathf.Max(0.0001f, b - a)));

        // 1 inside [low, high], falling to 0 over the soft edge on both sides.
        private static float Band(float v, float low, float high, float edge = Edge) =>
            Smooth(low - edge, low, v) * (1f - Smooth(high, high + edge, v));

        // Like Build, but the white shape gets a black outline `outlinePixels` wide (in texture pixels). The sprite is tinted by
        // Image.color, which leaves the black as it is, so the outline shows whatever colour the crosshair is.
        private static Sprite BuildOutlined(System.Func<float, float, float, float> pixel, float shapeScale, int outlinePixels)
        {
            var texture = new Texture2D(Size, Size, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear
            };
            float half = Size * 0.5f;
            var body = new float[Size * Size];
            for (int j = 0; j < Size; j++)
            {
                for (int i = 0; i < Size; i++)
                {
                    float x = (i + 0.5f - half) / half / shapeScale;
                    float y = (j + 0.5f - half) / half / shapeScale;
                    body[j * Size + i] = Mathf.Clamp01(pixel(x, y, Mathf.Sqrt(x * x + y * y)));
                }
            }

            // The outline is the body grown by a disc: each pixel takes the strongest body pixel within reach, fading at the rim.
            var pixels = new Color32[Size * Size];
            for (int j = 0; j < Size; j++)
            {
                for (int i = 0; i < Size; i++)
                {
                    float grown = 0f;
                    for (int dj = -outlinePixels; dj <= outlinePixels && grown < 1f; dj++)
                    {
                        int jj = j + dj;
                        if (jj < 0 || jj >= Size) continue;
                        for (int di = -outlinePixels; di <= outlinePixels; di++)
                        {
                            int ii = i + di;
                            if (ii < 0 || ii >= Size) continue;
                            float reach = outlinePixels + 0.5f - Mathf.Sqrt(di * di + dj * dj);
                            if (reach <= 0f) continue;
                            float value = body[jj * Size + ii] * Mathf.Clamp01(reach);
                            if (value > grown) grown = value;
                        }
                    }

                    float a = body[j * Size + i];
                    float alpha = a + grown * (1f - a);
                    byte white = alpha > 0.0001f ? (byte)Mathf.RoundToInt(255f * a / alpha) : (byte)0;
                    pixels[j * Size + i] = new Color32(white, white, white, (byte)Mathf.RoundToInt(alpha * 255f));
                }
            }
            texture.SetPixels32(pixels);
            texture.Apply();
            return Sprite.Create(texture, new Rect(0f, 0f, Size, Size), new Vector2(0.5f, 0.5f), Size);
        }

        private static Sprite Build(System.Func<float, float, float, float> pixel)
        {
            var texture = new Texture2D(Size, Size, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear
            };
            var pixels = new Color32[Size * Size];
            float half = Size * 0.5f;
            for (int j = 0; j < Size; j++)
            {
                for (int i = 0; i < Size; i++)
                {
                    float x = (i + 0.5f - half) / half;
                    float y = (j + 0.5f - half) / half;
                    float d = Mathf.Sqrt(x * x + y * y);
                    byte alpha = (byte)Mathf.RoundToInt(Mathf.Clamp01(pixel(x, y, d)) * 255f);
                    pixels[j * Size + i] = new Color32(255, 255, 255, alpha);
                }
            }
            texture.SetPixels32(pixels);
            texture.Apply();
            return Sprite.Create(texture, new Rect(0f, 0f, Size, Size), new Vector2(0.5f, 0.5f), Size);
        }
    }
}
