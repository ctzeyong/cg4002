using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Gameplay;
using BridgeRush.UI;

namespace BridgeRush.Input
{
    // Runs gestures from the glove (AiGestureHook calls PerformGesture) and shows the result banner for every
    // summon or spell attempt, whether it came from a glove gesture, a debug button or a card tap.
    public class GesturePad : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private SpellCaster spellCaster;
        [SerializeField] private Canvas canvas;
        [SerializeField] private Color acceptedColor = Color.green;
        [SerializeField] private Color rejectedColor = Color.red;
        [Tooltip("How long the result stays on screen.")]
        [SerializeField] private float resultDisplaySeconds = 3f;

        private const string DefaultSource = "Card";

        private string currentSource = DefaultSource;
        private Texture2D barTexture;
        private Texture2D backdropTexture;
        private string feedbackTitle;
        private string feedbackDetail;
        private Color feedbackColor;
        private float feedbackTimeRemaining;

        private void Awake()
        {
            if (matchState == null || spellCaster == null || canvas == null)
            {
                Debug.LogError($"{nameof(GesturePad)}: needs a Canvas, Match State and Spell Caster.", this);
                enabled = false;
                return;
            }

            barTexture = new Texture2D(1, 1);
            barTexture.SetPixel(0, 0, Color.white);
            barTexture.Apply();

            backdropTexture = new Texture2D(1, 1);
            backdropTexture.SetPixel(0, 0, new Color(0f, 0f, 0f, 0.72f));
            backdropTexture.Apply();

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

        private void OnUnitSummoned(UnitType type) =>
            ShowFeedback($"{currentSource} -> {type}", $"Summoned. Mana left: {matchState.ManaCurrent:0.#}", acceptedColor);

        private void OnUnitRejected(UnitType type, string reason) =>
            ShowFeedback($"{currentSource} -> {type}", matchState.DescribeSpawnFailure(type), rejectedColor);

        private void OnSpellCast(string key) =>
            ShowFeedback($"{currentSource} -> {SpellName(key)}", $"Cast. Mana left: {matchState.ManaCurrent:0.#}", acceptedColor);

        private void OnSpellRejected(string key, string reason) =>
            ShowFeedback($"{currentSource} -> {SpellName(key)}",
                spellCaster.DescribeCastFailure(key), rejectedColor);

        private static string SpellName(string key) => key == "lightning" ? "Lightning Strike" : "Fireball";

        private void Update()
        {
            if (feedbackTimeRemaining <= 0f) return;

            feedbackTimeRemaining -= Time.deltaTime;
            if (feedbackTimeRemaining <= 0f)
            {
                feedbackTitle = null;
                feedbackDetail = null;
            }
        }

        private void OnGUI()
        {
            if (string.IsNullOrEmpty(feedbackTitle)) return;

            float s = canvas.scaleFactor;
            float width = Mathf.Min(Screen.width - 24f * s, 620f * s);
            float height = 86f * s;
            float dockHeight = (HudLayout.HandTotalHeight + 16f) * s;
            var banner = new Rect(Screen.width / 2f - width / 2f, Screen.height - dockHeight - height - 12f * s, width, height);

            GUI.DrawTexture(banner, backdropTexture);
            var previousColor = GUI.color;
            GUI.color = feedbackColor;
            GUI.DrawTexture(new Rect(banner.x, banner.y, 6f * s, banner.height), barTexture);
            GUI.color = previousColor;

            var titleStyle = new GUIStyle(GUI.skin.label)
            {
                alignment = TextAnchor.UpperLeft,
                fontSize = Mathf.RoundToInt(22f * s),
                fontStyle = FontStyle.Bold,
                wordWrap = true,
                normal = { textColor = feedbackColor }
            };
            var detailStyle = new GUIStyle(GUI.skin.label)
            {
                alignment = TextAnchor.UpperLeft,
                fontSize = Mathf.RoundToInt(16f * s),
                wordWrap = true,
                normal = { textColor = Color.white }
            };
            GUI.Label(new Rect(banner.x + 16f * s, banner.y + 4f * s, banner.width - 24f * s, 30f * s), feedbackTitle, titleStyle);
            GUI.Label(new Rect(banner.x + 16f * s, banner.y + 36f * s, banner.width - 24f * s, banner.height - 40f * s), feedbackDetail, detailStyle);
        }

        public bool PerformGesture(GestureSymbol symbol, string source) =>
            Run(GestureBindings.FromSymbol(symbol), $"{symbol} ({source})");

        // A gesture by name (warrior, rogue, mage, tank, hero, lightning, fireball), e.g. from the debug menu.
        public bool PerformGloveGesture(string gestureName, string source) =>
            Run(GestureBindings.FromGlove(gestureName), $"{gestureName} ({source})");

        // The banner itself is shown by the summon and spell events above; this only names where the attempt came from.
        private bool Run(GameAction action, string label)
        {
            if (action == GameAction.None)
            {
                ShowFeedback($"{label}: INVALID gesture", "No unit or spell is bound to this gesture.", rejectedColor);
                return false;
            }

            currentSource = label;
            bool ok = GestureBindings.Perform(action, matchState, spellCaster);
            currentSource = DefaultSource;
            return ok;
        }

        private void ShowFeedback(string title, string detail, Color color)
        {
            feedbackTitle = title;
            feedbackDetail = detail;
            feedbackColor = color;
            feedbackTimeRemaining = resultDisplaySeconds;
        }
    }
}
