using System.Collections.Generic;
using TMPro;
using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.UI
{
    // Which motion each card shows. The glove bindings are in GestureBindings and match these pictures.
    public static class CardMotions
    {
        public static string For(UnitType type) => type switch
        {
            UnitType.Warrior => "chop",
            UnitType.Rogue => "jab",
            UnitType.Mage => "circle",
            UnitType.Tank => "shield",
            UnitType.Hero => "star",
            _ => "tap"
        };

        public static string ForSpell(string spellKey) => spellKey switch
        {
            "lightning" => "lightning",
            "fireball" => "punch",
            _ => "tap"
        };
    }

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
        private static readonly Dictionary<string, Sprite> gestureIcons = new();

        /// <summary>
        /// A white stroke drawing of a glove gesture (circle, caret, arc, triangle, star, z, infinity) on a transparent
        /// square, to be tinted by Image.color. Any other name draws a plus.
        /// </summary>
        public static Sprite CreateGestureIcon(string gesture, int size = 96)
        {
            string key = (gesture ?? "tap") + size;
            if (gestureIcons.TryGetValue(key, out var cached) && cached != null) return cached;

            var sprite = RenderStrokes(GestureStrokes(gesture), size, 0.045f);
            gestureIcons[key] = sprite;
            return sprite;
        }

        private static Sprite RenderStrokes(Vector2[][] strokes, int size, float halfWidthFraction)
        {
            float halfWidth = size * halfWidthFraction;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                filterMode = FilterMode.Bilinear,
                wrapMode = TextureWrapMode.Clamp
            };
            var pixels = new Color[size * size];
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    var p = new Vector2(x + 0.5f, y + 0.5f) / size;
                    float best = float.MaxValue;
                    foreach (var stroke in strokes)
                        for (int i = 0; i + 1 < stroke.Length; i++)
                            best = Mathf.Min(best, DistanceToSegment(p, stroke[i], stroke[i + 1]));
                    float alpha = Mathf.Clamp01(halfWidth + 0.75f - best * size);
                    pixels[y * size + x] = new Color(1f, 1f, 1f, alpha);
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();

            return Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
        }

        private static readonly Dictionary<string, Sprite> motionIcons = new();
        private static Sprite glowDot;

        /// <summary>
        /// A thin white drawing of a card's motion (lightning, punch, chop, jab, shield raise, circle, star). Every motion has a
        /// small dot where it starts, and the directed ones have an arrowhead where they end. Any other name falls back to the
        /// glove gesture drawing.
        /// </summary>
        public static Sprite CreateMotionIcon(string motion, int size = 128)
        {
            string key = (motion ?? "tap") + size;
            if (motionIcons.TryGetValue(key, out var cached) && cached != null) return cached;

            var spec = MotionSpec(motion);
            var strokes = new List<Vector2[]>(spec.Drawn);
            foreach (var path in spec.Directed) strokes.Add(path);
            foreach (var path in spec.Directed) strokes.AddRange(ArrowHead(path));
            foreach (var path in spec.Trace)
            {
                if (path.Length > 0) strokes.Add(Arc(path[0], 0.014f, 0f, 360f, 12));
            }

            var sprite = RenderStrokes(strokes.ToArray(), size, 0.024f);
            motionIcons[key] = sprite;
            return sprite;
        }

        /// <summary>The path or paths a small dot travels along to show the motion, in a 0..1 square, y up.</summary>
        public static Vector2[][] GetMotionPath(string motion) => MotionSpec(motion).Trace;

        public enum MotionEase { Smooth, Snap, Drive }

        /// <summary>
        /// How the tracer dot moves, in real seconds: the loop length, the time spent travelling, and the shape of the move.
        /// Snap starts fast and settles (a jab), Drive starts slow and finishes fast (a punch). The rest of the loop is a pause.
        /// </summary>
        public static void GetMotionTiming(string motion, out float cycleSeconds, out float travelSeconds, out MotionEase ease)
        {
            switch (motion)
            {
                case "jab": cycleSeconds = 1.8f; travelSeconds = 0.3f; ease = MotionEase.Snap; break;
                case "punch": cycleSeconds = 2.4f; travelSeconds = 1f; ease = MotionEase.Drive; break;
                case "chop": cycleSeconds = 2.2f; travelSeconds = 0.8f; ease = MotionEase.Drive; break;
                case "shield": cycleSeconds = 2.4f; travelSeconds = 1.1f; ease = MotionEase.Smooth; break;
                case "lightning": cycleSeconds = 2.2f; travelSeconds = 0.9f; ease = MotionEase.Snap; break;
                case "circle": cycleSeconds = 2.8f; travelSeconds = 1.8f; ease = MotionEase.Smooth; break;
                default: cycleSeconds = 3.4f; travelSeconds = 2.6f; ease = MotionEase.Smooth; break;
            }
        }

        public static Sprite CreateGlowDot(int size = 32)
        {
            if (glowDot != null) return glowDot;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            float half = size * 0.5f;
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float d = Mathf.Sqrt((x + 0.5f - half) * (x + 0.5f - half) + (y + 0.5f - half) * (y + 0.5f - half)) / half;
                    float a = Mathf.Clamp01(1.25f - d * 1.25f);
                    tex.SetPixel(x, y, new Color(1f, 1f, 1f, a * a));
                }
            }
            tex.Apply();
            glowDot = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
            return glowDot;
        }

        private struct Motion
        {
            public Vector2[][] Drawn;
            public Vector2[][] Directed;
            public Vector2[][] Trace;
        }

        private static readonly Vector2[][] NoPaths = new Vector2[0][];

        // Drawn: strokes with no direction (outlines, speed lines). Directed: strokes that end in an arrowhead.
        // Trace: what the tracer dot follows, and where the start dots go.
        private static Motion MotionSpec(string motion)
        {
            switch (motion)
            {
                case "lightning":
                {
                    var path = new[] { new Vector2(0.68f, 0.9f), new Vector2(0.32f, 0.56f), new Vector2(0.68f, 0.44f), new Vector2(0.32f, 0.1f) };
                    return new Motion { Drawn = NoPaths, Directed = new[] { path }, Trace = new[] { path } };
                }
                case "punch":
                {
                    var path = new[] { new Vector2(0.08f, 0.5f), new Vector2(0.5f, 0.5f) };
                    return new Motion { Drawn = new[] { Arc(new Vector2(0.77f, 0.5f), 0.15f, 0f, 360f, 32) }, Directed = new[] { path }, Trace = new[] { path } };
                }
                case "chop":
                {
                    var path = Bezier(new Vector2(0.24f, 0.88f), new Vector2(0.76f, 0.8f), new Vector2(0.74f, 0.14f), 20);
                    return new Motion { Drawn = NoPaths, Directed = new[] { path }, Trace = new[] { path } };
                }
                case "jab":
                {
                    var path = new[] { new Vector2(0.1f, 0.5f), new Vector2(0.84f, 0.5f) };
                    var speedLines = new[]
                    {
                        new[] { new Vector2(0.14f, 0.7f), new Vector2(0.42f, 0.7f) },
                        new[] { new Vector2(0.14f, 0.3f), new Vector2(0.42f, 0.3f) }
                    };
                    return new Motion { Drawn = speedLines, Directed = new[] { path }, Trace = new[] { path } };
                }
                case "shield":
                {
                    var outline = new List<Vector2> { new Vector2(0.27f, 0.92f), new Vector2(0.73f, 0.92f), new Vector2(0.73f, 0.7f) };
                    outline.AddRange(Bezier(new Vector2(0.73f, 0.7f), new Vector2(0.72f, 0.5f), new Vector2(0.5f, 0.42f), 14));
                    outline.AddRange(Bezier(new Vector2(0.5f, 0.42f), new Vector2(0.28f, 0.5f), new Vector2(0.27f, 0.7f), 14));
                    outline.Add(new Vector2(0.27f, 0.92f));
                    var raise = new[] { new Vector2(0.5f, 0.06f), new Vector2(0.5f, 0.32f) };
                    return new Motion { Drawn = new[] { outline.ToArray() }, Directed = new[] { raise }, Trace = new[] { raise } };
                }
                case "circle":
                {
                    var path = Arc(new Vector2(0.5f, 0.5f), 0.34f, 90f, -215f, 44);
                    return new Motion { Drawn = NoPaths, Directed = new[] { path }, Trace = new[] { path } };
                }
                case "star":
                {
                    // Drawn the usual way: start at the bottom-left point and go up to the top, then down, across and back.
                    var center = new Vector2(0.5f, 0.47f);
                    Vector2 Point(float degrees) => center + new Vector2(Mathf.Cos(degrees * Mathf.Deg2Rad), Mathf.Sin(degrees * Mathf.Deg2Rad)) * 0.42f;
                    var path = new[] { Point(-126f), Point(90f), Point(-54f), Point(162f), Point(18f), Point(-126f) };
                    // The first stroke (bottom-left up to the top) carries an arrowhead on its upper half, so the start and the
                    // direction are part of the star itself: the dot is at the bottom-left, the arrow points up.
                    var arrow = Chevron(path[0], path[1], 0.85f, 0.1f);
                    return new Motion { Drawn = new[] { path, arrow[0], arrow[1] }, Directed = NoPaths, Trace = new[] { path } };
                }
                default:
                {
                    var drawn = GestureStrokes(motion);
                    return new Motion { Drawn = drawn, Directed = NoPaths, Trace = drawn };
                }
            }
        }

        // An arrowhead part-way along a straight stroke, pointing from `from` toward `to`.
        private static Vector2[][] Chevron(Vector2 from, Vector2 to, float along, float length)
        {
            Vector2 point = Vector2.Lerp(from, to, along);
            Vector2 back = (from - to).normalized;
            var left = Quaternion.Euler(0f, 0f, 35f) * back;
            var right = Quaternion.Euler(0f, 0f, -35f) * back;
            return new[]
            {
                new[] { point, point + (Vector2)left * length },
                new[] { point, point + (Vector2)right * length }
            };
        }

        private static Vector2[] Bezier(Vector2 from, Vector2 control, Vector2 to, int steps)
        {
            var points = new Vector2[steps + 1];
            for (int i = 0; i <= steps; i++)
            {
                float t = i / (float)steps;
                points[i] = (1f - t) * (1f - t) * from + 2f * (1f - t) * t * control + t * t * to;
            }
            return points;
        }

        // Two short strokes at the end of a path, turned back from its last direction. Short paths get a smaller head.
        private static Vector2[][] ArrowHead(Vector2[] path)
        {
            var tip = path[path.Length - 1];
            Vector2 back = Vector2.zero;
            float pathLength = 0f;
            for (int i = 1; i < path.Length; i++) pathLength += Vector2.Distance(path[i - 1], path[i]);
            for (int i = path.Length - 2; i >= 0; i--)
            {
                back = path[i] - tip;
                if (back.magnitude > 0.06f) break;
            }
            if (back.sqrMagnitude < 1e-6f) return new Vector2[0][];
            back.Normalize();
            float length = Mathf.Min(0.12f, pathLength * 0.4f);
            var left = Quaternion.Euler(0f, 0f, 32f) * back;
            var right = Quaternion.Euler(0f, 0f, -32f) * back;
            return new[]
            {
                new[] { tip, tip + (Vector2)left * length },
                new[] { tip, tip + (Vector2)right * length }
            };
        }

        // Points in a 0..1 square, y up.
        private static Vector2[][] GestureStrokes(string gesture)
        {
            switch (gesture)
            {
                case "circle": return new[] { Arc(new Vector2(0.5f, 0.5f), 0.34f, 0f, 360f, 48) };
                case "arc": return new[] { Arc(new Vector2(0.5f, 0.24f), 0.42f, 25f, 155f, 32) };
                case "triangle":
                    return new[] { new[] { new Vector2(0.5f, 0.84f), new Vector2(0.84f, 0.22f), new Vector2(0.16f, 0.22f), new Vector2(0.5f, 0.84f) } };
                case "caret":
                    return new[] { new[] { new Vector2(0.14f, 0.24f), new Vector2(0.5f, 0.8f), new Vector2(0.86f, 0.24f) } };
                case "star":
                {
                    var points = new Vector2[6];
                    for (int i = 0; i < points.Length; i++)
                    {
                        float a = (90f + i * 144f) * Mathf.Deg2Rad;
                        points[i] = new Vector2(0.5f, 0.47f) + new Vector2(Mathf.Cos(a), Mathf.Sin(a)) * 0.42f;
                    }
                    return new[] { points };
                }
                case "z":
                    return new[] { new[] { new Vector2(0.2f, 0.8f), new Vector2(0.8f, 0.8f), new Vector2(0.2f, 0.2f), new Vector2(0.8f, 0.2f) } };
                case "infinity":
                {
                    var points = new Vector2[65];
                    for (int i = 0; i < points.Length; i++)
                    {
                        float t = i / 64f * Mathf.PI * 2f;
                        float d = 1f + Mathf.Sin(t) * Mathf.Sin(t);
                        points[i] = new Vector2(0.5f + 0.42f * Mathf.Cos(t) / d, 0.5f + 0.55f * Mathf.Sin(t) * Mathf.Cos(t) / d);
                    }
                    return new[] { points };
                }
                default:
                    return new[]
                    {
                        new[] { new Vector2(0.5f, 0.2f), new Vector2(0.5f, 0.8f) },
                        new[] { new Vector2(0.2f, 0.5f), new Vector2(0.8f, 0.5f) }
                    };
            }
        }

        private static Vector2[] Arc(Vector2 center, float radius, float fromDegrees, float toDegrees, int steps)
        {
            var points = new Vector2[steps + 1];
            for (int i = 0; i <= steps; i++)
            {
                float a = Mathf.Lerp(fromDegrees, toDegrees, i / (float)steps) * Mathf.Deg2Rad;
                points[i] = center + new Vector2(Mathf.Cos(a), Mathf.Sin(a)) * radius;
            }
            return points;
        }

        private static float DistanceToSegment(Vector2 p, Vector2 a, Vector2 b)
        {
            var ab = b - a;
            float t = Mathf.Clamp01(Vector2.Dot(p - a, ab) / Mathf.Max(ab.sqrMagnitude, 1e-6f));
            return Vector2.Distance(p, a + ab * t);
        }

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
        public const float CardWidth = 150f;
        public const float CardHeight = 214f;
        public const float CardSpacing = 10f;
        public const float HandBottomMargin = 36f;

        public const float HandTotalHeight = CardHeight + HandBottomMargin;
    }
}
