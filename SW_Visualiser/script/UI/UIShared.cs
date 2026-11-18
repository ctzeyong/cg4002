using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.EventSystems;
using BridgeRush.Match;

namespace BridgeRush.UI
{
    // Squashes a UI element a little while a finger is on it and springs it back, so every button feels pressed.
    // Add it only to elements nothing else scales (UIFactory.AddPressFeedback).
    public class PressFeedback : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IPointerExitHandler
    {
        private const float PressedScale = 0.93f;
        private float target = 1f;
        private float current = 1f;

        public void OnPointerDown(PointerEventData eventData) => target = PressedScale;
        public void OnPointerUp(PointerEventData eventData) => target = 1f;
        public void OnPointerExit(PointerEventData eventData) => target = 1f;

        private void OnDisable()
        {
            target = current = 1f;
            transform.localScale = Vector3.one;
        }

        private void Update()
        {
            if (Mathf.Approximately(current, target)) return;
            current = Mathf.Lerp(current, target, 1f - Mathf.Exp(-22f * Time.unscaledDeltaTime));
            if (Mathf.Abs(current - target) < 0.001f) current = target;
            transform.localScale = new Vector3(current, current, 1f);
        }
    }

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

        // What the player reads for a motion (the tutorial and the how-to-play page). The keys above stay as they are because they
        // pick the picture and its timing; only the words change: "chop" and "jab" are called by what they look like in play.
        public static string DisplayName(string motion) => motion switch
        {
            "chop" => "Forward chop",
            "jab" => "Forward backstab",
            "circle" => "Draw a circle",
            "shield" => "Raise a shield",
            "star" => "Draw a star",
            "lightning" => "Draw a zigzag",
            "punch" => "Forward punch",
            "shake" => "Shake side to side",
            "scan" => "Hold your aim on it",
            _ => motion ?? string.Empty
        };

        // The full sentence for the tutorial: every gesture is done IN FRONT of you with the glove hand, so each one says so.
        public static string Instruction(string motion) => motion switch
        {
            "chop" => "Swing your hand down and forward, in front of you, like a chop.",
            "jab" => "Thrust your hand straight forward, in front of you, like a backstab.",
            "circle" => "Draw a circle in the air, in front of you.",
            "shield" => "Raise your hand up in front of you, like lifting a shield.",
            "star" => "Draw a star in the air, in front of you.",
            "lightning" => "Draw a zigzag bolt downward in the air, in front of you.",
            "punch" => "Punch straight forward, in front of you.",
            "shake" => "Move the phone left and right, a few quick times.",
            "scan" => "Point the crosshair at the bubble on the enemy tower and hold it there until the ring fills.",
            _ => "Do the motion shown, in front of you."
        };

        public static string ForSpell(string spellKey) => spellKey switch
        {
            "lightning" => "lightning",
            "fireball" => "punch",
            _ => "tap"
        };
    }

    // Applies a text outline once the text object is active and TextMeshPro has its material (see UIFactory.ApplyOutline).
    public class DeferredOutline : MonoBehaviour
    {
        public float Width;
        public Color32 Color;

        private void Start()
        {
            var tmp = GetComponent<TextMeshProUGUI>();
            if (tmp != null && tmp.fontSharedMaterial != null)
            {
                tmp.outlineWidth = Width;
                tmp.outlineColor = Color;
            }
            Destroy(this);
        }
    }

    public static class UIFactory
    {
        // A crown emblem, tinted. The sprite has its own dark outline, so it reads on any background.
        public static RectTransform CreateCrown(Transform parent, string name, float size, Color color)
        {
            var go = CreateRect(name, parent);
            var rect = go.GetComponent<RectTransform>();
            rect.sizeDelta = new Vector2(size, size);
            var image = go.AddComponent<UnityEngine.UI.Image>();
            image.sprite = UITextureFactory.CreateCrownIcon();
            image.color = color;
            image.raycastTarget = false;
            return rect;
        }

        // Sets a text's outline. TextMeshPro has not loaded its font material yet for text created under an INACTIVE parent, and
        // setting the outline then throws (it killed the HUD's startup once), so that case waits until the text is active.
        public static void ApplyOutline(TextMeshProUGUI tmp, float width, Color32 color)
        {
            if (tmp == null) return;
            // Only once the text is active: a text under an inactive parent has the font's material but no instance of it yet, and
            // setting the outline then throws (which disables the component that built the UI).
            if (tmp.isActiveAndEnabled && tmp.fontSharedMaterial != null)
            {
                tmp.outlineWidth = width;
                tmp.outlineColor = color;
                return;
            }
            var deferred = tmp.gameObject.GetComponent<DeferredOutline>();
            if (deferred == null) deferred = tmp.gameObject.AddComponent<DeferredOutline>();
            deferred.Width = width;
            deferred.Color = color;
        }

        public static void AddPressFeedback(GameObject go)
        {
            if (go != null && go.GetComponent<PressFeedback>() == null) go.AddComponent<PressFeedback>();
        }

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

        private const string GameFontResource = "Fonts/LilitaOne-Regular";
        private static TMP_FontAsset gameFont;
        private static bool gameFontTried;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.SubsystemRegistration)]
        private static void ResetGameFont()
        {
            gameFont = null;
            gameFontTried = false;
        }

        // The game's chunky card-game font (Lilita One, SIL OFL). Built once from the TTF in Resources/Fonts, so no
        // Font Asset Creator step is needed. Null if the file is missing or the build fails: text then keeps TMP's default font.
        public static TMP_FontAsset GameFont
        {
            get
            {
                if (gameFont != null || gameFontTried) return gameFont;
                gameFontTried = true;
                try
                {
                    var source = Resources.Load<Font>(GameFontResource);
                    if (source == null) return null;
                    var asset = TMP_FontAsset.CreateFontAsset(source, 90, 9, UnityEngine.TextCore.LowLevel.GlyphRenderMode.SDFAA,
                        1024, 1024, AtlasPopulationMode.Dynamic, true);
                    if (asset == null) return null;
                    asset.name = "LilitaOne SDF (runtime)";
                    if (TMP_Settings.defaultFontAsset != null)
                    {
                        asset.fallbackFontAssetTable ??= new List<TMP_FontAsset>();
                        asset.fallbackFontAssetTable.Add(TMP_Settings.defaultFontAsset);
                    }
                    gameFont = asset;
                }
                catch (System.Exception e)
                {
                    Debug.LogWarning("[Font] Could not build the game font, using the default: " + e.Message);
                }
                return gameFont;
            }
        }

        public static TextMeshProUGUI CreateText(Transform parent, string text, float fontSize, FontStyles style, Color color)
        {
            var go = CreateRect("Text", parent);
            var tmp = go.AddComponent<TextMeshProUGUI>();
            bool headline = fontSize >= 28f && (style & FontStyles.Bold) != 0;
            var font = GameFont;
            if (font != null)
            {
                tmp.font = font;
                style &= ~FontStyles.Bold;
            }
            tmp.text = text;
            tmp.fontSize = fontSize;
            tmp.fontStyle = style;
            tmp.color = color;
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.textWrappingMode = TextWrappingModes.NoWrap;
            tmp.raycastTarget = false;
            // Headline-sized bold text gets a thick dark outline, the way mobile card games do it, so it pops on any background.
            if (headline) ApplyOutline(tmp, 0.17f, new Color32(30, 14, 4, 255));
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
                case "shake": cycleSeconds = 2f; travelSeconds = 0.9f; ease = MotionEase.Smooth; break;
                case "scan": cycleSeconds = 3.2f; travelSeconds = 2.5f; ease = MotionEase.Smooth; break;
                case "lightning": cycleSeconds = 2.2f; travelSeconds = 0.9f; ease = MotionEase.Snap; break;
                case "circle": cycleSeconds = 2.8f; travelSeconds = 1.8f; ease = MotionEase.Smooth; break;
                default: cycleSeconds = 3.4f; travelSeconds = 2.6f; ease = MotionEase.Smooth; break;
            }
        }

        private static Sprite gearIcon;

        /// <summary>A white filled gear (eight teeth, round hole) on a transparent square, to be tinted by Image.color.</summary>
        public static Sprite CreateGearIcon(int size = 128)
        {
            if (gearIcon != null) return gearIcon;
            const int teeth = 8;
            float edge = 1.5f / size; // anti-aliasing band, in fractions of the icon
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var pixels = new Color[size * size];
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float dx = (x + 0.5f) / size - 0.5f, dy = (y + 0.5f) / size - 0.5f;
                    float r = Mathf.Sqrt(dx * dx + dy * dy);
                    float angle = Mathf.Atan2(dy, dx);
                    // Square-ish teeth: the outer radius rises and falls quickly as the angle turns.
                    float tooth = Mathf.Clamp01((Mathf.Cos(angle * teeth) + 0.1f) * 5f);
                    float outer = 0.31f + 0.12f * tooth;
                    float body = Mathf.Clamp01((outer - r) / edge + 0.5f);
                    float hole = Mathf.Clamp01((r - 0.14f) / edge + 0.5f);
                    pixels[y * size + x] = new Color(1f, 1f, 1f, body * hole);
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            gearIcon = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
            return gearIcon;
        }

        /// <summary>
        /// A glossy "game button" panel: a solid dark outline, a vertical gradient with a lighter shine across the top half,
        /// a thin bright line just inside the outline at the top, and a darker lip along the bottom.
        /// </summary>
        public static Sprite CreateChunkyPanel(int size, int cornerRadius, Color topColor, Color bottomColor, Color outlineColor, int outline = 5)
        {
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var half = new Vector2(size * 0.5f, size * 0.5f);
            var pixels = new Color[size * size];
            for (int y = 0; y < size; y++)
            {
                float v = y / (float)(size - 1);
                for (int x = 0; x < size; x++)
                {
                    var p = new Vector2(x + 0.5f - half.x, y + 0.5f - half.y);
                    float dist = RoundedBoxSDF(p, half, cornerRadius);
                    float coverage = Mathf.Clamp01(0.5f - dist / 1.5f);
                    if (coverage <= 0f) { pixels[y * size + x] = Color.clear; continue; }

                    Color fill = Color.Lerp(bottomColor, topColor, v);
                    if (v > 0.5f) fill = Color.Lerp(fill, Color.white, 0.16f * (v - 0.5f) * 2f);          // shine on the top half
                    if (v < 0.14f) fill = Color.Lerp(fill, Color.black, 0.22f * (1f - v / 0.14f));        // darker lip at the bottom
                    float inside = -dist;                                                                  // distance in from the edge
                    if (inside < outline) fill = Color.Lerp(outlineColor, fill, Mathf.Clamp01(inside - outline + 1.5f)); // solid outline
                    else if (inside < outline + 3f && v > 0.55f) fill = Color.Lerp(fill, Color.white, 0.32f); // inner highlight

                    fill.a = coverage;
                    pixels[y * size + x] = fill;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            float border = Mathf.Min(cornerRadius + outline + 3f, size * 0.5f - 1f);
            return Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), 100f, 0, SpriteMeshType.FullRect, new Vector4(border, border, border, border));
        }

        private static Sprite crownIcon;

        /// <summary>A royal crown (three peaks with round jewels on a band): white inside, dark outline, so Image.color tints it.</summary>
        public static Sprite CreateCrownIcon(int size = 128)
        {
            if (crownIcon != null) return crownIcon;
            // Shapes in a 0..1 box, y up.
            var body = new[] { new Vector2(0.10f, 0.30f), new Vector2(0.05f, 0.80f), new Vector2(0.30f, 0.56f), new Vector2(0.50f, 0.90f),
                               new Vector2(0.70f, 0.56f), new Vector2(0.95f, 0.80f), new Vector2(0.90f, 0.30f) };
            var band = new[] { new Vector2(0.10f, 0.12f), new Vector2(0.90f, 0.12f), new Vector2(0.90f, 0.32f), new Vector2(0.10f, 0.32f) };
            var jewels = new[] { new Vector3(0.05f, 0.86f, 0.065f), new Vector3(0.50f, 0.95f, 0.07f), new Vector3(0.95f, 0.86f, 0.065f) };

            const float pad = 0.12f;            // room around the shape for the outline
            float scale = 1f - 2f * pad;
            float outlinePx = size * 0.045f;
            float aa = 1.4f;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var pixels = new Color[size * size];
            var dark = new Color(0.13f, 0.08f, 0.05f);
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    var p = new Vector2(((x + 0.5f) / size - pad) / scale, ((y + 0.5f) / size - pad) / scale);
                    float sd = Mathf.Min(PolygonDistance(p, body), PolygonDistance(p, band));
                    foreach (var j in jewels) sd = Mathf.Min(sd, Vector2.Distance(p, new Vector2(j.x, j.y)) - j.z);
                    sd *= size * scale; // to pixels
                    float fillCoverage = Mathf.Clamp01(0.5f - sd / aa);
                    float outlineCoverage = Mathf.Clamp01(0.5f - (sd - outlinePx) / aa);
                    var c = Color.Lerp(dark, Color.white, fillCoverage);
                    // a lighter band and a fold line so it is not a flat blob
                    if (fillCoverage > 0.5f && p.y < 0.32f) c = Color.Lerp(c, new Color(0.85f, 0.85f, 0.85f), 0.5f);
                    c.a = outlineCoverage;
                    pixels[y * size + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            crownIcon = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
            return crownIcon;
        }

        private static Sprite dropIcon;

        /// <summary>A mana droplet (round body, pointed top) with a baked dark outline, a soft shade toward the bottom and a
        /// highlight, to be tinted by Image.color (use the mana blue).</summary>
        public static Sprite CreateDropIcon(int size = 128)
        {
            if (dropIcon != null) return dropIcon;
            var tip = new[] { new Vector2(0.5f, 0.97f), new Vector2(0.5f - 0.265f, 0.5f), new Vector2(0.5f + 0.265f, 0.5f) };
            var bodyCenter = new Vector2(0.5f, 0.36f);
            const float bodyRadius = 0.3f;
            var shine = new Vector2(0.39f, 0.46f);

            const float pad = 0.1f;
            float scale = 1f - 2f * pad;
            float outlinePx = size * 0.05f;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var pixels = new Color[size * size];
            var dark = new Color(0.1f, 0.08f, 0.1f);
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    var p = new Vector2(((x + 0.5f) / size - pad) / scale, ((y + 0.5f) / size - pad) / scale);
                    float sd = Mathf.Min(PolygonDistance(p, tip), Vector2.Distance(p, bodyCenter) - bodyRadius) * size * scale;
                    float fillCoverage = Mathf.Clamp01(0.5f - sd / 1.4f);
                    float outlineCoverage = Mathf.Clamp01(0.5f - (sd - outlinePx) / 1.4f);
                    float shade = Mathf.Lerp(0.72f, 0.98f, Mathf.Clamp01((p.y - 0.06f) / 0.8f));          // darker toward the bottom
                    float gloss = Mathf.Clamp01(1f - Vector2.Distance(p, shine) / 0.085f);                 // small bright highlight
                    float value = Mathf.Clamp01(shade + gloss * 0.35f);
                    var c = Color.Lerp(dark, new Color(value, value, value), fillCoverage);
                    c.a = outlineCoverage;
                    pixels[y * size + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            dropIcon = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
            return dropIcon;
        }

        // Signed distance to a closed polygon: negative inside, positive outside.
        private static float PolygonDistance(Vector2 p, Vector2[] poly)
        {
            float best = float.MaxValue;
            bool inside = false;
            for (int i = 0, j = poly.Length - 1; i < poly.Length; j = i++)
            {
                best = Mathf.Min(best, DistanceToSegment(p, poly[j], poly[i]));
                if ((poly[i].y > p.y) != (poly[j].y > p.y) &&
                    p.x < (poly[j].x - poly[i].x) * (p.y - poly[i].y) / (poly[j].y - poly[i].y) + poly[i].x) inside = !inside;
            }
            return inside ? -best : best;
        }

        private static Sprite vignette;

        /// <summary>A black radial fade, clear in the middle and darker toward the edges and corners, stretched over the screen
        /// to frame the camera view and make the HUD read better against it.</summary>
        public static Sprite CreateVignette(int size = 128)
        {
            if (vignette != null) return vignette;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            var pixels = new Color[size * size];
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float dx = ((x + 0.5f) / size - 0.5f) * 2f, dy = ((y + 0.5f) / size - 0.5f) * 2f;
                    float r = Mathf.Sqrt(dx * dx + dy * dy); // 1 at the middle of an edge, about 1.41 in a corner
                    float a = Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(0.65f, 1.42f, r)) * 0.55f;
                    pixels[y * size + x] = new Color(0f, 0f, 0f, a);
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            vignette = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), size);
            return vignette;
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
                case "shake":
                {
                    // A phone outline with an arrow out to each side; the dot swings left and right across it.
                    var phone = new[]
                    {
                        new Vector2(0.4f, 0.26f), new Vector2(0.6f, 0.26f), new Vector2(0.6f, 0.74f), new Vector2(0.4f, 0.74f), new Vector2(0.4f, 0.26f)
                    };
                    var speaker = new[] { new Vector2(0.46f, 0.68f), new Vector2(0.54f, 0.68f) };
                    var left = new[] { new Vector2(0.33f, 0.5f), new Vector2(0.1f, 0.5f) };
                    var right = new[] { new Vector2(0.67f, 0.5f), new Vector2(0.9f, 0.5f) };
                    var swing = new[] { new Vector2(0.2f, 0.5f), new Vector2(0.8f, 0.5f), new Vector2(0.2f, 0.5f), new Vector2(0.8f, 0.5f), new Vector2(0.2f, 0.5f) };
                    return new Motion { Drawn = new[] { phone, speaker }, Directed = new[] { left, right }, Trace = new[] { swing } };
                }
                case "scan":
                {
                    // The bubble with a crosshair's four ticks around it; the dot runs round the progress ring, clockwise from the top.
                    var bubble = Arc(new Vector2(0.5f, 0.5f), 0.16f, 0f, 360f, 32);
                    var ticks = new[]
                    {
                        new[] { new Vector2(0.5f, 0.98f), new Vector2(0.5f, 0.86f) },
                        new[] { new Vector2(0.5f, 0.02f), new Vector2(0.5f, 0.14f) },
                        new[] { new Vector2(0.02f, 0.5f), new Vector2(0.14f, 0.5f) },
                        new[] { new Vector2(0.98f, 0.5f), new Vector2(0.86f, 0.5f) }
                    };
                    var ring = Arc(new Vector2(0.5f, 0.5f), 0.34f, 90f, -358f, 48);
                    return new Motion { Drawn = new[] { bubble, ticks[0], ticks[1], ticks[2], ticks[3] }, Directed = NoPaths, Trace = new[] { ring } };
                }
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

        public static readonly Color Navy = new(0.13f, 0.18f, 0.40f);
        public static readonly Color NavyDeep = new(0.06f, 0.08f, 0.2f);
        public static readonly Color Outline = new(0.12f, 0.06f, 0.02f);
        public static readonly Color ButtonBlue = new(0.25f, 0.55f, 0.95f);
        public static readonly Color ButtonBlueDeep = new(0.12f, 0.3f, 0.7f);
        public static readonly Color ButtonGreen = new(0.36f, 0.78f, 0.34f);
        public static readonly Color ButtonGreenDeep = new(0.16f, 0.5f, 0.18f);
        public static readonly Color ButtonGold = new(1f, 0.82f, 0.25f);
        public static readonly Color ButtonGoldDeep = new(0.9f, 0.5f, 0.08f);
        public static readonly Color ButtonRed = new(0.95f, 0.35f, 0.3f);
        public static readonly Color ButtonRedDeep = new(0.65f, 0.14f, 0.12f);

        public static readonly Color Spell = new(0.69f, 0.42f, 0.878f);
        public static readonly Color SpellDeep = new(0.416f, 0.18f, 0.58f);
    }

    public static class HudLayout
    {
        public const float ReferenceWidth = 1170f; // the canvas reference width (portrait phone, 1170 x 2532)
        public const float CardWidth = 150f;
        public const float CardHeight = 276f; // taller cards: a bigger gesture picture, smaller text under it
        public const float CardSpacing = 10f;
        public const float HandBottomMargin = 36f;

        public const float HandTotalHeight = CardHeight + HandBottomMargin;

        // The canvas is a portrait phone (1170 x 2532 reference). On a phone with a notch or a home bar the top and bottom of
        // the screen are not usable; these are those strips in canvas units (0 in the editor and on phones without them).
        public static float SafeTop(Canvas canvas) =>
            canvas == null ? 0f : Mathf.Max(0f, Screen.height - Screen.safeArea.yMax) / Mathf.Max(0.01f, canvas.scaleFactor);

        public static float SafeBottom(Canvas canvas) =>
            canvas == null ? 0f : Mathf.Max(0f, Screen.safeArea.yMin) / Mathf.Max(0.01f, canvas.scaleFactor);

        // Where the card hand's bottom edge sits above the bottom of the canvas.
        public static float HandBottom(Canvas canvas) => HandBottomMargin + SafeBottom(canvas);

        // Where the mana bar sits: just above the cards and their pop-up text.
        public const float ManaBarHeight = 68f;
        public static float ManaBarBottom(Canvas canvas) => HandBottom(canvas) + CardHeight + 114f;

        // Where the two tower-HP bars sit: right above the mana bar.
        public const float HpBarHeight = 58f;
        public static float HpBarsBottom(Canvas canvas) => ManaBarBottom(canvas) + ManaBarHeight + 14f;
    }
}
