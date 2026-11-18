using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Match;
using BridgeRush.Gameplay;
using BridgeRush.Tutorial;
using BridgeRush.UI;

namespace BridgeRush.Input
{
    // Runs gestures from the glove (GloveGestureReceiver, through GestureBindings) and shows a short result banner for every
    // summon or spell attempt, whether it came from a glove gesture, a debug button or a card tap. The banner sits under the
    // top bar: a big green "WARRIOR SUMMONED", or a red reason ("BAD AIM") with one line on how to fix it.
    public class GesturePad : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private SpellCaster spellCaster;
        [SerializeField] private Canvas canvas;
        [SerializeField] private Color acceptedColor = Color.green;
        [SerializeField] private Color rejectedColor = Color.red;

        private const float TopOffset = 310f; // the tutorial coach's top; the toast never sits higher than the coach's bottom edge
        private const float ToastTop = 600f; // normal resting height, well below the HP bars and nearer the player's eye line (the canvas is 1170 x 2532, so the crosshair at screen centre is at about 1266)
        private const float ToastWidth = 900f;
        private const float ToastHeight = 110f;
        private const float ToastHeightWithDetail = 156f;
        private const float PopSeconds = 0.15f;
        private const float FadeSeconds = 0.35f;
        // The result banner stays fully visible for HoldSeconds, then fades out over FadeSeconds. A constant, not a serialized
        // field, so an old value saved in the scene (it held 3) cannot override it.
        private const float HoldSeconds = 1.5f;
        private const float resultDisplaySeconds = HoldSeconds + FadeSeconds;

        private RectTransform toastRect;
        private CanvasGroup toastGroup;
        private Image toastBackground;
        private TextMeshProUGUI toastTitle;
        private TextMeshProUGUI toastDetail;
        private float shownAt = float.NegativeInfinity;

        private void Awake()
        {
            if (matchState == null || spellCaster == null || canvas == null)
            {
                Debug.LogError($"{nameof(GesturePad)}: needs a Canvas, Match State and Spell Caster.", this);
                enabled = false;
                return;
            }

            BuildToast();

            matchState.OwnUnitSummoned += OnUnitSummoned;
            matchState.OwnUnitRejected += OnUnitRejected;
            spellCaster.SpellCast += OnSpellCast;
            spellCaster.SpellRejected += OnSpellRejected;
        }

        private void OnDestroy()
        {
            if (matchState != null)
            {
                matchState.OwnUnitSummoned -= OnUnitSummoned;
                matchState.OwnUnitRejected -= OnUnitRejected;
            }
            if (spellCaster != null)
            {
                spellCaster.SpellCast -= OnSpellCast;
                spellCaster.SpellRejected -= OnSpellRejected;
            }
        }

        private void BuildToast()
        {
            var go = UIFactory.CreateRect("ResultToast", canvas.transform);
            toastRect = go.GetComponent<RectTransform>();
            toastRect.anchorMin = new Vector2(0.5f, 1f);
            toastRect.anchorMax = new Vector2(0.5f, 1f);
            toastRect.pivot = new Vector2(0.5f, 1f);
            toastRect.sizeDelta = new Vector2(ToastWidth, ToastHeight);

            toastGroup = go.AddComponent<CanvasGroup>();
            toastGroup.alpha = 0f;
            toastGroup.blocksRaycasts = false;
            toastGroup.interactable = false;

            toastBackground = go.AddComponent<Image>();
            toastBackground.sprite = CardArt.Panel(CardArt.Unit);
            toastBackground.type = Image.Type.Sliced;
            toastBackground.raycastTarget = false;

            toastTitle = UIFactory.CreateText(go.transform, string.Empty, 56f, FontStyles.Bold, HudPalette.Cream);
            toastDetail = UIFactory.CreateText(go.transform, string.Empty, 38f, FontStyles.Normal, HudPalette.Cream);
        }

        private void OnUnitSummoned(UnitType type) => ShowFeedback($"{type.ToString().ToUpperInvariant()} SUMMONED", string.Empty, true);

        private void OnUnitRejected(UnitType type, string reason) => ShowFeedback(Title(reason), Hint(reason), false);

        private void OnSpellCast(string key) => ShowFeedback($"{SpellName(key)} CAST", string.Empty, true);

        private void OnSpellRejected(string key, string reason) => ShowFeedback(Title(reason), Hint(reason, spell: true), false);

        private static string SpellName(string key) => key == "lightning" ? "LIGHTNING" : "FIREBALL";

        private static string Title(string reason) => string.IsNullOrEmpty(reason) ? "FAILED" : reason.ToUpperInvariant();

        // One line on how to fix the usual refusals. Empty when there is nothing useful to add.
        private static string Hint(string reason, bool spell = false)
        {
            string r = (reason ?? string.Empty).ToUpperInvariant();
            if (r.Contains("AIM")) return spell ? "Point the crosshair at the ground on the island" : "Point the crosshair at the ground on your own platform";
            if (r.Contains("ARENA")) return "Walk back into the arena";
            if (r.Contains("MANA")) return "Wait for your mana to refill";
            if (r.Contains("OPPONENT")) return "Look for the other player's tag";
            if (r.Contains("START")) return "Press START GAME first";
            if (r.Contains("WRONG")) return "Do the motion shown above";
            if (r.Contains("WAIT")) return "Get ready for the next step";
            return string.Empty;
        }

        private void Update()
        {
            // Low in the player's view, or just under the tutorial coach while it is showing (whichever is lower).
            var coach = TutorialCoach.Instance;
            float coachBottom = TopOffset + (coach != null ? coach.PanelHeight : 0f);
            toastRect.anchoredPosition = new Vector2(0f, -(Mathf.Max(ToastTop, coachBottom) + HudLayout.SafeTop(canvas)));

            float age = Time.unscaledTime - shownAt;
            if (age >= resultDisplaySeconds)
            {
                toastGroup.alpha = 0f;
                return;
            }

            float pop = Mathf.Clamp01(age / PopSeconds);
            float scale = Mathf.Lerp(0.8f, 1f, 1f - (1f - pop) * (1f - pop));
            toastRect.localScale = new Vector3(scale, scale, 1f);
            toastGroup.alpha = Mathf.Min(Mathf.Clamp01(age / 0.1f), Mathf.Clamp01((resultDisplaySeconds - age) / FadeSeconds));
        }

        public bool PerformGesture(GestureSymbol symbol, string source) => Run(GestureBindings.FromSymbol(symbol));

        // A gesture by name (warrior, rogue, mage, tank, hero, lightning, fireball), e.g. from the debug menu.
        public bool PerformGloveGesture(string gestureName, string source) => Run(GestureBindings.FromGlove(gestureName));

        // The banner itself is shown by the summon and spell events above.
        private bool Run(GameAction action)
        {
            if (action == GameAction.None)
            {
                ShowFeedback("INVALID GESTURE", "No unit or spell is bound to it", false);
                return false;
            }
            return GestureBindings.Perform(action, matchState, spellCaster);
        }

        private void ShowFeedback(string title, string detail, bool accepted)
        {
            var color = accepted ? acceptedColor : rejectedColor;
            bool hasDetail = !string.IsNullOrEmpty(detail);

            toastBackground.color = Color.Lerp(Color.white, color, 0.4f);
            toastTitle.text = title;
            toastTitle.color = accepted ? new Color(0.78f, 1f, 0.78f) : new Color(1f, 0.72f, 0.66f);
            toastDetail.text = detail;

            toastRect.sizeDelta = new Vector2(ToastWidth, hasDetail ? ToastHeightWithDetail : ToastHeight);
            var titleRect = toastTitle.rectTransform;
            titleRect.anchorMin = new Vector2(0f, hasDetail ? 0.42f : 0f);
            titleRect.anchorMax = Vector2.one;
            titleRect.offsetMin = Vector2.zero;
            titleRect.offsetMax = Vector2.zero;
            var detailRect = toastDetail.rectTransform;
            detailRect.anchorMin = Vector2.zero;
            detailRect.anchorMax = new Vector2(1f, 0.42f);
            detailRect.offsetMin = new Vector2(8f, 6f);
            detailRect.offsetMax = new Vector2(-8f, 0f);
            toastDetail.gameObject.SetActive(hasDetail);

            shownAt = Time.unscaledTime;
        }
    }
}
