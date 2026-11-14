using UnityEngine;

namespace BridgeRush.UI
{
    // Code-drawn art for the menu buttons (no image files): the wooden sign.
    public static class TitleArt
    {
        private static Sprite plank;

        private static float Rand(int seed) => Mathf.Repeat(Mathf.Sin(seed * 12.9898f) * 43758.5453f, 1f);
        private static Color Mix(Color a, Color b, float t) => Color.Lerp(a, b, Mathf.Clamp01(t));
        private static float Cover(float sd) => Mathf.Clamp01(0.5f - sd / 1.2f);
        private static float Band(float x, float a, float b) => Mathf.Clamp01(x - a + 0.5f) * Mathf.Clamp01(b - x + 0.5f);

        private static float RoundBox(Vector2 p, Vector2 half, float radius)
        {
            var q = new Vector2(Mathf.Abs(p.x) - half.x + radius, Mathf.Abs(p.y) - half.y + radius);
            return new Vector2(Mathf.Max(q.x, 0f), Mathf.Max(q.y, 0f)).magnitude + Mathf.Min(Mathf.Max(q.x, q.y), 0f) - radius;
        }

        // A wooden sign: dark grained planks in a thick steel frame, bevelled (lit along the top, shadowed along the bottom), with a
        // gold rivet in each corner. Sliced, so it fits any size.
        public static Sprite CreatePlank()
        {
            if (plank != null) return plank;
            const int S = 128, Border = 36;
            const float R = 17f;
            var tex = new Texture2D(S, S, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var pixels = new Color[S * S];
            var half = new Vector2(S * 0.5f, S * 0.5f);
            var outline = new Color(0.06f, 0.04f, 0.04f);
            var ironLow = new Color(0.16f, 0.18f, 0.24f);
            var ironHigh = new Color(0.50f, 0.54f, 0.63f);
            var woodLow = new Color(0.27f, 0.15f, 0.07f);
            var woodHigh = new Color(0.55f, 0.33f, 0.15f);
            var goldLow = new Color(0.62f, 0.40f, 0.08f);
            var goldHigh = new Color(1f, 0.86f, 0.4f);
            var rivets = new[] { new Vector2(25f, 25f), new Vector2(S - 25f, 25f), new Vector2(25f, S - 25f), new Vector2(S - 25f, S - 25f) };

            for (int y = 0; y < S; y++)
            {
                float v = y / (S - 1f);
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2(x + 0.5f, y + 0.5f);
                    float sd = RoundBox(p - half, half, R);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;

                    // the planks: lighter toward the top, fine horizontal grain, a shadow just under the frame
                    var wood = Color.Lerp(woodLow, woodHigh, 0.3f + 0.5f * v);
                    float grain = Rand(y * 7 + 3) - 0.5f;
                    wood *= 1f + grain * 0.2f + Mathf.Sin(y * 0.9f + Rand(Mathf.FloorToInt(x / 24f)) * 6f) * 0.045f;
                    wood = Mix(wood, Color.black, Band(inside, 10.5f, 15f) * 0.32f);

                    // the frame: steel, bright on its top edge and dark on its bottom edge
                    var iron = Color.Lerp(ironLow, ironHigh, Mathf.Clamp01(v * 1.1f - 0.05f));
                    if (inside < 4f) iron = Mix(iron, Color.white, v > 0.5f ? 0.22f : 0f);
                    else iron = Mix(iron, Color.black, 0.18f);

                    var c = wood;
                    c = Mix(c, outline * 1.4f, Band(inside, 9f, 10.8f));
                    c = Mix(c, iron, Band(inside, 2f, 9f));
                    c = Mix(c, outline, Band(inside, 0f, 2f));

                    foreach (var r in rivets)
                    {
                        float d = Vector2.Distance(p, r);
                        float lit = Mathf.Clamp01(0.5f + ((r.x - p.x) + (p.y - r.y)) / 11f);
                        var gold = Color.Lerp(goldLow, goldHigh, lit);
                        c = Mix(c, outline, Cover(d - 7.2f));
                        c = Mix(c, gold, Cover(d - 5.6f));
                    }

                    c.a = coverage;
                    pixels[y * S + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            plank = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f, 0, SpriteMeshType.FullRect, new Vector4(Border, Border, Border, Border));
            return plank;
        }
    }
}
