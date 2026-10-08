using TMPro;
using UnityEngine;

namespace BridgeRush.UI
{
    public static class UIFactory
    {
        public static GameObject CreateRect(string name, Transform parent)
        {
            var go = new GameObject(name, typeof(RectTransform));
            var rect = (RectTransform)go.transform;
            rect.SetParent(parent, false);
            rect.anchorMin = new Vector2(0.5f, 0.5f);
            rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = Vector2.zero;
            rect.sizeDelta = Vector2.zero;
            return go;
        }

        public static TextMeshProUGUI CreateText(Transform parent, string text, float fontSize, FontStyles style, Color color)
        {
            var go = CreateRect("Text", parent);
            var tmp = go.AddComponent<TextMeshProUGUI>();
            tmp.text = text;
            tmp.fontSize = fontSize;
            tmp.fontStyle = style;
            tmp.color = color;
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.textWrappingMode = TextWrappingModes.NoWrap;
            tmp.raycastTarget = false;
            return tmp;
        }

        public static void StretchFull(RectTransform rect)
        {
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;
        }
    }

    public static class UITextureFactory
    {
        public static Sprite CreateRoundedPanel(
            int size, int cornerRadius, Color topColor, Color bottomColor,
            int borderThickness, Color borderColor)
        {
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                filterMode = FilterMode.Bilinear,
                wrapMode = TextureWrapMode.Clamp
            };

            Vector2 half = new(size * 0.5f, size * 0.5f);
            var pixels = new Color[size * size];

            for (int y = 0; y < size; y++)
            {
                float v = y / (float)(size - 1);
                Color rowColor = Color.Lerp(bottomColor, topColor, v);

                for (int x = 0; x < size; x++)
                {
                    Vector2 p = new(x + 0.5f - half.x, y + 0.5f - half.y);
                    float dist = RoundedBoxSDF(p, half, cornerRadius);

                    float coverage = Mathf.Clamp01(0.5f - dist / 1.5f);
                    if (coverage <= 0f)
                    {
                        pixels[y * size + x] = Color.clear;
                        continue;
                    }

                    Color fill = rowColor;
                    if (borderThickness > 0 && dist > -borderThickness)
                    {
                        float borderT = Mathf.Clamp01((dist + borderThickness) / borderThickness);
                        fill = Color.Lerp(fill, borderColor, borderT);
                    }

                    fill.a *= coverage;
                    pixels[y * size + x] = fill;
                }
            }

            tex.SetPixels(pixels);
            tex.Apply();

            float border = Mathf.Min(cornerRadius + borderThickness + 2f, size * 0.5f - 1f);
            return Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), 100f,
                0, SpriteMeshType.FullRect, new Vector4(border, border, border, border));
        }

        public static Sprite CreateGlow(int size, int cornerRadius, int glowMargin)
        {
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                filterMode = FilterMode.Bilinear,
                wrapMode = TextureWrapMode.Clamp
            };

            Vector2 half = new(size * 0.5f - glowMargin, size * 0.5f - glowMargin);
            var pixels = new Color[size * size];

            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    Vector2 p = new(x + 0.5f - size * 0.5f, y + 0.5f - size * 0.5f);
                    float dist = RoundedBoxSDF(p, half, cornerRadius);

                    float alpha = dist <= 0f
                        ? 1f
                        : Mathf.Clamp01(1f - dist / glowMargin);
                    pixels[y * size + x] = new Color(1f, 1f, 1f, alpha * alpha);
                }
            }

            tex.SetPixels(pixels);
            tex.Apply();
            return Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f));
        }

        private static float RoundedBoxSDF(Vector2 p, Vector2 halfSize, float radius)
        {
            Vector2 q = new(Mathf.Abs(p.x) - halfSize.x + radius, Mathf.Abs(p.y) - halfSize.y + radius);
            float outside = new Vector2(Mathf.Max(q.x, 0f), Mathf.Max(q.y, 0f)).magnitude;
            float inside = Mathf.Min(Mathf.Max(q.x, q.y), 0f);
            return outside + inside - radius;
        }
    }

    public static class HudPalette
    {
        public static readonly Color Wood = new(0.353f, 0.227f, 0.125f);
        public static readonly Color WoodDark = new(0.227f, 0.141f, 0.071f);
        public static readonly Color Gold = new(0.949f, 0.741f, 0.271f);
        public static readonly Color GoldDark = new(0.663f, 0.443f, 0.106f);
        public static readonly Color Cream = new(1f, 0.965f, 0.875f);

        public static readonly Color Own = new(0.227f, 0.627f, 0.941f);
        public static readonly Color OwnDeep = new(0.11f, 0.361f, 0.561f);
        public static readonly Color Opponent = new(0.937f, 0.294f, 0.227f);
        public static readonly Color OpponentDeep = new(0.561f, 0.141f, 0.098f);

        public static readonly Color Mana = new(0.31f, 0.816f, 0.91f);
        public static readonly Color ManaDeep = new(0.102f, 0.486f, 0.561f);

        public static readonly Color Spell = new(0.69f, 0.42f, 0.878f);
        public static readonly Color SpellDeep = new(0.416f, 0.18f, 0.58f);
    }

    public static class HudLayout
    {
        public const float CardWidth = 132f;
        public const float CardHeight = 188f;
        public const float CardSpacing = 12f;
        public const float HandBottomMargin = 36f;

        public const float GesturePadSize = 190f;

        public const float HandTotalHeight = CardHeight + HandBottomMargin;
    }
}
