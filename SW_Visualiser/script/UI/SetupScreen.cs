using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Tracking;

namespace BridgeRush.UI
{
    // The title swells and settles slowly, so the menu is never quite still.
    public class TitleBreath : MonoBehaviour
    {
        private void Update()
        {
            float s = 1f + 0.022f * Mathf.Sin(Time.unscaledTime * 1.7f);
            transform.localScale = new Vector3(s, s, 1f);
        }
    }

    // The crown above the title bobs gently.
    public class CrownBob : MonoBehaviour
    {
        private RectTransform rect;
        private Vector2 home;

        private void Start()
        {
            rect = (RectTransform)transform;
            home = rect.anchoredPosition;
        }

        private void Update()
        {
            if (rect == null) return;
            rect.anchoredPosition = home + new Vector2(0f, 10f * Mathf.Sin(Time.unscaledTime * 2.1f));
            rect.localRotation = Quaternion.Euler(0f, 0f, 3f * Mathf.Sin(Time.unscaledTime * 1.3f));
        }
    }

    public class SetupScreen : MonoBehaviour
    {
        [SerializeField] private Canvas canvas;
        [SerializeField] private MatchSetup matchSetup;

        private GameObject modeRoot;
        private GameObject playerRoot;

        private const float ButtonWidth = 640f;
        private const float ButtonHeight = 132f;
        private const float PrimaryHeight = 168f;
        private const float ButtonGap = 28f;

        private static readonly Color LabelLit = new(1f, 0.95f, 0.5f);     // gold labels: yellow at the top, orange at the bottom
        private static readonly Color LabelEmber = new(1f, 0.55f, 0.1f);

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

        // Tutorial, VS CPU and VS PLAYER (the bigger sign, the main event) are wooden signs; the player signs have blue and red labels.
        private void BuildModeSelect()
        {
            modeRoot = UIFactory.CreateRect("ModeSelect", canvas.transform);
            UIFactory.StretchFull(modeRoot.GetComponent<RectTransform>());
            BuildBackdrop(modeRoot.transform);
            BuildTitle(modeRoot.transform);

            float cpuY = -20f;
            float tutorialY = cpuY + ButtonHeight + ButtonGap;
            float playerY = cpuY - (ButtonHeight + PrimaryHeight) * 0.5f - ButtonGap;

            AddButton(modeRoot.transform, "TUTORIAL", new Vector2(0f, tutorialY), ButtonHeight, LabelLit, LabelEmber, matchSetup.ChooseTutorial);
            AddButton(modeRoot.transform, "VS CPU", new Vector2(0f, cpuY), ButtonHeight, LabelLit, LabelEmber, matchSetup.ChooseVsCpu);
            AddButton(modeRoot.transform, "VS PLAYER", new Vector2(0f, playerY), PrimaryHeight, LabelLit, LabelEmber, matchSetup.ChooseVsPlayer);
        }

        // Blue for player 1, red for player 2: the same colours as their tower-HP bars.
        private void BuildPlayerSelect()
        {
            playerRoot = UIFactory.CreateRect("PlayerSelect", canvas.transform);
            UIFactory.StretchFull(playerRoot.GetComponent<RectTransform>());
            BuildBackdrop(playerRoot.transform);

            float headingY = 330f;
            var plaque = UIFactory.CreateRect("Plaque", playerRoot.transform);
            var plaqueRect = plaque.GetComponent<RectTransform>();
            plaqueRect.anchoredPosition = new Vector2(0f, headingY);
            plaqueRect.sizeDelta = new Vector2(900f, 150f);
            var plaqueImage = plaque.AddComponent<Image>();
            plaqueImage.sprite = CardArt.Panel(CardArt.Unit);
            plaqueImage.type = Image.Type.Sliced;
            plaqueImage.raycastTarget = false;

            float halfStep = (ButtonHeight + ButtonGap) * 0.5f;
            var heading = UIFactory.CreateText(playerRoot.transform, "CHOOSE YOUR SIDE", 64f, FontStyles.Bold, HudPalette.ButtonGold);
            heading.characterSpacing = 6f;
            var headingRect = heading.rectTransform;
            headingRect.sizeDelta = new Vector2(860f, 100f);
            headingRect.anchoredPosition = new Vector2(0f, headingY);

            AddButton(playerRoot.transform, "I AM PLAYER 1", new Vector2(0f, halfStep), ButtonHeight, new Color(0.80f, 0.93f, 1f), new Color(0.30f, 0.62f, 1f), OnPlayer1Selected);
            AddButton(playerRoot.transform, "I AM PLAYER 2", new Vector2(0f, -halfStep), ButtonHeight, new Color(1f, 0.82f, 0.72f), new Color(1f, 0.36f, 0.30f), OnPlayer2Selected);
            // BACK goes to the mode screen (a tutorial's flags are cleared with it).
            AddButton(playerRoot.transform, "< BACK", new Vector2(0f, -(halfStep + ButtonHeight * 0.5f + ButtonGap + 50f)), 100f,
                new Color(1f, 0.98f, 0.9f), new Color(0.82f, 0.76f, 0.62f), matchSetup.DebugReset, 320f);
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

        // A soft dark veil and vignette so the title and buttons read over the camera feed.
        private static void BuildBackdrop(Transform parent)
        {
            var go = UIFactory.CreateRect("Backdrop", parent);
            UIFactory.StretchFull(go.GetComponent<RectTransform>());
            var image = go.AddComponent<Image>();
            image.sprite = UITextureFactory.CreateVignette();
            image.raycastTarget = false;
            var veil = UIFactory.CreateRect("Veil", parent);
            UIFactory.StretchFull(veil.GetComponent<RectTransform>());
            var veilImage = veil.AddComponent<Image>();
            veilImage.color = new Color(0.04f, 0.05f, 0.12f, 0.42f);
            veilImage.raycastTarget = false;
        }

        // The title sits on a stone plaque with gold edges and rivets, a crown above it. Everything hangs from the TOP of the screen
        // (not the middle), so it is never cut off whatever shape the screen is.
        private void BuildTitle(Transform parent)
        {
            float top = HudLayout.SafeTop(canvas);

            var ribbon = UIFactory.CreateRect("Ribbon", parent);
            var ribbonRect = ribbon.GetComponent<RectTransform>();
            ribbonRect.anchorMin = new Vector2(0f, 1f);
            ribbonRect.anchorMax = new Vector2(1f, 1f);
            ribbonRect.pivot = new Vector2(0.5f, 1f);
            ribbonRect.anchoredPosition = new Vector2(0f, -(top + 170f));
            ribbonRect.sizeDelta = new Vector2(-56f, 330f);   // inset, so its gold rim shows all round
            var ribbonImage = ribbon.AddComponent<Image>();
            ribbonImage.sprite = CardArt.Panel(CardArt.Unit);
            ribbonImage.type = Image.Type.Sliced;
            ribbonImage.raycastTarget = false;

            var crown = UIFactory.CreateCrown(parent, "Crown", 230f, HudPalette.ButtonGold);
            crown.anchorMin = crown.anchorMax = new Vector2(0.5f, 1f);
            crown.anchoredPosition = new Vector2(0f, -(top + 135f));
            crown.gameObject.AddComponent<CrownBob>();

            var title = UIFactory.CreateText(parent, "BRIDGE RUSH", 122f, FontStyles.Bold, Color.white);
            UIFactory.ApplyOutline(title, 0.3f, new Color32(40, 18, 4, 255));
            title.textWrappingMode = TextWrappingModes.NoWrap;
            // Yellow at the top fading to orange at the bottom, so the letters look lit from above.
            var lit = new Color(1f, 0.95f, 0.5f);
            var ember = new Color(1f, 0.55f, 0.1f);
            title.enableVertexGradient = true;
            title.colorGradient = new VertexGradient(lit, lit, ember, ember);
            var titleRect = title.rectTransform;
            titleRect.anchorMin = titleRect.anchorMax = new Vector2(0.5f, 1f);
            titleRect.sizeDelta = new Vector2(1120f, 180f);
            titleRect.anchoredPosition = new Vector2(0f, -(top + 338f));
            title.gameObject.AddComponent<TitleBreath>();

            var tagline = UIFactory.CreateText(parent, "AR  TOWER  DEFENCE", 44f, FontStyles.Bold, HudPalette.Cream);
            tagline.characterSpacing = 12f;
            var taglineRect = tagline.rectTransform;
            taglineRect.anchorMin = taglineRect.anchorMax = new Vector2(0.5f, 1f);
            taglineRect.sizeDelta = new Vector2(900f, 60f);
            taglineRect.anchoredPosition = new Vector2(0f, -(top + 436f));
        }

        // A wooden sign in a steel frame; the label is centred, large and lit from above (yellow to orange, or the colours given).
        private static void AddButton(Transform parent, string label, Vector2 anchoredPosition, float height, Color labelTop, Color labelBottom, System.Action onClick, float width = ButtonWidth)
        {
            var go = UIFactory.CreateRect("ModeButton", parent);
            var rect = go.GetComponent<RectTransform>();
            rect.anchoredPosition = anchoredPosition;
            rect.sizeDelta = new Vector2(width, height);

            var image = go.AddComponent<Image>();
            image.sprite = TitleArt.CreatePlank();
            image.type = Image.Type.Sliced;

            var button = go.AddComponent<Button>();
            button.targetGraphic = image;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() => onClick());

            float fontSize = height > ButtonHeight ? 70f : height < ButtonHeight ? 50f : 58f;
            var text = UIFactory.CreateText(go.transform, label, fontSize, FontStyles.Bold, Color.white);
            text.characterSpacing = 4f;
            UIFactory.ApplyOutline(text, 0.34f, new Color32(30, 12, 2, 255));
            text.enableVertexGradient = true;
            text.colorGradient = new VertexGradient(labelTop, labelTop, labelBottom, labelBottom);
            UIFactory.StretchFull(text.rectTransform);
            UIFactory.AddPressFeedback(go);
        }
    }
}
