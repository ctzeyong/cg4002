using System;
using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;

namespace BridgeRush.UI
{
    /// <summary>
    /// The settings window: Haptics, Music and Sound switches, HOW TO PLAY, the DEBUG menu, Quit to main menu, and Resume.
    /// MatchHudUI creates it on its canvas and opens it from the gear button at the top right of the screen, so it needs no
    /// scene wiring. It does not pause the match.
    /// </summary>
    public class OptionsWindow
    {
        private const float PanelWidth = 640f;
        private const float PanelScale = 1.4f;
        private const float RowHeight = 60f;
        private const float Gap = 10f;
        private const float Pad = 24f;
        private const float TitleHeight = 64f;
        private const string QuitText = "QUIT TO MAIN MENU";
        private const string QuitConfirmText = "TAP AGAIN TO CONFIRM";

        private readonly GameObject root;
        private readonly Func<bool> canQuit;
        private readonly Action onQuit;
        private readonly Action onOpenHowToPlay;
        private readonly Action onOpenDebug;
        private readonly List<Action> refreshers = new();
        private readonly Sprite rowSprite;
        private readonly Sprite pillOn;
        private readonly Sprite pillOff;

        private CanvasGroup quitGroup;
        private TextMeshProUGUI quitLabel;
        private bool quitArmed;

        public bool IsOpen => root.activeSelf;

        /// <param name="canQuit">False while the player is already on the mode-select screen, so there is nothing to quit.</param>
        /// <param name="onQuit">Resets the match back to the mode-select screen. Called after the player confirms.</param>
        /// <param name="onOpenHowToPlay">Opens the HOW TO PLAY pages. This window closes first.</param>
        /// <param name="onOpenDebug">Opens the debug menu. This window closes first.</param>
        public OptionsWindow(Transform canvasTransform, Func<bool> canQuit, Action onQuit, Action onOpenHowToPlay, Action onOpenDebug)
        {
            this.canQuit = canQuit;
            this.onQuit = onQuit;
            this.onOpenHowToPlay = onOpenHowToPlay;
            this.onOpenDebug = onOpenDebug;

            rowSprite = CardArt.Window(CardArt.Unit);
            pillOn = UITextureFactory.CreateRoundedPanel(64, 22, new Color(0.35f, 0.75f, 0.35f), new Color(0.2f, 0.5f, 0.2f), 2, HudPalette.Cream);
            pillOff = UITextureFactory.CreateRoundedPanel(64, 22, new Color(0.45f, 0.42f, 0.4f), new Color(0.28f, 0.26f, 0.25f), 2, HudPalette.GoldDark);

            root = UIFactory.CreateRect("OptionsWindow", canvasTransform);
            UIFactory.StretchFull(root.GetComponent<RectTransform>());

            // The dimmed backdrop is a sibling of the panel, not its parent, so a tap on the panel is not also a tap on the backdrop.
            var backdrop = UIFactory.CreateRect("Backdrop", root.transform);
            UIFactory.StretchFull(backdrop.GetComponent<RectTransform>());
            var dim = backdrop.AddComponent<Image>();
            dim.color = new Color(0f, 0f, 0f, 0.65f);
            var dimButton = backdrop.AddComponent<Button>();
            dimButton.targetGraphic = dim;
            dimButton.transition = Selectable.Transition.None;
            dimButton.onClick.AddListener(Close);

            const int toggleCount = 3;
            const int buttonCount = 3; // HOW TO PLAY, DEBUG MENU, QUIT
            float height = Pad + TitleHeight + Gap + toggleCount * (RowHeight + Gap) + 8f + buttonCount * (RowHeight + Gap) + RowHeight + Pad;

            var panel = UIFactory.CreateRect("Panel", root.transform);
            panel.GetComponent<RectTransform>().sizeDelta = new Vector2(PanelWidth, height);
            panel.transform.localScale = Vector3.one * PanelScale; // 640 units is narrow on a 1170-wide phone canvas, and the text too small
            var panelImage = panel.AddComponent<Image>();
            panelImage.sprite = CardArt.Panel(CardArt.Unit);
            panelImage.type = Image.Type.Sliced;

            float y = Pad;
            var title = UIFactory.CreateText(panel.transform, "SETTINGS", 44f, FontStyles.Bold, HudPalette.Gold);
            Place(title.rectTransform, y, TitleHeight);
            y += TitleHeight + Gap;

            AddToggle(panel.transform, ref y, "Haptics", () => Haptics.Enabled, () => Haptics.Enabled = !Haptics.Enabled);
            AddToggle(panel.transform, ref y, "Music", () => GameMusic.Enabled, () => GameMusic.Enabled = !GameMusic.Enabled);
            AddToggle(panel.transform, ref y, "Sound", () => !GameAudio.Muted, () => GameAudio.Muted = !GameAudio.Muted);
            y += 8f;

            var plainSprite = CardArt.Button(CardArt.Unit);
            AddButton(panel.transform, ref y, "HOW TO PLAY", plainSprite, () => { Close(); this.onOpenHowToPlay?.Invoke(); });
            AddButton(panel.transform, ref y, "DEBUG MENU", plainSprite, () => { Close(); this.onOpenDebug?.Invoke(); });
            AddQuitButton(panel.transform, ref y);
            AddButton(panel.transform, ref y, "RESUME",
                CardArt.Button(CardArt.Green), Close);

            root.SetActive(false);
        }

        public void Open()
        {
            quitArmed = false;
            root.transform.SetAsLastSibling();
            root.SetActive(true);
            Refresh();
        }

        public void Close()
        {
            quitArmed = false;
            root.SetActive(false);
        }

        public void Toggle()
        {
            if (IsOpen) Close();
            else Open();
        }

        // Called every frame while open so the switches follow changes made elsewhere.
        public void Refresh()
        {
            foreach (var refresh in refreshers) refresh();

            bool allowed = canQuit();
            quitGroup.interactable = allowed;
            quitGroup.alpha = allowed ? 1f : 0.4f;
            if (!allowed) quitArmed = false;
            quitLabel.text = quitArmed ? QuitConfirmText : QuitText;
        }

        private static void Place(RectTransform rect, float top, float height)
        {
            rect.anchorMin = new Vector2(0.5f, 1f);
            rect.anchorMax = new Vector2(0.5f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, -top);
            rect.sizeDelta = new Vector2(PanelWidth - 2f * Pad, height);
        }

        private void AddToggle(Transform panel, ref float y, string label, Func<bool> isOn, Action toggle)
        {
            var row = UIFactory.CreateRect(label + "Row", panel);
            Place(row.GetComponent<RectTransform>(), y, RowHeight);
            y += RowHeight + Gap;

            var bg = row.AddComponent<Image>();
            bg.sprite = rowSprite;
            bg.type = Image.Type.Sliced;
            var button = row.AddComponent<Button>();
            button.targetGraphic = bg;
            button.onClick.AddListener(() =>
            {
                toggle();
                Refresh();
            });
            UIFactory.AddPressFeedback(row);

            var name = UIFactory.CreateText(row.transform, label, 30f, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(name.rectTransform);
            name.alignment = TextAlignmentOptions.Left;
            name.margin = new Vector4(28f, 0f, 0f, 0f);

            var pill = UIFactory.CreateRect("Switch", row.transform);
            var pillRect = pill.GetComponent<RectTransform>();
            pillRect.anchorMin = new Vector2(1f, 0.5f);
            pillRect.anchorMax = new Vector2(1f, 0.5f);
            pillRect.pivot = new Vector2(1f, 0.5f);
            pillRect.anchoredPosition = new Vector2(-16f, 0f);
            pillRect.sizeDelta = new Vector2(130f, 44f);
            var pillImage = pill.AddComponent<Image>();
            pillImage.type = Image.Type.Sliced;
            pillImage.raycastTarget = false;
            var state = UIFactory.CreateText(pill.transform, "OFF", 26f, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(state.rectTransform);

            refreshers.Add(() =>
            {
                bool on = isOn();
                pillImage.sprite = on ? pillOn : pillOff;
                state.text = on ? "ON" : "OFF";
            });
        }

        private void AddQuitButton(Transform panel, ref float y)
        {
            var sprite = CardArt.Button(CardArt.Fireball);
            var go = AddButton(panel, ref y, QuitText, sprite, () =>
            {
                if (!canQuit()) return;
                if (!quitArmed)
                {
                    quitArmed = true;
                    Refresh();
                    return;
                }
                Close();
                onQuit();
            }, out quitLabel);
            quitGroup = go.AddComponent<CanvasGroup>();
        }

        private static GameObject AddButton(Transform panel, ref float y, string label, Sprite sprite, Action onClick)
            => AddButton(panel, ref y, label, sprite, onClick, out _);

        private static GameObject AddButton(Transform panel, ref float y, string label, Sprite sprite, Action onClick, out TextMeshProUGUI text)
        {
            var go = UIFactory.CreateRect(label + "Button", panel);
            Place(go.GetComponent<RectTransform>(), y, RowHeight);
            y += RowHeight + Gap;

            var image = go.AddComponent<Image>();
            image.sprite = sprite;
            image.type = Image.Type.Sliced;
            var button = go.AddComponent<Button>();
            button.targetGraphic = image;
            button.onClick.AddListener(() => onClick());
            UIFactory.AddPressFeedback(go);

            text = UIFactory.CreateText(go.transform, label, 28f, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(text.rectTransform);
            return go;
        }
    }
}
