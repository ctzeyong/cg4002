using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Tracking;

namespace BridgeRush.UI
{
    public class SetupScreen : MonoBehaviour
    {
        [SerializeField] private Canvas canvas;
        [SerializeField] private MatchSetup matchSetup;

        private GameObject modeRoot;
        private GameObject playerRoot;

        private void Awake()
        {
            if (canvas == null || matchSetup == null)
            {
                Debug.LogError($"{nameof(SetupScreen)}: needs a Canvas and a Match Setup.", this);
                enabled = false;
                return;
            }

            BuildModeSelect();
            BuildPlayerSelect();
        }

        private void Update()
        {
            modeRoot.SetActive(!matchSetup.HasChosenMode);
            playerRoot.SetActive(!matchSetup.HasSelected && matchSetup.HasChosenMode);
        }

        private void BuildModeSelect()
        {
            modeRoot = UIFactory.CreateRect("ModeSelect", canvas.transform);

            const float buttonHeight = 100f;
            const float spacing = 16f;
            float halfStep = (buttonHeight + spacing) * 0.5f;

            AddButton(modeRoot.transform, "VS CPU", new Vector2(0f, halfStep), matchSetup.ChooseVsCpu);
            AddButton(modeRoot.transform, "VS PLAYER", new Vector2(0f, -halfStep), matchSetup.ChooseVsPlayer);
        }

        private void BuildPlayerSelect()
        {
            playerRoot = UIFactory.CreateRect("PlayerSelect", canvas.transform);

            const float buttonHeight = 100f;
            const float spacing = 16f;
            float halfStep = (buttonHeight + spacing) * 0.5f;

            AddButton(playerRoot.transform, "I AM PLAYER 1", new Vector2(0f, halfStep), OnPlayer1Selected);
            AddButton(playerRoot.transform, "I AM PLAYER 2", new Vector2(0f, -halfStep), OnPlayer2Selected);
        }

        private void OnPlayer1Selected()
        {
            matchSetup.SelectPlayer1();
            matchSetup.ActivateCpuOpponent();
        }

        private void OnPlayer2Selected()
        {
            matchSetup.SelectPlayer2();
            matchSetup.ActivateCpuOpponent();
        }

        private static void AddButton(Transform parent, string label, Vector2 anchoredPosition, System.Action onClick)
        {
            var go = UIFactory.CreateRect("ModeButton", parent);
            var rect = go.GetComponent<RectTransform>();
            rect.anchoredPosition = anchoredPosition;
            rect.sizeDelta = new Vector2(420f, 100f);
            BuildButtonVisual(go, label, onClick);
        }

        private static void BuildButtonVisual(GameObject go, string label, System.Action onClick)
        {
            var image = go.AddComponent<Image>();
            image.sprite = UITextureFactory.CreateRoundedPanel(96, 14, HudPalette.Wood, HudPalette.WoodDark, 3, HudPalette.Gold);
            image.type = Image.Type.Sliced;

            var button = go.AddComponent<Button>();
            button.targetGraphic = image;
            button.onClick.AddListener(() => onClick());

            var text = UIFactory.CreateText(go.transform, label, 26, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(text.rectTransform);
        }
    }
}
