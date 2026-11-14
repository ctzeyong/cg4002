using System;
using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;
using BridgeRush.Match;

namespace BridgeRush.UI
{
    /// <summary>
    /// The HOW TO PLAY pages, opened from the settings window. A few pages with a title, a short text and the same moving
    /// motion pictures the cards and the tutorial use. It only explains; the hands-on lesson is the TUTORIAL mode on the
    /// main menu. MatchHudUI creates it on its canvas, so the scene needs no wiring. It does not pause the match.
    ///
    /// The pages say nothing about numbers that live in the scene (regeneration, timer), so they cannot go stale when those
    /// are tuned. The one exception is the unit table, which reads each unit's numbers from the scene's UnitStatsTable
    /// through MatchState when the window is built, so it always shows what the game uses.
    /// </summary>
    public class HowToPlayWindow
    {
        private const float PanelScale = 1.2f;
        private const float PanelWidth = 900f;
        private const float PanelHeight = 700f;
        private const float Pad = 30f;
        private const float TitleHeight = 70f;
        private const float FooterHeight = 84f;
        private const float BodyWidth = PanelWidth - 2f * Pad;
        private const float BodyHeight = PanelHeight - Pad - TitleHeight - FooterHeight;

        private static readonly Color Gold = new Color(1f, 0.85f, 0.4f);

        private static readonly Color ValidColor = new(0.3f, 1f, 0.4f);
        private static readonly Color InvalidColor = new(1f, 0.3f, 0.25f);
        private static readonly Color LightningColor = new(0.35f, 0.72f, 1f);
        private static readonly Color FireColor = new(1f, 0.22f, 0.18f);

        private readonly MatchState matchState;
        private readonly GameObject root;
        private readonly List<GameObject> pages = new();
        private readonly List<string> titles = new();
        private TextMeshProUGUI titleText;
        private TextMeshProUGUI counterText;
        private TextMeshProUGUI nextLabel;
        private GameObject backButton;
        private int page;

        public bool IsOpen => root.activeSelf;

        public HowToPlayWindow(Transform canvasTransform, MatchState matchState = null)
        {
            this.matchState = matchState;
            root = UIFactory.CreateRect("HowToPlayWindow", canvasTransform);
            UIFactory.StretchFull(root.GetComponent<RectTransform>());

            // The dimmed backdrop is a sibling of the panel, so a tap on the panel is not a tap on the backdrop.
            var backdrop = UIFactory.CreateRect("Backdrop", root.transform);
            UIFactory.StretchFull(backdrop.GetComponent<RectTransform>());
            var dim = backdrop.AddComponent<Image>();
            dim.color = new Color(0f, 0f, 0f, 0.7f);
            var dimButton = backdrop.AddComponent<Button>();
            dimButton.targetGraphic = dim;
            dimButton.transition = Selectable.Transition.None;
            dimButton.onClick.AddListener(Close);

            var panel = UIFactory.CreateRect("Panel", root.transform);
            var panelRect = panel.GetComponent<RectTransform>();
            panelRect.sizeDelta = new Vector2(PanelWidth, PanelHeight);
            panelRect.localScale = Vector3.one * PanelScale; // 900 wide fills about 90% of a 1170-wide phone canvas at 1.2
            var panelImage = panel.AddComponent<Image>();
            panelImage.sprite = CardArt.Panel(CardArt.Unit);
            panelImage.type = Image.Type.Sliced;

            titleText = Label(panel.transform, "", 44f, FontStyles.Bold, HudPalette.Gold, TextAlignmentOptions.Center);
            Place(titleText.rectTransform, Pad + 80f, Pad, BodyWidth - 160f, TitleHeight); // centred, clear of the X button

            // The page area. Every page is a child of it, only one is active.
            var body = UIFactory.CreateRect("Body", panel.transform);
            var bodyRect = body.GetComponent<RectTransform>();
            Place(bodyRect, Pad, Pad + TitleHeight, BodyWidth, BodyHeight);

            BuildPages(bodyRect);
            BuildFooter(panel.transform);
            BuildCloseButton(panel.transform);

            root.SetActive(false);
        }

        public void Open()
        {
            root.transform.SetAsLastSibling();
            root.SetActive(true);
            ShowPage(0);
        }

        public void Close() => root.SetActive(false);

        // --- the pages ----------------------------------------------------------------------------------------------

        private void BuildPages(RectTransform body)
        {
            // 1. The goal
            var goal = NewPage(body, "THE GOAL");
            Paragraph(goal, "Destroy the enemy tower before yours falls.", 0f, 0f, BodyWidth, 54f, 36f, FontStyles.Bold, HudPalette.Cream);
            Paragraph(goal, "Summon units on your side of the arena. They cross the bridge, fight whatever they meet and attack the enemy tower.", 0f, 80f, BodyWidth, 120f, 30f);
            Paragraph(goal, "When the clock runs out, the tower with more HP wins.", 0f, 215f, BodyWidth, 50f, 30f);
            Paragraph(goal, "Stay inside the arena. Standing outside it hurts your tower.", 0f, 290f, BodyWidth, 50f, 30f);

            // 2. Lock the tower
            var tower = NewPage(body, "LOCK YOUR TOWER");
            MotionTile(tower, "shake", 0f, 20f, 240f);
            Paragraph(tower, "Move your phone quickly LEFT and RIGHT a few times to LOCK your tower where you stand. A TOWER LOCKED banner shows while it is locked.", 280f, 0f, BodyWidth - 280f, 150f, 29f);
            Paragraph(tower, "Do it again to UNLOCK it. The tower then follows you again.", 280f, 170f, BodyWidth - 280f, 80f, 29f);
            Paragraph(tower, "It does not need to be hard: a quick, small left-right wiggle is enough. Only sideways counts, not up and down.", 280f, 270f, BodyWidth - 280f, 80f, 29f, FontStyles.Italic, Gold);

            // 3. Aim the cursor
            var aim = NewPage(body, "AIM YOUR SUMMONS");
            Paragraph(aim, "The cursor is in the middle of your screen. A unit appears where it points on the floor.", 0f, 0f, BodyWidth, 70f, 27f);
            Paragraph(aim, "You can only summon on YOUR OWN island.", 0f, 72f, BodyWidth, 44f, 31f, FontStyles.Bold, Gold);
            CursorRow(aim, 126f, "GREEN", ValidColor, "Your own side. You can summon here.",
                (ReticleArt.Summon, ValidColor, 0.9f), (ReticleArt.Screen, ValidColor, 0.5f));
            CursorRow(aim, 226f, "RED", InvalidColor, "The enemy's side, or outside the arena. Nothing can be summoned here.",
                (ReticleArt.Summon, InvalidColor, 0.9f), (ReticleArt.Screen, InvalidColor, 0.5f));
            CursorRow(aim, 326f, "CIRCLES", LightningColor, "A blue ring is where Lightning lands, a red dotted one is Fireball. Spells can land anywhere on the island.",
                (ReticleArt.Area, LightningColor, 0.9f), (ReticleArt.Dotted, FireColor, 0.6f));
            Paragraph(aim, "No cursor? You are not looking at the floor, or it is too far away.", 0f, 440f, BodyWidth, 60f, 24f, FontStyles.Italic, Gold);

            // 4. Units, with each one's numbers
            var units = NewPage(body, "SUMMON UNITS");
            Paragraph(units, "Do the motion with your glove, in front of you.", 0f, 0f, BodyWidth, 40f, 26f);
            // Three number columns and a wider one for the speed word.
            float[] colWidth = { 92f, 92f, 92f, 132f };
            float statsX = BodyWidth - (colWidth[0] + colWidth[1] + colWidth[2] + colWidth[3]);
            float[] colX = { statsX, statsX + colWidth[0], statsX + colWidth[0] + colWidth[1], statsX + colWidth[0] + colWidth[1] + colWidth[2] };
            string[] headers = { "MANA", "HP", "DMG/s", "SPEED" };
            for (int c = 0; c < headers.Length; c++)
                Cell(units, headers[c], colX[c], 44f, colWidth[c], 26f, 20f, Gold);
            var types = new[] { UnitType.Warrior, UnitType.Rogue, UnitType.Mage, UnitType.Tank, UnitType.Hero };
            for (int i = 0; i < types.Length; i++)
            {
                float y = 76f + i * 76f;
                string motion = CardMotions.For(types[i]);
                MotionTile(units, motion, 0f, y + 6f, 64f);

                UnitStatsTable.Entry entry = default;
                bool known = matchState != null && matchState.TryGetUnitStats(types[i], out entry);
                string name = types[i].ToString().ToUpperInvariant();
                if (known && entry.AttackRangeMeters > 0f) name += "  <size=65%><color=#9AD8FF>RANGED</color></size>";
                Paragraph(units, name, 80f, y + 4f, statsX - 90f, 36f, 28f, FontStyles.Bold, HudPalette.Cream);
                Paragraph(units, CardMotions.DisplayName(motion).ToUpperInvariant(), 80f, y + 38f, statsX - 90f, 30f, 20f, FontStyles.Bold, Gold);

                Color speedColor = HudPalette.Cream;
                string speed = known ? SpeedWord(entry.SecondsToCrossLane, out speedColor) : "-";
                string[] values = known
                    ? new[] { entry.ManaCost.ToString("0.#"), entry.MaxHP.ToString("0"), entry.AttackDps.ToString("0"), speed }
                    : new[] { "-", "-", "-", "-" };
                for (int c = 0; c < values.Length; c++)
                    Cell(units, values[c], colX[c], y + 16f, colWidth[c], 36f, c == 3 ? 23f : 28f, c == 3 ? speedColor : HudPalette.Cream);

                var line = UIFactory.CreateRect("RowLine", units);
                Place(line.GetComponent<RectTransform>(), 0f, y + 72f, BodyWidth, 2f);
                var lineImage = line.AddComponent<Image>();
                lineImage.color = new Color(1f, 1f, 1f, 0.1f);
                lineImage.raycastTarget = false;
            }
            Paragraph(units, "MANA is the cost. DMG/s is damage a second. SPEED is how fast the unit crosses the arena.", 0f, 462f, BodyWidth, 50f, 21f, FontStyles.Italic, Gold);

            // 5. Spells, with each one's numbers (the same spell spec the cards use)
            var spells = NewPage(body, "CAST SPELLS");
            Paragraph(spells, "Do the motion in front of you. Spells use mana too. Aim anywhere on the island.", 0f, 0f, BodyWidth, 64f, 26f);
            var spec = matchState != null ? matchState.Spells : MatchState.SpellSpec.Default;
            float[] spellCol = { 110f, 110f, 110f };
            float spellStatsX = BodyWidth - (spellCol[0] + spellCol[1] + spellCol[2]);
            float[] spellX = { spellStatsX, spellStatsX + spellCol[0], spellStatsX + spellCol[0] + spellCol[1] };
            string[] spellHeaders = { "MANA", "UNITS", "TOWER" };
            for (int c = 0; c < spellHeaders.Length; c++)
                Cell(spells, spellHeaders[c], spellX[c], 76f, spellCol[c], 26f, 20f, Gold);
            SpellRow(spells, 106f, "LIGHTNING STRIKE", CardMotions.ForSpell("lightning"),
                $"Lands {spec.LightningTelegraphSeconds:0.#} s after you aim. Full damage in the middle, {spec.LightningEdgeDamageFraction * 100f:0}% at the edge.",
                new[] { spec.LightningManaCost.ToString("0.#"), spec.LightningUnitDamage.ToString("0"), spec.LightningTowerDamage.ToString("0") }, spellX, spellCol, spellStatsX);
            SpellRow(spells, 242f, "FIREBALL", CardMotions.ForSpell("fireball"),
                $"Flies to the target, hits an area, then burns the ground: {spec.FireballBurnDps:0} damage a second for {spec.FireballBurnSeconds:0} s.",
                new[] { spec.FireballManaCost.ToString("0.#"), spec.FireballUnitDamage.ToString("0"), spec.FireballTowerDamage.ToString("0") }, spellX, spellCol, spellStatsX);
            Paragraph(spells, "MANA is the cost. UNITS and TOWER are the damage to enemy units and to the enemy tower.", 0f, 384f, BodyWidth, 50f, 21f, FontStyles.Italic, Gold);

            // 6. Mana and bubbles
            var mana = NewPage(body, "MANA AND BUBBLES");
            Paragraph(mana, "Every unit and spell costs mana. The cost is on each card.", 0f, 0f, BodyWidth, 50f, 30f);
            Paragraph(mana, "Mana refills over time, and twice as fast in the last minute.", 0f, 80f, BodyWidth, 50f, 30f);
            Paragraph(mana, "A bubble floats on the enemy tower. Point the centre crosshair at it and hold: the crosshair turns yellow, then red as the ring fills. A full bubble gives you extra mana.", 0f, 160f, BodyWidth, 150f, 28f);
            Paragraph(mana, "The other player is warned while you scan, and the ring drains if you look away.", 0f, 320f, BodyWidth, 80f, 28f);
            Paragraph(mana, "Want to practise? Quit to the main menu and pick TUTORIAL.", 0f, 410f, BodyWidth, 50f, 28f, FontStyles.Italic, Gold);
        }

        private RectTransform NewPage(RectTransform body, string title)
        {
            var go = UIFactory.CreateRect("Page" + (pages.Count + 1), body);
            UIFactory.StretchFull(go.GetComponent<RectTransform>());
            go.SetActive(false);
            pages.Add(go);
            titles.Add(title);
            return (RectTransform)go.transform;
        }

        private void ShowPage(int index)
        {
            page = Mathf.Clamp(index, 0, pages.Count - 1);
            for (int i = 0; i < pages.Count; i++) pages[i].SetActive(i == page);
            titleText.text = titles[page];
            counterText.text = $"{page + 1} / {pages.Count}";
            backButton.SetActive(page > 0);
            nextLabel.text = page == pages.Count - 1 ? "DONE" : "NEXT >";
        }

        private void OnNext()
        {
            if (page >= pages.Count - 1) Close();
            else ShowPage(page + 1);
        }

        // --- pieces -------------------------------------------------------------------------------------------------

        private static void Paragraph(RectTransform page, string text, float x, float y, float width, float height, float size,
            FontStyles style = FontStyles.Normal, Color? color = null)
        {
            var label = Label(page, text, size, style, color ?? HudPalette.Cream, TextAlignmentOptions.TopLeft);
            Place(label.rectTransform, x, y, width, height);
        }

        // A dark tile with a looping motion picture (the card's picture plus the moving dot).
        private static RectTransform MotionTile(RectTransform page, string motion, float x, float y, float size)
        {
            var tileGo = UIFactory.CreateRect("Tile_" + motion, page);
            var tile = tileGo.GetComponent<RectTransform>();
            Place(tile, x, y, size, size);
            var tileImage = tileGo.AddComponent<Image>();
            tileImage.sprite = UITextureFactory.CreateRoundedPanel(64, 18, new Color(0.13f, 0.08f, 0.04f), new Color(0.09f, 0.05f, 0.02f), 2, HudPalette.GoldDark);
            tileImage.type = Image.Type.Sliced;
            tileImage.raycastTarget = false;

            var iconGo = UIFactory.CreateRect("Motion", tile);
            var iconRect = iconGo.GetComponent<RectTransform>();
            iconRect.sizeDelta = new Vector2(size * 0.88f, size * 0.88f);
            var icon = iconGo.AddComponent<Image>();
            icon.sprite = UITextureFactory.CreateMotionIcon(motion);
            icon.preserveAspect = true;
            icon.color = HudPalette.Cream;
            icon.raycastTarget = false;
            MotionTracer.Attach(iconRect, motion, Gold, size / 90f);
            return tile;
        }

        // One spell on the spells page: a motion tile, the name and motion, three number cells, a line row and a note under them.
        private static void SpellRow(RectTransform page, float y, string name, string motion, string note, string[] values, float[] colX, float[] colWidth, float statsX)
        {
            MotionTile(page, motion, 0f, y + 4f, 72f);
            Paragraph(page, name, 88f, y + 2f, statsX - 98f, 36f, 26f, FontStyles.Bold, HudPalette.Cream);
            Paragraph(page, CardMotions.DisplayName(motion).ToUpperInvariant(), 88f, y + 38f, statsX - 98f, 30f, 20f, FontStyles.Bold, Gold);
            for (int c = 0; c < values.Length; c++)
                Cell(page, values[c], colX[c], y + 20f, colWidth[c], 36f, 28f, HudPalette.Cream);
            Paragraph(page, note, 0f, y + 82f, BodyWidth, 50f, 21f, FontStyles.Normal, new Color(0.85f, 0.8f, 0.68f));
            var line = UIFactory.CreateRect("RowLine", page);
            Place(line.GetComponent<RectTransform>(), 0f, y + 130f, BodyWidth, 2f);
            var lineImage = line.AddComponent<Image>();
            lineImage.color = new Color(1f, 1f, 1f, 0.1f);
            lineImage.raycastTarget = false;
        }

        // Speed in words from the seconds a unit takes to cross the arena (shorter is faster), with a colour: fastest green, slow orange.
        private static string SpeedWord(float secondsToCross, out Color color)
        {
            if (secondsToCross <= 7f) { color = new Color(0.45f, 1f, 0.5f); return "FASTEST"; }
            if (secondsToCross <= 10f) { color = new Color(0.75f, 1f, 0.45f); return "FAST"; }
            if (secondsToCross <= 12f) { color = HudPalette.Cream; return "MEDIUM"; }
            color = new Color(1f, 0.65f, 0.3f);
            return "SLOW";
        }

        // A centred one-line text in a fixed box (the unit table's cells and headers).
        private static void Cell(RectTransform page, string text, float x, float y, float width, float height, float size, Color color)
        {
            var label = Label(page, text, size, FontStyles.Bold, color, TextAlignmentOptions.Center);
            Place(label.rectTransform, x, y, width, height);
        }

        // A dark tile showing the cursor pictures (sprite, colour, size as a fraction of the tile), then a coloured name and a line of text.
        private static void CursorRow(RectTransform page, float y, string name, Color nameColor, string text, params (Sprite sprite, Color color, float scale)[] layers)
        {
            const float tile = 84f;
            var tileGo = UIFactory.CreateRect("CursorTile_" + name, page);
            var tileRect = tileGo.GetComponent<RectTransform>();
            Place(tileRect, 0f, y, tile, tile);
            var tileImage = tileGo.AddComponent<Image>();
            tileImage.sprite = UITextureFactory.CreateRoundedPanel(64, 18, new Color(0.13f, 0.08f, 0.04f), new Color(0.09f, 0.05f, 0.02f), 2, HudPalette.GoldDark);
            tileImage.type = Image.Type.Sliced;
            tileImage.raycastTarget = false;
            foreach (var layer in layers)
            {
                var go = UIFactory.CreateRect("Layer", tileRect);
                go.GetComponent<RectTransform>().sizeDelta = new Vector2(tile * layer.scale, tile * layer.scale);
                var image = go.AddComponent<Image>();
                image.sprite = layer.sprite;
                image.color = layer.color;
                image.raycastTarget = false;
            }
            Paragraph(page, name, tile + 18f, y + 2f, BodyWidth - tile - 18f, 36f, 29f, FontStyles.Bold, nameColor);
            Paragraph(page, text, tile + 18f, y + 38f, BodyWidth - tile - 18f, 60f, 23f);
        }

        private void BuildFooter(Transform panel)
        {
            var sprite = CardArt.Button(CardArt.Unit);
            float top = PanelHeight - FooterHeight - 6f;

            backButton = FooterButton(panel, "< BACK", sprite, Pad, top, 200f, () => ShowPage(page - 1), out _);
            FooterButton(panel, "NEXT >", sprite, PanelWidth - Pad - 200f, top, 200f, OnNext, out nextLabel);

            counterText = Label(panel, "", 28f, FontStyles.Bold, Gold, TextAlignmentOptions.Center);
            Place(counterText.rectTransform, PanelWidth / 2f - 80f, top, 160f, 66f);
        }

        private static GameObject FooterButton(Transform panel, string label, Sprite sprite, float x, float y, float width, Action onClick, out TextMeshProUGUI text)
        {
            var go = UIFactory.CreateRect(label + "Button", panel);
            Place(go.GetComponent<RectTransform>(), x, y, width, 66f);
            var image = go.AddComponent<Image>();
            image.sprite = sprite;
            image.type = Image.Type.Sliced;
            var button = go.AddComponent<Button>();
            button.targetGraphic = image;
            button.onClick.AddListener(() => onClick());
            UIFactory.AddPressFeedback(go);
            text = Label(go.transform, label, 28f, FontStyles.Bold, HudPalette.Cream, TextAlignmentOptions.Center);
            UIFactory.StretchFull(text.rectTransform);
            return go;
        }

        private void BuildCloseButton(Transform panel)
        {
            var sprite = CardArt.Button(CardArt.Fireball);
            FooterButton(panel, "X", sprite, PanelWidth - Pad - 64f, Pad + 2f, 64f, Close, out _);
        }

        private static TextMeshProUGUI Label(Transform parent, string text, float size, FontStyles style, Color color, TextAlignmentOptions alignment)
        {
            var label = UIFactory.CreateText(parent, text, size, style, color);
            label.alignment = alignment;
            label.textWrappingMode = TextWrappingModes.Normal;
            label.raycastTarget = false;
            return label;
        }

        // Top-left anchored box inside its parent.
        private static void Place(RectTransform rect, float x, float y, float width, float height)
        {
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0f, 1f);
            rect.anchoredPosition = new Vector2(x, -y);
            rect.sizeDelta = new Vector2(width, height);
        }
    }
}
