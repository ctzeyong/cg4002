using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Networking;

namespace BridgeRush.UI
{
    // Defender's warning: the opponent is locking onto a capture bubble on your tower, so move. Builds itself on a canvas.
    public class BubbleWarningUI : MonoBehaviour
    {
        [SerializeField] private BubbleSync bubbleSync;
        [SerializeField] private Canvas canvas;

        private const float FadeSpeed = 6f;

        private GameObject root;
        private CanvasGroup group;
        private Image border;
        private Image barFill;
        private TextMeshProUGUI title;

        private void Awake()
        {
            if (canvas == null) canvas = FindAnyObjectByType<Canvas>();
            if (bubbleSync == null || canvas == null)
            {
                Debug.LogError($"{nameof(BubbleWarningUI)}: needs a Bubble Sync and a Canvas.", this);
                enabled = false;
                return;
            }
            Build();
        }

        private void Build()
        {
            root = UIFactory.CreateRect("BubbleWarning", canvas.transform);
            var rootRect = (RectTransform)root.transform;
            rootRect.anchorMin = rootRect.anchorMax = new Vector2(0.5f, 1f);
            rootRect.pivot = new Vector2(0.5f, 1f);
            rootRect.anchoredPosition = new Vector2(0f, -170f);
            rootRect.sizeDelta = new Vector2(560f, 110f);

            group = root.AddComponent<CanvasGroup>();
            group.alpha = 0f;
            group.interactable = false;
            group.blocksRaycasts = false;

            border = root.AddComponent<Image>();
            border.sprite = UITextureFactory.CreateRoundedPanel(96, 22, HudPalette.OpponentDeep, new Color(0.3f, 0.07f, 0.05f), 3, HudPalette.Opponent);
            border.raycastTarget = false;

            title = UIFactory.CreateText(root.transform, "ENEMY LOCKING ON", 30f, FontStyles.Bold, HudPalette.Cream);
            var titleRect = title.rectTransform;
            titleRect.anchorMin = new Vector2(0f, 1f);
            titleRect.anchorMax = new Vector2(1f, 1f);
            titleRect.pivot = new Vector2(0.5f, 1f);
            titleRect.anchoredPosition = new Vector2(0f, -12f);
            titleRect.sizeDelta = new Vector2(0f, 40f);

            var hint = UIFactory.CreateText(root.transform, "Move away to break the lock", 20f, FontStyles.Normal, HudPalette.Gold);
            var hintRect = hint.rectTransform;
            hintRect.anchorMin = new Vector2(0f, 1f);
            hintRect.anchorMax = new Vector2(1f, 1f);
            hintRect.pivot = new Vector2(0.5f, 1f);
            hintRect.anchoredPosition = new Vector2(0f, -50f);
            hintRect.sizeDelta = new Vector2(0f, 28f);

            var track = UIFactory.CreateRect("Track", root.transform).AddComponent<Image>();
            track.sprite = UITextureFactory.CreateRoundedPanel(32, 10, new Color(0f, 0f, 0f, 0.55f), new Color(0f, 0f, 0f, 0.55f), 0, Color.clear);
            track.raycastTarget = false;
            var trackRect = track.rectTransform;
            trackRect.anchorMin = new Vector2(0.5f, 0f);
            trackRect.anchorMax = new Vector2(0.5f, 0f);
            trackRect.pivot = new Vector2(0.5f, 0f);
            trackRect.anchoredPosition = new Vector2(0f, 14f);
            trackRect.sizeDelta = new Vector2(480f, 14f);

            barFill = UIFactory.CreateRect("Fill", track.transform).AddComponent<Image>();
            barFill.sprite = UITextureFactory.CreateRoundedPanel(32, 10, Color.white, Color.white, 0, Color.clear);
            barFill.type = Image.Type.Filled;
            barFill.fillMethod = Image.FillMethod.Horizontal;
            barFill.fillOrigin = 0;
            barFill.raycastTarget = false;
            UIFactory.StretchFull(barFill.rectTransform);
        }

        private void Update()
        {
            bool warn = bubbleSync.IncomingTargeted || bubbleSync.IncomingProgress > 0f;
            group.alpha = Mathf.MoveTowards(group.alpha, warn ? 1f : 0f, FadeSpeed * Time.unscaledDeltaTime);
            if (group.alpha <= 0f) return;

            float progress = Mathf.Clamp01(bubbleSync.IncomingProgress);
            barFill.fillAmount = progress;
            barFill.color = Color.Lerp(HudPalette.Gold, HudPalette.Opponent, progress);

            float pulse = 0.5f + 0.5f * Mathf.Sin(Time.unscaledTime * 3f);
            border.color = Color.Lerp(Color.white, new Color(1f, 0.8f, 0.75f), pulse * progress);
        }
    }
}
