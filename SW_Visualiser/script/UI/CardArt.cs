using System.Collections.Generic;
using UnityEngine;

namespace BridgeRush.UI
{
    // Code-drawn art for the cards in the hand (no image files): a near-black card with a gold outer rim and a coloured inner band,
    // a faceted mana gem, a small diamond on the top edge, a wooden name plank and, for the Hero, a pair of gold wings.
    public static class CardArt
    {
        public sealed class Theme
        {
            public string Name;
            public Color Rim, RimDeep;          // the outer metal frame
            public Color Inner, InnerDeep;      // the thin coloured band inside it
            public Color GemLight, GemMid, GemDeep;
            public Color WoodTop, WoodBottom, Trim;
        }

        public static readonly Theme Unit = new Theme
        {
            Name = "unit",
            Rim = HudPalette.Gold, RimDeep = HudPalette.GoldDark,
            Inner = new Color(0.16f, 0.42f, 0.85f), InnerDeep = new Color(0.06f, 0.17f, 0.45f),
            GemLight = new Color(0.66f, 0.88f, 1f), GemMid = new Color(0.17f, 0.5f, 0.9f), GemDeep = new Color(0.05f, 0.2f, 0.55f),
            WoodTop = new Color(0.17f, 0.30f, 0.56f), WoodBottom = new Color(0.07f, 0.13f, 0.30f), Trim = HudPalette.GoldDark
        };

        public static readonly Theme Lightning = new Theme
        {
            Name = "lightning",
            Rim = Color.Lerp(HudPalette.Gold, HudPalette.Spell, 0.55f), RimDeep = Color.Lerp(HudPalette.GoldDark, HudPalette.SpellDeep, 0.6f),
            Inner = new Color(0.62f, 0.34f, 0.85f), InnerDeep = new Color(0.27f, 0.10f, 0.42f),
            GemLight = new Color(0.88f, 0.74f, 1f), GemMid = new Color(0.55f, 0.30f, 0.85f), GemDeep = new Color(0.25f, 0.10f, 0.45f),
            WoodTop = new Color(0.32f, 0.15f, 0.44f), WoodBottom = new Color(0.16f, 0.07f, 0.26f), Trim = new Color(0.55f, 0.36f, 0.6f)
        };

        public static readonly Theme Fireball = new Theme
        {
            Name = "fireball",
            Rim = Color.Lerp(HudPalette.Gold, new Color(0.95f, 0.3f, 0.15f), 0.55f), RimDeep = new Color(0.5f, 0.18f, 0.06f),
            Inner = new Color(0.9f, 0.28f, 0.15f), InnerDeep = new Color(0.45f, 0.10f, 0.05f),
            GemLight = new Color(1f, 0.74f, 0.58f), GemMid = new Color(0.9f, 0.28f, 0.15f), GemDeep = new Color(0.45f, 0.08f, 0.05f),
            WoodTop = new Color(0.52f, 0.15f, 0.09f), WoodBottom = new Color(0.27f, 0.07f, 0.05f), Trim = new Color(0.72f, 0.36f, 0.18f)
        };

        public static readonly Theme Green = new Theme
        {
            Name = "green",
            Rim = HudPalette.Gold, RimDeep = HudPalette.GoldDark,
            Inner = new Color(0.30f, 0.76f, 0.34f), InnerDeep = new Color(0.10f, 0.38f, 0.15f),
            GemLight = new Color(0.72f, 1f, 0.72f), GemMid = new Color(0.25f, 0.75f, 0.3f), GemDeep = new Color(0.08f, 0.35f, 0.12f),
            WoodTop = new Color(0.16f, 0.38f, 0.19f), WoodBottom = new Color(0.07f, 0.19f, 0.09f), Trim = HudPalette.GoldDark
        };

        public static readonly Theme Gold = new Theme
        {
            Name = "gold",
            Rim = HudPalette.Gold, RimDeep = HudPalette.GoldDark,
            Inner = new Color(0.95f, 0.5f, 0.1f), InnerDeep = new Color(0.55f, 0.24f, 0.04f),
            GemLight = new Color(1f, 0.92f, 0.6f), GemMid = new Color(0.95f, 0.65f, 0.15f), GemDeep = new Color(0.55f, 0.28f, 0.05f),
            WoodTop = new Color(0.46f, 0.30f, 0.09f), WoodBottom = new Color(0.22f, 0.12f, 0.04f), Trim = HudPalette.GoldDark
        };

        public static Theme ForSpell(string spellKey) => spellKey == "fireball" ? Fireball : Lightning;

        private static readonly Dictionary<string, Sprite> cache = new();

        private static Texture2D NewTexture(int w, int h) =>
            new Texture2D(w, h, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };

        private static Color Mix(Color a, Color b, float t) => Color.Lerp(a, b, Mathf.Clamp01(t));
        private static Color Metal(Theme t, float v) => Color.Lerp(t.RimDeep, Mix(t.Rim, Color.white, 0.3f), v);
        private static float Cover(float sd) => Mathf.Clamp01(0.5f - sd / 1.2f);
        private static float Band(float x, float a, float b) => Mathf.Clamp01(x - a + 0.5f) * Mathf.Clamp01(b - x + 0.5f);
        private static float Rand(int seed) => Mathf.Repeat(Mathf.Sin(seed * 12.9898f) * 43758.5453f, 1f);

        private static float RoundBox(Vector2 p, Vector2 half, float radius)
        {
            var q = new Vector2(Mathf.Abs(p.x) - half.x + radius, Mathf.Abs(p.y) - half.y + radius);
            return new Vector2(Mathf.Max(q.x, 0f), Mathf.Max(q.y, 0f)).magnitude + Mathf.Min(Mathf.Max(q.x, q.y), 0f) - radius;
        }

        private static float Segment(Vector2 p, Vector2 a, Vector2 b)
        {
            var ab = b - a;
            float t = Mathf.Clamp01(Vector2.Dot(p - a, ab) / Mathf.Max(ab.sqrMagnitude, 1e-6f));
            return Vector2.Distance(p, a + ab * t);
        }

        private static float Polygon(Vector2 p, Vector2[] poly)
        {
            float best = float.MaxValue;
            bool inside = false;
            for (int i = 0, j = poly.Length - 1; i < poly.Length; j = i++)
            {
                best = Mathf.Min(best, Segment(p, poly[j], poly[i]));
                if ((poly[i].y > p.y) != (poly[j].y > p.y) &&
                    p.x < (poly[j].x - poly[i].x) * (p.y - poly[i].y) / (poly[j].y - poly[i].y) + poly[i].x) inside = !inside;
            }
            return inside ? -best : best;
        }

        // The card itself, sliced: a near-black body, a bevelled outer rim, a dark gap, a coloured inner band and a thin dark line,
        // with a short gold flourish and a small stud in each corner.
        public static Sprite Frame(Theme theme)
        {
            string key = "frame_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int S = 128;
            const float R = 14f;
            var tex = NewTexture(S, S);
            var pixels = new Color[S * S];
            var half = new Vector2(S * 0.5f, S * 0.5f);
            var outline = new Color(0.05f, 0.03f, 0.02f);
            var bodyBottom = new Color(0.055f, 0.042f, 0.042f);
            var bodyTop = new Color(0.115f, 0.09f, 0.085f);
            var flourish = Mix(theme.Rim, Color.white, 0.2f);
            for (int y = 0; y < S; y++)
            {
                float v = y / (S - 1f);
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2(x + 0.5f - half.x, y + 0.5f - half.y);
                    float sd = RoundBox(p, half, R);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;

                    var c = Color.Lerp(bodyBottom, bodyTop, v);
                    c = Mix(c, c * 0.55f, 1f - Mathf.Clamp01(inside / 36f));

                    float bevel = inside < 3.4f ? 0.2f : -0.25f;                       // lit outer lip, shaded inner half
                    var metal = Mix(Metal(theme, v), bevel > 0f ? Color.white : theme.RimDeep, Mathf.Abs(bevel));
                    var band = Color.Lerp(theme.InnerDeep, theme.Inner, v);
                    if (inside < 9.2f) band = Mix(band, Color.white, 0.22f);           // lit top edge of the band

                    c = Mix(c, outline, Band(inside, 6.6f, 7.8f) * 0.9f);              // gap between the rim and the band
                    c = Mix(c, band, Band(inside, 7.8f, 12.6f));
                    c = Mix(c, outline, Band(inside, 12.6f, 14.2f) * 0.85f);           // thin dark line inside the band
                    c = Mix(c, metal, Band(inside, 1.6f, 6.6f));
                    c = Mix(c, outline, Band(inside, 0f, 1.6f));

                    float u = Mathf.Min(x + 0.5f, S - x - 0.5f), w = Mathf.Min(y + 0.5f, S - y - 0.5f);
                    float along = (u + w) * 0.5f;
                    float line = Mathf.Abs(u - w) * 0.7071f;
                    if (along > 15f && along < 27f) c = Mix(c, flourish, Cover(line - 0.9f) * 0.9f);
                    c = Mix(c, flourish, Cover(Vector2.Distance(new Vector2(u, w), new Vector2(28f, 28f)) - 2.6f));

                    c.a = coverage;
                    pixels[y * S + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f, 0, SpriteMeshType.FullRect, new Vector4(32, 32, 32, 32));
            cache[key] = sprite;
            return sprite;
        }

        // The picture window: darker than the card, falling to near black at the edges, with a faint lit edge in the band colour.
        public static Sprite Window(Theme theme)
        {
            string key = "window_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int S = 64;
            const float R = 8f;
            var tex = NewTexture(S, S);
            var pixels = new Color[S * S];
            var half = new Vector2(S * 0.5f, S * 0.5f);
            var centre = new Color(0.095f, 0.072f, 0.07f);
            var edge = new Color(0.035f, 0.027f, 0.027f);
            for (int y = 0; y < S; y++)
            {
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2(x + 0.5f - half.x, y + 0.5f - half.y);
                    float sd = RoundBox(p, half, R);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;
                    float r = new Vector2(p.x / half.x, p.y / half.y).magnitude;
                    var c = Color.Lerp(centre, edge, Mathf.Pow(Mathf.Clamp01(r / 1.2f), 1.6f));
                    c = Mix(c, theme.InnerDeep, Band(inside, 0f, 1.8f) * 0.55f);
                    c.a = coverage;
                    pixels[y * S + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f, 0, SpriteMeshType.FullRect, new Vector4(14, 14, 14, 14));
            cache[key] = sprite;
            return sprite;
        }

        // The name plank: dark grained boards with chamfered corners and a small notch at each end, a gold-brown trim and a lit top edge.
        // Not sliceable: made at 4 : 1, the shape it is shown at.
        public static Sprite Plank(Theme theme)
        {
            string key = "plank_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int W = 280, H = 70;
            var tex = NewTexture(W, H);
            var pixels = new Color[W * H];
            float c = H * 0.2f, n = H * 0.17f;
            var poly = new[]
            {
                new Vector2(c, 1.5f), new Vector2(W - c, 1.5f), new Vector2(W - 1.5f, c), new Vector2(W - 1.5f - n, H * 0.5f),
                new Vector2(W - 1.5f, H - c), new Vector2(W - c, H - 1.5f), new Vector2(c, H - 1.5f), new Vector2(1.5f, H - c),
                new Vector2(1.5f + n, H * 0.5f), new Vector2(1.5f, c)
            };
            var outline = new Color(0.04f, 0.03f, 0.04f);
            for (int y = 0; y < H; y++)
            {
                float v = y / (H - 1f);
                for (int x = 0; x < W; x++)
                {
                    float sd = Polygon(new Vector2(x + 0.5f, y + 0.5f), poly);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * W + x] = Color.clear; continue; }
                    float inside = -sd;

                    var col = Color.Lerp(theme.WoodBottom, theme.WoodTop, v);
                    float grain = Rand(Mathf.FloorToInt(x / 11f) + Mathf.FloorToInt(y / 4f) * 131) - 0.5f;
                    col *= 1f + grain * 0.22f + Mathf.Sin(y * 1.35f + Rand(Mathf.FloorToInt(x / 40f)) * 6f) * 0.05f;
                    foreach (float seam in new[] { 0.34f, 0.67f })                   // two seams between the boards
                        col = Mix(col, Color.black, Band(y, H * seam, H * seam + 1f) * 0.4f);
                    if (v > 0.5f) col = Mix(col, Color.white, Band(inside, 3.2f, 4.6f) * 0.2f);
                    col = Mix(col, theme.Trim, Band(inside, 1.5f, 3.2f));
                    col = Mix(col, outline, Band(inside, 0f, 1.5f));
                    col.a = coverage;
                    pixels[y * W + x] = col;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, W, H), new Vector2(0.5f, 0.5f), 100f);
            cache[key] = sprite;
            return sprite;
        }

        // The frame of a status bar (tower HP, mana), sliced: the card's look in a thin strip, a near-black body, a gold rim, a
        // dark gap and a thin coloured band. The fill sits inside it, about 11 units in from the edge.
        public static Sprite BarFrame(Theme theme, bool coloured = false)
        {
            string key = (coloured ? "button_" : "bar_") + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int S = 64;
            const float R = 15f;
            var tex = NewTexture(S, S);
            var pixels = new Color[S * S];
            var half = new Vector2(S * 0.5f, S * 0.5f);
            var outline = new Color(0.05f, 0.03f, 0.02f);
            var bodyBottom = coloured ? theme.WoodBottom : new Color(0.035f, 0.028f, 0.03f);
            var bodyTop = coloured ? theme.WoodTop : new Color(0.085f, 0.068f, 0.066f);
            for (int y = 0; y < S; y++)
            {
                float v = y / (S - 1f);
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2(x + 0.5f - half.x, y + 0.5f - half.y);
                    float sd = RoundBox(p, half, R);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;

                    var c = Color.Lerp(bodyBottom, bodyTop, v);
                    if (coloured) c = Mix(c, Color.white, Mathf.Clamp01(v - 0.55f) * 0.18f);   // a soft shine on the top half of a button
                    var metal = Mix(Metal(theme, v), inside < 2.8f ? Color.white : theme.RimDeep, inside < 2.8f ? 0.2f : 0.25f);
                    var band = Color.Lerp(theme.InnerDeep, theme.Inner, v);
                    if (inside < 7.4f) band = Mix(band, Color.white, 0.2f);

                    c = Mix(c, outline, Band(inside, 5.2f, 6.2f) * 0.9f);
                    c = Mix(c, band, Band(inside, 6.2f, 9.2f));
                    c = Mix(c, outline, Band(inside, 9.2f, 10.6f) * 0.85f);
                    c = Mix(c, metal, Band(inside, 1.5f, 5.2f));
                    c = Mix(c, outline, Band(inside, 0f, 1.5f));
                    c.a = coverage;
                    pixels[y * S + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f, 0, SpriteMeshType.FullRect, new Vector4(22, 22, 22, 22));
            cache[key] = sprite;
            return sprite;
        }

        // A card-framed panel for windows, banners and plaques: the card's near-black body, gold rim and coloured band. Sliced; keep it
        // at least 64 units each way.
        public static Sprite Panel(Theme theme = null) => Frame(theme ?? Unit);

        // A card-framed button: the thin frame of the status bars around a body in the theme's plank colours. Sliced.
        public static Sprite Button(Theme theme = null) => BarFrame(theme ?? Unit, true);

        // A round medallion in the same frame, for the settings button.
        public static Sprite Medallion(Theme theme = null)
        {
            theme ??= Unit;
            string key = "medal_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int S = 128;
            var tex = NewTexture(S, S);
            var pixels = new Color[S * S];
            float half = S * 0.5f;
            var outline = new Color(0.05f, 0.03f, 0.02f);
            for (int y = 0; y < S; y++)
            {
                float v = y / (S - 1f);
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2(x + 0.5f - half, y + 0.5f - half);
                    float sd = p.magnitude - (half - 1f);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;

                    var c = Color.Lerp(theme.WoodBottom, theme.WoodTop, v);
                    c = Mix(c, Color.white, Mathf.Clamp01(v - 0.55f) * 0.18f);
                    var metal = Mix(Metal(theme, v), inside < 4.5f ? Color.white : theme.RimDeep, inside < 4.5f ? 0.2f : 0.25f);
                    var band = Color.Lerp(theme.InnerDeep, theme.Inner, v);
                    if (inside < 11f) band = Mix(band, Color.white, 0.2f);

                    c = Mix(c, outline, Band(inside, 8.4f, 9.8f) * 0.9f);
                    c = Mix(c, band, Band(inside, 9.8f, 14f));
                    c = Mix(c, outline, Band(inside, 14f, 15.6f) * 0.85f);
                    c = Mix(c, metal, Band(inside, 2f, 8.4f));
                    c = Mix(c, outline, Band(inside, 0f, 2f));
                    c.a = coverage;
                    pixels[y * S + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f);
            cache[key] = sprite;
            return sprite;
        }

        // The mana gem: a faceted crystal (eight facets lit from the upper left, a flat lighter table in the middle) with a dark
        // outline and a pale edge. Baked in colour, so leave Image.color white.
        public static Sprite Gem(Theme theme)
        {
            string key = "gem_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int S = 128;
            var tex = NewTexture(S, S);
            var pixels = new Color[S * S];
            var radii = new[] { 0.47f, 0.41f, 0.43f, 0.41f, 0.46f, 0.41f, 0.43f, 0.41f };   // top, then round anticlockwise
            var outer = new Vector2[8];
            var table = new Vector2[8];
            for (int i = 0; i < 8; i++)
            {
                float a = Mathf.PI * 0.5f + i * Mathf.PI * 0.25f;
                outer[i] = new Vector2(0.5f + Mathf.Cos(a) * radii[i], 0.5f + Mathf.Sin(a) * radii[i]);
                table[i] = new Vector2(0.5f + Mathf.Cos(a) * radii[i] * 0.52f, 0.5f + Mathf.Sin(a) * radii[i] * 0.52f);
            }
            var dark = new Color(0.06f, 0.05f, 0.09f);
            float lightAngle = Mathf.PI * 0.75f;
            for (int y = 0; y < S; y++)
            {
                for (int x = 0; x < S; x++)
                {
                    var p = new Vector2((x + 0.5f) / S, (y + 0.5f) / S);
                    float sd = Polygon(p, outer) * S;
                    float coverage = Cover(sd - 0f);
                    if (coverage <= 0f) { pixels[y * S + x] = Color.clear; continue; }
                    float inside = -sd;

                    var d = p - new Vector2(0.5f, 0.5f);
                    float angle = Mathf.Atan2(d.y, d.x);
                    float facet = Mathf.Floor(Mathf.Repeat(angle - Mathf.PI * 0.5f + Mathf.PI * 0.125f, Mathf.PI * 2f) / (Mathf.PI * 0.25f));
                    float facetAngle = Mathf.PI * 0.5f + facet * Mathf.PI * 0.25f;
                    float lit = 0.5f + 0.5f * Mathf.Cos(facetAngle - lightAngle);
                    var col = Color.Lerp(theme.GemDeep, theme.GemLight, Mathf.Pow(lit, 1.15f));
                    col = Mix(col, theme.GemMid, 0.25f);

                    float seamAngle = facetAngle - Mathf.PI * 0.125f;                     // a thin dark line between facets
                    float seam = Mathf.Abs(Mathf.Sin(angle - seamAngle)) * d.magnitude * S;
                    col = Mix(col, dark, Cover(seam - 0.7f) * 0.45f);

                    float sdTable = Polygon(p, table) * S;
                    if (sdTable < 0f)
                    {
                        float tableLit = 0.5f + 0.5f * Vector2.Dot(d.normalized, new Vector2(Mathf.Cos(lightAngle), Mathf.Sin(lightAngle)));
                        var tableCol = Color.Lerp(theme.GemMid, theme.GemLight, 0.35f + 0.4f * tableLit);
                        float spec = Mathf.Clamp01(1f - Vector2.Distance(p, new Vector2(0.4f, 0.62f)) / 0.11f);
                        tableCol = Mix(tableCol, Color.white, spec * 0.7f);
                        col = Mix(col, tableCol, Cover(sdTable + 0.5f));
                        col = Mix(col, dark, Band(-sdTable, 0f, 1f) * 0.35f);
                    }

                    col = Mix(col, Mix(theme.GemLight, Color.white, 0.4f), Band(inside, 1.8f, 3.4f) * 0.55f);   // pale edge
                    col = Mix(col, dark, Band(inside, 0f, 2f));
                    col.a = coverage;
                    pixels[y * S + x] = col;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, S, S), new Vector2(0.5f, 0.5f), 100f);
            cache[key] = sprite;
            return sprite;
        }

        // The small diamond on the top edge of the card: a gold lozenge with a gem in it and a short bar to each side.
        public static Sprite TopOrnament(Theme theme)
        {
            string key = "top_" + theme.Name;
            if (cache.TryGetValue(key, out var cached) && cached != null) return cached;

            const int W = 120, H = 72;
            var tex = NewTexture(W, H);
            var pixels = new Color[W * H];
            var centre = new Vector2(W * 0.5f, H * 0.5f);
            var outerDiamond = Diamond(centre, 29f, 35f);
            var innerDiamond = Diamond(centre, 16f, 21f);
            var dark = new Color(0.05f, 0.03f, 0.02f);
            for (int y = 0; y < H; y++)
            {
                float v = y / (H - 1f);
                for (int x = 0; x < W; x++)
                {
                    var p = new Vector2(x + 0.5f, y + 0.5f);
                    float sdD = Polygon(p, outerDiamond);
                    float sdBar = Mathf.Max(Mathf.Abs(p.x - W * 0.5f) - 54f, Mathf.Abs(p.y - H * 0.5f) - 4f);
                    float sd = Mathf.Min(sdD, sdBar);
                    float coverage = Cover(sd);
                    if (coverage <= 0f) { pixels[y * W + x] = Color.clear; continue; }
                    float inside = -sd;

                    var c = Metal(theme, v);
                    c = Mix(c, Color.white, Band(inside, 2f, 3.4f) * 0.25f);
                    float sdG = Polygon(p, innerDiamond);
                    if (sdG < 0.5f)
                    {
                        var g = Color.Lerp(theme.GemDeep, theme.GemLight, Mathf.Clamp01(0.25f + (p.x < W * 0.5f ? 0.45f : 0f) + (v - 0.4f) * 0.5f));
                        c = Mix(c, g, Cover(sdG));
                        c = Mix(c, dark, Band(-sdG, 0f, 1.2f) * 0.5f);
                    }
                    c = Mix(c, dark, Band(inside, 0f, 1.7f));
                    c.a = coverage;
                    pixels[y * W + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, W, H), new Vector2(0.5f, 0.5f), 100f);
            cache[key] = sprite;
            return sprite;
        }

        private static Vector2[] Diamond(Vector2 c, float halfWidth, float halfHeight) => new[]
        {
            new Vector2(c.x, c.y + halfHeight), new Vector2(c.x - halfWidth, c.y), new Vector2(c.x, c.y - halfHeight), new Vector2(c.x + halfWidth, c.y)
        };

        // A gold wing, drawn for the left side (flip it with a negative x scale for the right). Four feathers fan out from the root
        // at the right-hand bottom corner of the sprite.
        public static Sprite Wing()
        {
            if (cache.TryGetValue("wing", out var cached) && cached != null) return cached;

            const int W = 96, H = 72;
            var tex = NewTexture(W, H);
            var pixels = new Color[W * H];
            var root = new Vector2(86f, 20f);
            var angles = new[] { 168f, 148f, 128f, 108f };
            var lengths = new[] { 72f, 74f, 66f, 50f };
            var dark = new Color(0.07f, 0.04f, 0.02f);
            for (int y = 0; y < H; y++)
            {
                float v = y / (H - 1f);
                for (int x = 0; x < W; x++)
                {
                    var p = new Vector2(x + 0.5f, y + 0.5f);
                    Color c = Color.clear;
                    for (int k = 0; k < angles.Length; k++)
                    {
                        float a = angles[k] * Mathf.Deg2Rad;
                        var tip = root + new Vector2(Mathf.Cos(a), Mathf.Sin(a)) * lengths[k];
                        var ab = tip - root;
                        float t = Mathf.Clamp01(Vector2.Dot(p - root, ab) / ab.sqrMagnitude);
                        float radius = Mathf.Lerp(10.5f, 3.6f, Mathf.Pow(t, 0.7f));
                        float sd = Vector2.Distance(p, root + ab * t) - radius;
                        if (sd >= 0.6f) continue;                                     // later feathers lie on top of earlier ones
                        float coverage = Cover(sd);
                        var fill = Color.Lerp(HudPalette.GoldDark, Mix(HudPalette.Gold, Color.white, 0.35f), Mathf.Clamp01(0.3f + v * 0.9f - t * 0.25f));
                        fill = Mix(fill, dark, Band(-sd, 0f, 1.5f));
                        fill = Mix(fill, Color.white, Band(-sd, 2.2f, 3.4f) * 0.18f);
                        c = Mix(c, fill, 1f);
                        c.a = Mathf.Max(c.a, coverage);
                    }
                    pixels[y * W + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.Apply();
            var sprite = Sprite.Create(tex, new Rect(0, 0, W, H), new Vector2(0.5f, 0.5f), 100f);
            cache["wing"] = sprite;
            return sprite;
        }
    }
}
