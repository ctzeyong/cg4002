using System;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;
using BridgeRush.Match;
using BridgeRush.Networking;
using BridgeRush.Tracking;
using BridgeRush.UI;

namespace BridgeRush.Tutorial
{
    /// <summary>
    /// The tutorial's coaching panel. The tutorial is a VS CPU match (MatchSetup.IsTutorial) with no enemy bot and free
    /// summons, so units and spells really spawn and every failure shows the usual banner (bad aim, outside the arena...).
    /// This says which gesture to do next and moves on when the game performs it: it listens to the summon and spell
    /// events, never to MQTT, so the glove still reaches the game only through GloveGestureReceiver (MqttConnection keeps
    /// one handler per topic).
    ///
    /// What the player sees, with as little text as possible: a big looping animation of the motion, the unit's name, the
    /// motion's name, a gold pointer on the matching card (the other cards dim), seven progress dots, a green "NICE!" when it
    /// works and a shake when the wrong thing was done. After the last step the match is cleared and a normal VS CPU match
    /// starts by itself.
    ///
    /// Builds itself at startup, like GloveGestureReceiver, so the scene needs no wiring.
    /// </summary>
    public class TutorialCoach : MonoBehaviour
    {
        public static TutorialCoach Instance { get; private set; }

        // Sits under the HUD's top row (LOCK TOWER, timer, OPTIONS). The result banner goes below it.
        private const float PanelTop = 250f;
        private const float PanelWidth = 760f;
        private const float PanelHeightUnits = 210f;
        private const float PipsHeight = 48f;
        private const float CelebrateSeconds = 1.2f;
        private const float WrongSeconds = 2.5f;
        private const float CompleteSeconds = 4f;
        private const int Pips = 7;

        private static readonly Color Gold = new Color(1f, 0.85f, 0.4f);
        private static readonly Color Good = new Color(0.45f, 0.9f, 0.45f);
        private static readonly Color Bad = new Color(1f, 0.45f, 0.4f);
        private static readonly Color Grey = new Color(0.42f, 0.38f, 0.34f);

        private readonly TutorialFlow flow = new TutorialFlow();
        private MatchSetup setup;
        private MatchState match;
        private SpellCaster spells;
        private ArenaFrame arena;
        private MatchCountdown countdown;
        private CardHandUI cards;
        private MatchHudUI hud;
        private MatchState subscribedMatch;
        private SpellCaster subscribedSpells;

        private float celebrateUntil;
        private int celebrateIndex = -1;
        private string celebrateName = "";
        private float wrongUntil;
        private float wrongStart = -10f;
        private string wrongName = "";
        private string wrongStepName = "";
        private string wrongMotion = "";
        private float completeAt = -1f;
        private bool cpuStartPending;
        private int shownIndex = -2;
        private float popStart = -10f;
        private int highlightedIndex = -2;

        // UI
        private GameObject root;
        private RectTransform panel;
        private Image glow;
        private GameObject[] motionViews;
        private TextMeshProUGUI stepText;
        private TextMeshProUGUI titleText;
        private TextMeshProUGUI motionText;
        private TextMeshProUGUI hintText;
        private Image[] pips;

        private bool IsTutorial => setup != null && setup.IsTutorial;

        /// <summary>A step is waiting for its gesture. The options menu shows its skip row only then.</summary>
        public bool IsRunning => IsTutorial && match != null && match.MatchStarted && !flow.Finished;

        /// <summary>How far down from the top bar the coach reaches while it shows, so the result banner can sit below it.</summary>
        public float PanelHeight => root != null && root.activeSelf ? PanelHeightUnits + PipsHeight : 0f;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Bootstrap()
        {
            if (FindAnyObjectByType<TutorialCoach>() != null) return;
            new GameObject(nameof(TutorialCoach)).AddComponent<TutorialCoach>();
        }

        private Func<string, string> filter;

        private void Awake()
        {
            Instance = this;
            filter = AllowAction;
        }

        private void OnDestroy()
        {
            if (Instance == this) Instance = null;
            if (subscribedMatch != null) subscribedMatch.OwnUnitSummoned -= OnUnitSummoned;
            if (subscribedSpells != null) subscribedSpells.SpellCast -= OnSpellCast;
        }

        // --- the lesson ---------------------------------------------------------------------------------------------

        private void Update()
        {
            if (setup == null) setup = FindAnyObjectByType<MatchSetup>();
            if (match == null) match = FindAnyObjectByType<MatchState>();
            if (spells == null) spells = FindAnyObjectByType<SpellCaster>();
            if (arena == null) arena = FindAnyObjectByType<ArenaFrame>();
            if (countdown == null) countdown = FindAnyObjectByType<MatchCountdown>();
            if (cards == null) cards = FindAnyObjectByType<CardHandUI>();
            if (hud == null) hud = FindAnyObjectByType<MatchHudUI>();
            if (setup == null || match == null || spells == null) return;

            Subscribe();
            StartCpuWhenReady();

            if (!setup.IsTutorial || !setup.IsSetupComplete) // MatchSetup clears the match's tutorial flags when the mode changes
            {
                if (match.ActionFilter == filter) match.ActionFilter = null;
                Hide();
                return;
            }
            match.ActionFilter = filter;
            if (!EnsureBuilt()) return;

            if (!match.MatchStarted)
            {
                // Nothing shows until START GAME has been pressed and the countdown is over. A new match (first start, or
                // Play Again after a restart) begins the lesson from the top.
                flow.Reset();
                celebrateIndex = -1;
                wrongUntil = 0f;
                completeAt = -1f;
                Hide();
                return;
            }

            // Nothing ends the match until the lesson hands over to the VS CPU match.
            match.EndlessMatch = true;
            match.GodMode = true; // free summons and no damage while learning, even if it was switched off in OPTIONS

            float now = Time.unscaledTime;
            if (flow.Finished)
            {
                if (completeAt < 0f) completeAt = now + CelebrateSeconds + CompleteSeconds;
                if (now < completeAt - CompleteSeconds) Show(Stage.Celebrate);
                else if (now < completeAt) Show(Stage.Complete);
                else StartVsCpu();
            }
            else
            {
                Show(now < celebrateUntil ? Stage.Celebrate : now < wrongUntil ? Stage.Wrong : Stage.Step);
            }
        }

        private void Subscribe()
        {
            if (subscribedMatch != match)
            {
                if (subscribedMatch != null) subscribedMatch.OwnUnitSummoned -= OnUnitSummoned;
                match.OwnUnitSummoned += OnUnitSummoned;
                subscribedMatch = match;
            }
            if (subscribedSpells != spells)
            {
                if (subscribedSpells != null) subscribedSpells.SpellCast -= OnSpellCast;
                spells.SpellCast += OnSpellCast;
                subscribedSpells = spells;
            }
        }

        // Only the current step's unit or spell may be performed, and nothing in the moment after a step is done, so wrong cards
        // and spamming do nothing. A wrong one shakes the panel. Before the match starts the game's own refusal applies.
        private string AllowAction(string action)
        {
            if (!IsTutorial || !match.MatchStarted) return null;
            float now = Time.unscaledTime;
            string refusal = flow.Refusal(action, now < celebrateUntil);
            if (refusal == "WRONG CARD")
            {
                var step = flow.Current;
                wrongName = action.ToUpperInvariant();
                wrongStepName = step.Name.ToUpperInvariant();
                wrongMotion = MotionName(step);
                wrongStart = now;
                wrongUntil = now + WrongSeconds;
            }
            return refusal;
        }

        private void OnUnitSummoned(UnitType type) => Report(type.ToString(), type.ToString().ToUpperInvariant());

        private void OnSpellCast(string spell) => Report(spell, spell == "lightning" ? "LIGHTNING" : "FIREBALL");

        private void Report(string action, string name)
        {
            if (!IsTutorial || !match.MatchStarted) return;

            var step = flow.Current;
            int index = flow.Index;
            switch (flow.Report(action))
            {
                case TutorialOutcome.Advanced:
                case TutorialOutcome.Finished:
                    celebrateIndex = index;
                    celebrateName = name;
                    celebrateUntil = Time.unscaledTime + CelebrateSeconds;
                    break;
                case TutorialOutcome.Wrong:
                    // The game did perform it; it is just not the one this step asks for.
                    wrongName = name;
                    wrongStepName = step.Name.ToUpperInvariant();
                    wrongMotion = MotionName(step);
                    wrongStart = Time.unscaledTime;
                    wrongUntil = wrongStart + WrongSeconds;
                    break;
            }
        }

        /// <summary>For the presenter, when a glove gesture will not come.</summary>
        public void SkipStep()
        {
            if (!IsRunning) return;
            celebrateIndex = flow.Index;
            celebrateName = flow.Current.Name.ToUpperInvariant();
            flow.Skip();
            celebrateUntil = Time.unscaledTime + CelebrateSeconds;
        }

        // The lesson is done: clear everything and start a normal VS CPU match by itself.
        private void StartVsCpu()
        {
            if (cards != null) cards.ClearTutorialHighlight();
            highlightedIndex = -2;
            match.ResetMatch();
            if (arena != null) arena.DebugForgetOpponent();
            if (countdown != null) countdown.ResetCountdownGate();
            setup.FinishTutorialIntoVsCpu(); // after the forget: it puts the fake opponent back
            flow.Reset();
            completeAt = -1f;
            celebrateIndex = -1;
            cpuStartPending = true;
            Hide();
        }

        // Starts the VS CPU match once the fake opponent has given the arena a lane (the frame after StartVsCpu).
        private void StartCpuWhenReady()
        {
            if (!cpuStartPending) return;
            if (arena != null && !arena.HasLane) return;
            cpuStartPending = false;
            if (countdown != null) countdown.RequestStart();
            else match.StartMatch();
        }

        private static string MotionName(TutorialStep step) =>
            step.IsSpell ? CardMotions.ForSpell(step.Action) : Enum.TryParse(step.Action, out UnitType type) ? CardMotions.For(type) : "tap";

        // --- what is on screen --------------------------------------------------------------------------------------

        private enum Stage { Step, Celebrate, Wrong, Complete }

        private void Hide()
        {
            if (root != null && root.activeSelf) root.SetActive(false);
            if (highlightedIndex != -2 && cards != null) cards.ClearTutorialHighlight();
            highlightedIndex = -2;
            shownIndex = -2;
        }

        private void Show(Stage stage)
        {
            if (!root.activeSelf) root.SetActive(true);
            float now = Time.unscaledTime;

            // Which step's picture: the one just done while "NICE!" shows, otherwise the current one.
            int index = stage == Stage.Celebrate && celebrateIndex >= 0 ? celebrateIndex : Mathf.Min(flow.Index, TutorialFlow.Steps.Count - 1);
            if (stage == Stage.Complete) index = -1;
            if (index != shownIndex)
            {
                shownIndex = index;
                popStart = now;
                for (int i = 0; i < motionViews.Length; i++) motionViews[i].SetActive(i == index);
            }

            // The pointer goes to the next card as soon as a step is done, not after "NICE!" has gone.
            int target = stage == Stage.Complete || flow.Finished ? -1 : flow.Index;
            if (target != highlightedIndex && cards != null)
            {
                highlightedIndex = target;
                if (target < 0) cards.ClearTutorialHighlight();
                else
                {
                    var step = TutorialFlow.Steps[target];
                    if (step.IsSpell) cards.HighlightForTutorial(null, step.Action);
                    else if (Enum.TryParse(step.Action, out UnitType type)) cards.HighlightForTutorial(type, null);
                }
            }

            string title, line, hint;
            Color titleColor, glowColor;
            var current = index >= 0 ? TutorialFlow.Steps[index] : null;
            switch (stage)
            {
                case Stage.Celebrate:
                    title = "NICE!"; titleColor = Good;
                    line = celebrateName; hint = "";
                    glowColor = Good;
                    break;
                case Stage.Wrong:
                    title = "NOT QUITE"; titleColor = Bad;
                    line = $"That was {wrongName}"; hint = $"For {wrongStepName}, do the {wrongMotion.ToUpperInvariant()}";
                    glowColor = Bad;
                    break;
                case Stage.Complete:
                    int left = Mathf.Max(1, Mathf.CeilToInt(completeAt - now));
                    title = "ALL DONE!"; titleColor = Good;
                    line = $"VS CPU starts in {left}"; hint = "Everything resets for a fresh match";
                    glowColor = Good;
                    break;
                default:
                    title = current.Name.ToUpperInvariant(); titleColor = HudPalette.Cream;
                    line = MotionName(current).ToUpperInvariant();
                    hint = current.IsSpell ? "Aim anywhere on the island" : "Aim at the ground on your side";
                    glowColor = Gold;
                    break;
            }

            stepText.text = stage == Stage.Complete ? "" : $"STEP {index + 1} OF {TutorialFlow.Steps.Count}";
            titleText.text = title;
            titleText.color = titleColor;
            motionText.text = line;
            motionText.color = stage == Stage.Step ? Gold : HudPalette.Cream;
            hintText.text = hint;

            // A small pop when the picture changes, a shake when the wrong thing was done.
            float pop = 1f + 0.07f * Mathf.Max(0f, 1f - (now - popStart) / 0.25f);
            panel.localScale = new Vector3(pop, pop, 1f);
            float shakeAge = now - wrongStart;
            float shake = stage == Stage.Wrong && shakeAge < 0.5f ? Mathf.Sin(shakeAge * 45f) * 14f * (1f - shakeAge / 0.5f) : 0f;
            panel.anchoredPosition = new Vector2(shake, -PanelTop);

            float beat = 0.5f + 0.5f * Mathf.Sin(now * 4f);
            glow.color = new Color(glowColor.r, glowColor.g, glowColor.b, 0.25f + 0.3f * beat);

            for (int i = 0; i < pips.Length; i++)
            {
                bool done = flow.Finished || i < flow.Index;
                bool isCurrent = !flow.Finished && i == flow.Index;
                pips[i].color = done ? Good : isCurrent ? Gold : Grey;
                float size = isCurrent ? 1.25f + 0.15f * beat : 1f;
                pips[i].rectTransform.localScale = new Vector3(size, size, 1f);
            }
        }

        // --- building the panel -------------------------------------------------------------------------------------

        private bool EnsureBuilt()
        {
            if (root != null) return true;
            var canvas = hud != null ? hud.HudCanvas : null;
            if (canvas == null) return false;

            root = UIFactory.CreateRect("TutorialCoach", canvas.transform);
            UIFactory.StretchFull(root.GetComponent<RectTransform>());

            var panelGo = UIFactory.CreateRect("Panel", root.transform);
            panel = panelGo.GetComponent<RectTransform>();
            panel.anchorMin = new Vector2(0.5f, 1f);
            panel.anchorMax = new Vector2(0.5f, 1f);
            panel.pivot = new Vector2(0.5f, 1f);
            panel.anchoredPosition = new Vector2(0f, -PanelTop);
            panel.sizeDelta = new Vector2(PanelWidth, PanelHeightUnits);
            var background = panelGo.AddComponent<Image>();
            background.sprite = UITextureFactory.CreateRoundedPanel(72, 24, HudPalette.Wood, HudPalette.WoodDark, 4, HudPalette.Gold);
            background.type = Image.Type.Sliced;
            background.raycastTarget = false;

            // The animation, on a darker tile with a soft glow behind it.
            var tileGo = UIFactory.CreateRect("MotionTile", panel);
            var tile = tileGo.GetComponent<RectTransform>();
            tile.anchorMin = new Vector2(0f, 0.5f);
            tile.anchorMax = new Vector2(0f, 0.5f);
            tile.anchoredPosition = new Vector2(125f, 0f);
            tile.sizeDelta = new Vector2(180f, 180f);
            var tileImage = tileGo.AddComponent<Image>();
            tileImage.sprite = UITextureFactory.CreateRoundedPanel(64, 18, new Color(0.13f, 0.08f, 0.04f), new Color(0.09f, 0.05f, 0.02f), 2, HudPalette.GoldDark);
            tileImage.type = Image.Type.Sliced;
            tileImage.raycastTarget = false;

            var glowGo = UIFactory.CreateRect("Glow", tile);
            var glowRect = glowGo.GetComponent<RectTransform>();
            glowRect.sizeDelta = new Vector2(210f, 210f);
            glow = glowGo.AddComponent<Image>();
            glow.sprite = UITextureFactory.CreateGlowDot();
            glow.raycastTarget = false;

            motionViews = new GameObject[TutorialFlow.Steps.Count];
            for (int i = 0; i < motionViews.Length; i++)
            {
                var step = TutorialFlow.Steps[i];
                string motion = MotionName(step);
                var iconGo = UIFactory.CreateRect("Motion_" + step.Name, tile);
                var iconRect = iconGo.GetComponent<RectTransform>();
                iconRect.sizeDelta = new Vector2(160f, 160f);
                var icon = iconGo.AddComponent<Image>();
                icon.sprite = UITextureFactory.CreateMotionIcon(motion);
                icon.preserveAspect = true;
                icon.color = HudPalette.Cream;
                icon.raycastTarget = false;
                MotionTracer.Attach(iconRect, motion, Gold, 2.4f);
                iconGo.SetActive(false);
                motionViews[i] = iconGo;
            }

            stepText = Label("Step", panel, 22f, FontStyles.Bold, Gold, 0.82f, 0.97f);
            titleText = Label("Title", panel, 56f, FontStyles.Bold, HudPalette.Cream, 0.46f, 0.84f);
            motionText = Label("Motion", panel, 36f, FontStyles.Bold, Gold, 0.27f, 0.47f);
            hintText = Label("Hint", panel, 23f, FontStyles.Normal, new Color(0.95f, 0.9f, 0.78f), 0.05f, 0.27f);

            // Progress dots under the card.
            var pipRow = UIFactory.CreateRect("Progress", panel);
            var pipRowRect = pipRow.GetComponent<RectTransform>();
            pipRowRect.anchorMin = new Vector2(0.5f, 0f);
            pipRowRect.anchorMax = new Vector2(0.5f, 0f);
            pipRowRect.anchoredPosition = new Vector2(0f, -28f);
            pipRowRect.sizeDelta = new Vector2(Pips * 38f, 30f);
            pips = new Image[Pips];
            for (int i = 0; i < Pips; i++)
            {
                var dot = UIFactory.CreateRect("Pip" + i, pipRow.transform);
                var dotRect = dot.GetComponent<RectTransform>();
                dotRect.anchoredPosition = new Vector2((i - (Pips - 1) * 0.5f) * 38f, 0f);
                dotRect.sizeDelta = new Vector2(22f, 22f);
                pips[i] = dot.AddComponent<Image>();
                pips[i].sprite = UITextureFactory.CreateGlowDot();
                pips[i].raycastTarget = false;
            }

            root.SetActive(false);
            return true;
        }

        // Left-aligned text in the right-hand part of the panel, between two heights (fractions of the panel).
        private static TextMeshProUGUI Label(string name, RectTransform parent, float size, FontStyles style, Color color, float yMin, float yMax)
        {
            var text = UIFactory.CreateText(parent, string.Empty, size, style, color);
            text.gameObject.name = name;
            text.alignment = TextAlignmentOptions.MidlineLeft;
            var rect = text.rectTransform;
            rect.anchorMin = new Vector2(0.315f, yMin);
            rect.anchorMax = new Vector2(0.975f, yMax);
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;
            return text;
        }
    }
}
