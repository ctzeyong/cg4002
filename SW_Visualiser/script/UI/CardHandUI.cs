using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Input;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Gameplay;
using BridgeRush.Networking;

namespace BridgeRush.UI
{
    public class CardHandUI : MonoBehaviour
    {
        [SerializeField] private Canvas canvas;
        [SerializeField] private MatchState matchState;
        [SerializeField] private SpellCaster spellCaster;
        [SerializeField] private UnitStatsTable unitStatsTable;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchCountdown matchCountdown;
        [SerializeField] private PeerReadySync peerReadySync;
        [Tooltip("Hides the start button until the mode (and player, in VS Player) is chosen. Optional.")]
        [SerializeField] private MatchSetup matchSetup;

        private const float CardWidth = HudLayout.CardWidth;
        private const float CardHeight = HudLayout.CardHeight;
        private const float CardSpacing = HudLayout.CardSpacing;
        private const float HandBottomMargin = HudLayout.HandBottomMargin;

        private readonly List<CardEntry> cards = new();
        private RectTransform handRoot;
        private GameObject startGateRoot;
        private Button startGateButton;
        private Image startGateImage;
        private TextMeshProUGUI startGateLabel;

        private enum StartGateState { Hidden, NoBoard, Searching, ReadyToLockIn, WaitingForPeer, ReadyToStart }

        private class CardEntry
        {
            public System.Func<float> GetCost;
            public System.Action Cast;
            public Image Glow;
            public Image Panel;
            public List<Image> Dimmable;
            public TextMeshProUGUI CostText;
            public UnitType? Unit;
            public string SpellKey;
            public RectTransform Root;
            public Image Burst;
            public Image Flash;
            public TextMeshProUGUI SummonText;
            public float PopStart = -10f;
            public bool Popping;
            public Image Reject;
            public RectTransform Pointer;
            public Color LabelColor;
            public float RejectStart = -10f;
            public bool Rejecting;
        }

        // The tutorial's target card: it glows gold with a bobbing pointer above it, and the other cards dim.
        private CardEntry tutorialTarget;
        private static Sprite pointerSprite;
        private static readonly Color PointerGold = new(1f, 0.85f, 0.3f);

        public void HighlightForTutorial(UnitType? unit, string spellKey)
        {
            tutorialTarget = null;
            foreach (var card in cards)
            {
                if ((unit.HasValue && card.Unit == unit) || (spellKey != null && card.SpellKey == spellKey)) tutorialTarget = card;
            }
            if (tutorialTarget == null) ClearTutorialHighlight();
        }

        public void ClearTutorialHighlight()
        {
            tutorialTarget = null;
            foreach (var card in cards)
            {
                card.Pointer.gameObject.SetActive(false);
                card.Glow.rectTransform.localScale = Vector3.one;
                var accent = card.LabelColor;
                card.Glow.color = new Color(accent.r, accent.g, accent.b, 0f);
            }
        }

        // A white triangle pointing down, for the tutorial pointer.
        private static Sprite PointerSprite()
        {
            if (pointerSprite != null) return pointerSprite;
            const int size = 64;
            var tex = new Texture2D(size, size, TextureFormat.RGBA32, false) { filterMode = FilterMode.Bilinear, wrapMode = TextureWrapMode.Clamp };
            for (int y = 0; y < size; y++)
            {
                float half = (y / (float)(size - 1)) * (size * 0.5f); // narrow at the bottom, wide at the top
                for (int x = 0; x < size; x++)
                {
                    float d = Mathf.Abs(x - (size - 1) * 0.5f);
                    tex.SetPixel(x, y, new Color(1f, 1f, 1f, Mathf.Clamp01(half - d + 0.5f)));
                }
            }
            tex.Apply();
            pointerSprite = Sprite.Create(tex, new Rect(0, 0, size, size), new Vector2(0.5f, 0.5f), 100f);
            return pointerSprite;
        }

        private const float PopSeconds = 0.8f;
        private const float RejectSeconds = 0.7f;
        private static readonly Color RejectLabelColor = new(1f, 0.38f, 0.3f);

        private void Awake()
        {
            if (canvas == null || matchState == null || unitStatsTable == null || spellCaster == null)
            {
                Debug.LogError($"{nameof(CardHandUI)}: needs a Canvas, Match State, Unit Stats Table and Spell Caster.", this);
                enabled = false;
                return;
            }

            BuildHand();
            BuildStartGate();

            matchState.OwnUnitSummoned += OnOwnUnitSummoned;
            matchState.OwnUnitRejected += OnOwnUnitRejected;
            spellCaster.SpellCast += OnSpellCast;
            spellCaster.SpellRejected += OnSpellRejected;
        }

        private void OnDestroy()
        {
            if (matchState != null)
            {
                matchState.OwnUnitSummoned -= OnOwnUnitSummoned;
                matchState.OwnUnitRejected -= OnOwnUnitRejected;
            }
            if (spellCaster != null)
            {
                spellCaster.SpellCast -= OnSpellCast;
                spellCaster.SpellRejected -= OnSpellRejected;
            }
        }

        private void OnOwnUnitRejected(UnitType type, string reason)
        {
            foreach (var card in cards)
            {
                if (card.Unit == type) StartReject(card, reason);
            }
        }

        private void OnSpellRejected(string key, string reason)
        {
            foreach (var card in cards)
            {
                if (card.SpellKey == key) StartReject(card, reason);
            }
        }

        private static void StartReject(CardEntry card, string reason)
        {
            ResetPop(card);
            card.RejectStart = Time.unscaledTime;
            card.Rejecting = true;
            card.SummonText.text = $"FAILED!\n<size=65%>{reason}</size>";
            card.SummonText.color = RejectLabelColor;
        }

        private static void ResetPop(CardEntry card)
        {
            card.Popping = false;
            card.Root.localScale = Vector3.one;
            card.Root.localRotation = Quaternion.identity;
            SetAlpha(card.Burst, 0f);
            SetAlpha(card.Flash, 0f);
            SetAlpha(card.Reject, 0f);
            SetAlpha(card.SummonText, 0f);
            card.SummonText.rectTransform.anchoredPosition = Vector2.zero;
        }

        private void OnOwnUnitSummoned(UnitType type)
        {
            foreach (var card in cards)
            {
                if (card.Unit == type) StartPop(card, "SUMMONED!");
            }
        }

        private void OnSpellCast(string key)
        {
            foreach (var card in cards)
            {
                if (card.SpellKey == key) StartPop(card, "CAST!");
            }
        }

        private static void StartPop(CardEntry card, string label)
        {
            card.Rejecting = false;
            ResetPop(card);
            card.PopStart = Time.unscaledTime;
            card.Popping = true;
            card.SummonText.text = label;
            card.SummonText.color = card.LabelColor;
        }

        private static float EaseOut(float t) => 1f - (1f - t) * (1f - t);

        // Pop, white flash, glow burst and a rising label on the card that was just used.
        private static void AnimatePop(CardEntry card)
        {
            AnimateReject(card);
            if (!card.Popping) return;

            float t = (Time.unscaledTime - card.PopStart) / PopSeconds;
            if (t >= 1f)
            {
                ResetPop(card);
                return;
            }

            float scale = 1f + 0.28f * Mathf.Sin(Mathf.PI * Mathf.Min(1f, t / 0.45f));
            card.Root.localScale = new Vector3(scale, scale, 1f);

            card.Burst.rectTransform.localScale = Vector3.one * (1f + 0.9f * EaseOut(t));
            SetAlpha(card.Burst, Mathf.Pow(1f - t, 1.5f) * 0.9f);

            SetAlpha(card.Flash, Mathf.Max(0f, 1f - t / 0.3f) * 0.6f);

            card.SummonText.rectTransform.anchoredPosition = new Vector2(0f, 14f + 52f * EaseOut(t));
            SetAlpha(card.SummonText, t < 0.7f ? 1f : (1f - t) / 0.3f);
        }

        // A refused summon: the card flashes dark red, shakes side to side and shows why.
        private static void AnimateReject(CardEntry card)
        {
            if (!card.Rejecting) return;

            float t = (Time.unscaledTime - card.RejectStart) / RejectSeconds;
            if (t >= 1f)
            {
                card.Rejecting = false;
                ResetPop(card);
                return;
            }

            float shake = Mathf.Sin(t * Mathf.PI * 7f) * (1f - t) * 7f;
            card.Root.localRotation = Quaternion.Euler(0f, 0f, shake);

            SetAlpha(card.Reject, t < 0.55f ? 0.78f : 0.78f * (1f - (t - 0.55f) / 0.45f));

            card.SummonText.rectTransform.anchoredPosition = new Vector2(0f, 14f + 22f * EaseOut(t));
            SetAlpha(card.SummonText, t < 0.7f ? 1f : (1f - t) / 0.3f);
        }

        // Dims or restores everything on the card that carries its colour (the panel, the picture frame and the name plate).
        private static void TintCard(CardEntry card, Color color)
        {
            card.Panel.color = color;
            foreach (var image in card.Dimmable) image.color = color;
        }

        private static void SetAlpha(Graphic graphic, float alpha)
        {
            var color = graphic.color;
            color.a = alpha;
            graphic.color = color;
        }

        private void Update()
        {
            var gateState = ComputeStartGateState();
            startGateRoot.SetActive(gateState != StartGateState.Hidden);
            if (gateState != StartGateState.Hidden) ApplyStartGateState(gateState);

            bool started = matchState.MatchStarted;
            handRoot.gameObject.SetActive(started);
            if (!started) return;
            handRoot.anchoredPosition = new Vector2(0f, HudLayout.HandBottom(canvas)); // clear of a phone's home bar

            foreach (var card in cards)
            {
                float cost = card.GetCost();
                bool ready = matchState.ManaCurrent >= cost;

                card.CostText.text = cost.ToString("0");
                AnimatePop(card);

                if (tutorialTarget != null)
                {
                    bool target = card == tutorialTarget;
                    float beat = 0.5f + 0.5f * Mathf.Sin(Time.unscaledTime * 5f);
                    card.Glow.color = target ? new Color(PointerGold.r, PointerGold.g, PointerGold.b, 0.55f + 0.45f * beat) : new Color(1f, 1f, 1f, 0f);
                    card.Glow.rectTransform.localScale = Vector3.one * (target ? 1.05f + 0.07f * beat : 1f);
                    TintCard(card, target ? Color.white : new Color(0.5f, 0.5f, 0.5f, 1f));
                    card.Pointer.gameObject.SetActive(target);
                    if (target) card.Pointer.anchoredPosition = new Vector2(0f, 14f + 12f * beat);
                    continue;
                }

                if (card.Pointer.gameObject.activeSelf) card.Pointer.gameObject.SetActive(false);
                card.Glow.rectTransform.localScale = Vector3.one;

                if (ready)
                {
                    float pulse = 0.55f + Mathf.PingPong(Time.time * 1.4f, 0.45f);
                    var glowColor = card.Glow.color;
                    glowColor.a = pulse;
                    card.Glow.color = glowColor;
                    TintCard(card, Color.white);
                }
                else
                {
                    var glowColor = card.Glow.color;
                    glowColor.a = 0f;
                    card.Glow.color = glowColor;
                    TintCard(card, new Color(0.55f, 0.55f, 0.55f, 1f));
                }
            }
        }

        private void BuildHand()
        {
            var handGo = UIFactory.CreateRect("CardHand", canvas.transform);
            handRoot = handGo.GetComponent<RectTransform>();
            handRoot.anchorMin = new Vector2(0.5f, 0f);
            handRoot.anchorMax = new Vector2(0.5f, 0f);
            handRoot.pivot = new Vector2(0.5f, 0f);
            handRoot.anchoredPosition = new Vector2(0f, HudLayout.HandBottom(canvas));

            var layout = handGo.AddComponent<HorizontalLayoutGroup>();
            layout.spacing = CardSpacing;
            layout.childAlignment = TextAnchor.LowerCenter;
            layout.childControlWidth = false;
            layout.childControlHeight = false;
            var fitter = handGo.AddComponent<ContentSizeFitter>();
            fitter.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            AddUnitCard(handRoot, CardMotions.For(UnitType.Warrior), "Warrior", UnitType.Warrior);
            AddUnitCard(handRoot, CardMotions.For(UnitType.Rogue), "Rogue", UnitType.Rogue);
            AddUnitCard(handRoot, CardMotions.For(UnitType.Mage), "Mage", UnitType.Mage);
            AddUnitCard(handRoot, CardMotions.For(UnitType.Tank), "Tank", UnitType.Tank);
            AddUnitCard(handRoot, CardMotions.For(UnitType.Hero), "Hero", UnitType.Hero);

            AddSpellCard(handRoot, CardMotions.ForSpell("lightning"), "Lightning", "lightning",
                () => spellCaster.LightningStrikeManaCost, () => spellCaster.CastLightningStrike());
            AddSpellCard(handRoot, CardMotions.ForSpell("fireball"), "Fireball", "fireball",
                () => spellCaster.FireballManaCost, () => spellCaster.CastFireball());
        }

        private void AddUnitCard(Transform parent, string gesture, string name, UnitType type)
        {
            System.Func<float> getCost = () =>
                unitStatsTable.TryGetEntry(type, out var e) ? e.ManaCost : 0f;

            var entry = BuildCard(parent, gesture, name, CardArt.Unit, isSpell: false, winged: type == UnitType.Hero);
            entry.GetCost = getCost;
            entry.Unit = type;
            entry.Cast = () => matchState.SpawnUnit(type);
            cards.Add(entry);
        }

        private void AddSpellCard(Transform parent, string gesture, string name, string spellKey,
            System.Func<float> getCost, System.Action cast)
        {
            var entry = BuildCard(parent, gesture, name, CardArt.ForSpell(spellKey), isSpell: true, winged: false);
            entry.GetCost = getCost;
            entry.SpellKey = spellKey;
            entry.Cast = cast;
            cards.Add(entry);
        }

        private CardEntry BuildCard(Transform parent, string gesture, string name, CardArt.Theme theme, bool isSpell, bool winged)
        {
            Color accent = isSpell ? HudPalette.Spell : HudPalette.Gold;
            Color accentDeep = isSpell ? HudPalette.SpellDeep : HudPalette.GoldDark;

            var cardGo = UIFactory.CreateRect("Card_" + name, parent);
            var cardRect = cardGo.GetComponent<RectTransform>();
            cardRect.sizeDelta = new Vector2(CardWidth, CardHeight);

            var layoutElement = cardGo.AddComponent<LayoutElement>();
            layoutElement.preferredWidth = CardWidth;
            layoutElement.preferredHeight = CardHeight;

            var glowGo = UIFactory.CreateRect("Glow", cardRect);
            var glowRect = glowGo.GetComponent<RectTransform>();
            glowRect.anchorMin = Vector2.zero;
            glowRect.anchorMax = Vector2.one;
            glowRect.offsetMin = new Vector2(-10f, -10f);
            glowRect.offsetMax = new Vector2(10f, 10f);
            var glowImage = glowGo.AddComponent<Image>();
            glowImage.sprite = UITextureFactory.CreateGlow(128, 22, 26);
            glowImage.color = new Color(accent.r, accent.g, accent.b, 0f);
            glowImage.raycastTarget = false;

            var burstGo = UIFactory.CreateRect("Burst", cardRect);
            var burstRect = burstGo.GetComponent<RectTransform>();
            burstRect.anchorMin = Vector2.zero;
            burstRect.anchorMax = Vector2.one;
            burstRect.offsetMin = new Vector2(-10f, -10f);
            burstRect.offsetMax = new Vector2(10f, 10f);
            var burstImage = burstGo.AddComponent<Image>();
            burstImage.sprite = UITextureFactory.CreateGlow(128, 22, 26);
            burstImage.color = new Color(1f, 1f, 1f, 0f);
            burstImage.raycastTarget = false;

            var panelGo = UIFactory.CreateRect("Panel", cardRect);
            var panelRect = panelGo.GetComponent<RectTransform>();
            panelRect.anchorMin = Vector2.zero;
            panelRect.anchorMax = Vector2.one;
            panelRect.offsetMin = Vector2.zero;
            panelRect.offsetMax = Vector2.zero;
            var panelImage = panelGo.AddComponent<Image>();
            panelImage.sprite = CardArt.Frame(theme);
            panelImage.type = Image.Type.Sliced;

            var button = cardGo.AddComponent<Button>();
            button.targetGraphic = panelImage;
            button.transition = Selectable.Transition.None;

            var flashGo = UIFactory.CreateRect("Flash", cardRect);
            var flashRect = flashGo.GetComponent<RectTransform>();
            UIFactory.StretchFull(flashRect);
            var flashImage = flashGo.AddComponent<Image>();
            flashImage.sprite = UITextureFactory.CreateRoundedPanel(96, 14, Color.white, Color.white, 0, Color.clear);
            flashImage.type = Image.Type.Sliced;
            flashImage.color = new Color(1f, 1f, 1f, 0f);
            flashImage.raycastTarget = false;

            // The picture window: a dark recess inside the frame's coloured band. The gesture animation plays in it.
            var dimmable = new List<Image>();
            var frameGo = UIFactory.CreateRect("PictureFrame", cardRect);
            var frameRect = frameGo.GetComponent<RectTransform>();
            frameRect.anchorMin = new Vector2(0.105f, 0.39f);
            frameRect.anchorMax = new Vector2(0.895f, 0.935f);
            frameRect.offsetMin = frameRect.offsetMax = Vector2.zero;
            var frameImage = frameGo.AddComponent<Image>();
            frameImage.sprite = CardArt.Window(theme);
            frameImage.type = Image.Type.Sliced;
            frameImage.raycastTarget = false;
            dimmable.Add(frameImage);

            // A faint light behind the gesture and a drop shadow under it, so the cream lines lift off the dark window.
            var haloGo = UIFactory.CreateRect("Halo", cardRect);
            var haloRect = haloGo.GetComponent<RectTransform>();
            haloRect.anchorMin = haloRect.anchorMax = new Vector2(0.5f, 0.665f);
            haloRect.sizeDelta = new Vector2(120f, 120f);
            var haloImage = haloGo.AddComponent<Image>();
            haloImage.sprite = UITextureFactory.CreateGlowDot();
            haloImage.color = new Color(1f, 0.86f, 0.55f, 0.10f);
            haloImage.raycastTarget = false;

            var iconSprite = UITextureFactory.CreateMotionIcon(gesture);
            var shadowGo = UIFactory.CreateRect("GestureShadow", cardRect);
            var shadowRect = shadowGo.GetComponent<RectTransform>();
            shadowRect.anchorMin = new Vector2(0.5f, 0.39f);
            shadowRect.anchorMax = new Vector2(0.5f, 0.935f);
            shadowRect.sizeDelta = new Vector2(110f, -18f);
            shadowRect.anchoredPosition = new Vector2(3f, -3f);
            var shadowImage = shadowGo.AddComponent<Image>();
            shadowImage.sprite = iconSprite;
            shadowImage.preserveAspect = true;
            shadowImage.color = new Color(0f, 0f, 0f, 0.5f);
            shadowImage.raycastTarget = false;

            var iconGo = UIFactory.CreateRect("GestureIcon", cardRect);
            var iconRect = iconGo.GetComponent<RectTransform>();
            iconRect.anchorMin = new Vector2(0.5f, 0.39f);
            iconRect.anchorMax = new Vector2(0.5f, 0.935f);
            iconRect.sizeDelta = new Vector2(110f, -18f);   // as wide as the window allows: the gesture is the point of the card
            iconRect.anchoredPosition = Vector2.zero;
            var iconImage = iconGo.AddComponent<Image>();
            iconImage.sprite = iconSprite;
            iconImage.preserveAspect = true;
            iconImage.color = HudPalette.Cream;
            iconImage.raycastTarget = false;
            MotionTracer.Attach(iconRect, gesture, accent, 2f);

            // The name plank: dark boards in the card's colour with the name on it.
            var plateGo = UIFactory.CreateRect("NamePlate", cardRect);
            var plateRect = plateGo.GetComponent<RectTransform>();
            plateRect.anchorMin = new Vector2(0.045f, 0.262f);
            plateRect.anchorMax = new Vector2(0.955f, 0.382f);
            plateRect.offsetMin = plateRect.offsetMax = Vector2.zero;
            var plateImage = plateGo.AddComponent<Image>();
            plateImage.sprite = CardArt.Plank(theme);
            plateImage.raycastTarget = false;
            dimmable.Add(plateImage);

            // The Hero's gold wings: one each side of the plank, reaching a little past the card's edge.
            if (winged)
            {
                dimmable.Add(AddWing(cardRect, "WingLeft", -1f));
                dimmable.Add(AddWing(cardRect, "WingRight", 1f));
            }

            var nameText = UIFactory.CreateText(cardRect, name, 24, FontStyles.Normal, new Color(1f, 0.96f, 0.85f));
            UIFactory.ApplyOutline(nameText, 0.2f, new Color32(8, 6, 14, 255));
            var nameRect = nameText.rectTransform;
            nameRect.anchorMin = new Vector2(0.045f, 0.262f);
            nameRect.anchorMax = new Vector2(0.955f, 0.382f);
            nameRect.offsetMin = new Vector2(18f, 0f);
            nameRect.offsetMax = new Vector2(-18f, 0f);
            nameText.enableAutoSizing = true; // "Lightning" must stay on one line in a 150-wide card
            nameText.fontSizeMin = 14f;
            nameText.fontSizeMax = 24f;
            nameText.textWrappingMode = TextWrappingModes.NoWrap;

            // What to do with the glove, in small muted letters under the plank.
            var motionText = UIFactory.CreateText(cardRect, CardMotions.DisplayName(gesture).ToLowerInvariant(), 18f, FontStyles.Normal, new Color(0.82f, 0.72f, 0.54f));
            var motionRect = motionText.rectTransform;
            motionRect.anchorMin = new Vector2(0.07f, 0.075f);
            motionRect.anchorMax = new Vector2(0.93f, 0.235f);
            motionRect.offsetMin = motionRect.offsetMax = Vector2.zero;
            motionText.enableAutoSizing = true;
            motionText.fontSizeMin = 11f;
            motionText.fontSizeMax = 18f;
            motionText.textWrappingMode = TextWrappingModes.NoWrap;

            // The small diamond on the top edge.
            var topGo = UIFactory.CreateRect("TopOrnament", cardRect);
            var topRect = topGo.GetComponent<RectTransform>();
            topRect.anchorMin = topRect.anchorMax = new Vector2(0.5f, 1f);
            topRect.pivot = new Vector2(0.5f, 0.5f);
            topRect.anchoredPosition = new Vector2(0f, 1f);
            topRect.sizeDelta = new Vector2(72f, 43f);
            var topImage = topGo.AddComponent<Image>();
            topImage.sprite = CardArt.TopOrnament(theme);
            topImage.raycastTarget = false;
            dimmable.Add(topImage);

            // The mana cost: a faceted gem on the top-left corner. It is built last among the card's parts so nothing is drawn over it.
            var costGo = UIFactory.CreateRect("Cost", cardRect);
            var costRect = costGo.GetComponent<RectTransform>();
            costRect.anchorMin = new Vector2(0f, 1f);
            costRect.anchorMax = new Vector2(0f, 1f);
            costRect.pivot = new Vector2(0.5f, 0.5f);
            costRect.anchoredPosition = new Vector2(17f, -1f);
            costRect.sizeDelta = new Vector2(62f, 62f);
            var costImage = costGo.AddComponent<Image>();
            costImage.sprite = CardArt.Gem(theme);
            costImage.raycastTarget = false;
            dimmable.Add(costImage);   // goes grey with the card when it cannot be afforded
            var costText = UIFactory.CreateText(costGo.transform, "0", 34, FontStyles.Bold, Color.white);
            UIFactory.ApplyOutline(costText, 0.22f, new Color32(8, 20, 70, 255));
            var costTextRect = costText.rectTransform;
            costTextRect.anchorMin = Vector2.zero;
            costTextRect.anchorMax = Vector2.one;
            costTextRect.offsetMin = costTextRect.offsetMax = Vector2.zero;

            var rejectGo = UIFactory.CreateRect("Reject", cardRect);
            var rejectRect = rejectGo.GetComponent<RectTransform>();
            UIFactory.StretchFull(rejectRect);
            var rejectImage = rejectGo.AddComponent<Image>();
            rejectImage.sprite = UITextureFactory.CreateRoundedPanel(96, 14, Color.white, Color.white, 0, Color.clear);
            rejectImage.type = Image.Type.Sliced;
            rejectImage.color = new Color(0.32f, 0.02f, 0.02f, 0f);
            rejectImage.raycastTarget = false;

            var summonText = UIFactory.CreateText(cardRect, string.Empty, 28f, FontStyles.Bold, accent);
            var summonRect = summonText.rectTransform;
            summonRect.anchorMin = new Vector2(0f, 1f);
            summonRect.anchorMax = new Vector2(1f, 1f);
            summonRect.pivot = new Vector2(0.5f, 0f);
            summonRect.anchoredPosition = Vector2.zero;
            summonRect.sizeDelta = new Vector2(60f, 34f);
            summonText.textWrappingMode = TextWrappingModes.NoWrap;
            summonText.overflowMode = TextOverflowModes.Overflow;
            SetAlpha(summonText, 0f);

            var pointerGo = UIFactory.CreateRect("TutorialPointer", cardRect);
            var pointerRect = pointerGo.GetComponent<RectTransform>();
            pointerRect.anchorMin = new Vector2(0.5f, 1f);
            pointerRect.anchorMax = new Vector2(0.5f, 1f);
            pointerRect.pivot = new Vector2(0.5f, 0f);
            pointerRect.anchoredPosition = new Vector2(0f, 14f);
            pointerRect.sizeDelta = new Vector2(52f, 44f);
            var pointerImage = pointerGo.AddComponent<Image>();
            pointerImage.sprite = PointerSprite();
            pointerImage.color = PointerGold;
            pointerImage.raycastTarget = false;
            pointerGo.SetActive(false);

            var entry = new CardEntry
            {
                Pointer = pointerRect,
                Glow = glowImage,
                Panel = panelImage,
                Dimmable = dimmable,
                CostText = costText,
                Root = cardRect,
                Burst = burstImage,
                Flash = flashImage,
                Reject = rejectImage,
                SummonText = summonText,
                LabelColor = accent
            };

            button.onClick.AddListener(() => entry.Cast?.Invoke());

            return entry;
        }

        // One gold wing for the Hero card, fanning out from the end of the name plank. `side` is -1 for the left and 1 for the right.
        private static Image AddWing(RectTransform card, string name, float side)
        {
            var go = UIFactory.CreateRect(name, card);
            var rect = go.GetComponent<RectTransform>();
            rect.anchorMin = rect.anchorMax = new Vector2(side < 0f ? 0f : 1f, 0.322f);
            rect.pivot = new Vector2(0.92f, 0.22f);   // the wing's root: flipping for the right side mirrors around it
            rect.anchoredPosition = new Vector2(side < 0f ? 32f : -32f, 0f);
            rect.sizeDelta = new Vector2(46f, 35f);
            rect.localScale = new Vector3(side < 0f ? 1f : -1f, 1f, 1f);
            var image = go.AddComponent<Image>();
            image.sprite = CardArt.Wing();
            image.raycastTarget = false;
            return image;
        }

        private void BuildStartGate()
        {
            startGateRoot = UIFactory.CreateRect("StartGate", canvas.transform);
            var rect = startGateRoot.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 0.38f);
            rect.anchorMax = new Vector2(0.5f, 0.38f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.sizeDelta = new Vector2(760f, 124f);

            startGateImage = startGateRoot.AddComponent<Image>();
            startGateImage.sprite = TitleArt.CreatePlank();   // the same wooden sign as the menu buttons
            startGateImage.type = Image.Type.Sliced;

            UIFactory.AddPressFeedback(startGateRoot);
            startGateButton = startGateRoot.AddComponent<Button>();
            startGateButton.targetGraphic = startGateImage;
            startGateButton.transition = Selectable.Transition.None;
            startGateButton.onClick.AddListener(OnStartGateClicked);

            startGateLabel = UIFactory.CreateText(startGateRoot.transform, "START GAME", 44, FontStyles.Bold, HudPalette.ButtonGold);
            UIFactory.ApplyOutline(startGateLabel, 0.3f, new Color32(30, 14, 4, 255));
            UIFactory.StretchFull(startGateLabel.rectTransform);
            startGateLabel.enableAutoSizing = true; // "SEARCHING FOR OPPONENT..." must fit on one line
            startGateLabel.fontSizeMin = 24f;
            startGateLabel.fontSizeMax = 44f;
            startGateLabel.textWrappingMode = TextWrappingModes.NoWrap;
            startGateLabel.margin = new Vector4(20f, 0f, 20f, 0f);

            // BACK: under the sign, goes to the player screen. It hides with the sign.
            var backGo = UIFactory.CreateRect("BackButton", startGateRoot.transform);
            var backRect = backGo.GetComponent<RectTransform>();
            backRect.anchoredPosition = new Vector2(0f, -(124f * 0.5f + 28f + 46f));
            backRect.sizeDelta = new Vector2(300f, 92f);
            var backImage = backGo.AddComponent<Image>();
            backImage.sprite = TitleArt.CreatePlank();
            backImage.type = Image.Type.Sliced;
            var backButton = backGo.AddComponent<Button>();
            backButton.targetGraphic = backImage;
            backButton.transition = Selectable.Transition.None;
            backButton.onClick.AddListener(OnBackClicked);
            UIFactory.AddPressFeedback(backGo);
            var backLabel = UIFactory.CreateText(backGo.transform, "< BACK", 46, FontStyles.Bold, HudPalette.Cream);
            UIFactory.ApplyOutline(backLabel, 0.3f, new Color32(30, 14, 4, 255));
            UIFactory.StretchFull(backLabel.rectTransform);
        }

        private StartGateState ComputeStartGateState()
        {
            if (matchState.MatchStarted) return StartGateState.Hidden;
            if (matchCountdown != null && matchCountdown.HasRequestedStart) return StartGateState.Hidden;
            if (matchSetup != null && !matchSetup.IsSetupComplete) return StartGateState.Hidden;

            // VS PLAYER is played on the board. Until this phone has the board role there is nothing to start.
            if (matchSetup != null && matchSetup.Mode == MatchSetup.GameMode.VsPlayer && !matchState.IsBoardClient) return StartGateState.NoBoard;

            bool opponentFound = arenaFrame == null || arenaFrame.HasLane;
            if (!opponentFound) return StartGateState.Searching;

            if (peerReadySync != null && peerReadySync.RequiresLockIn && !peerReadySync.HasLockedIn)
                return StartGateState.ReadyToLockIn;

            bool peerReady = peerReadySync == null || peerReadySync.PeerReady;
            if (!peerReady) return StartGateState.WaitingForPeer;

            return StartGateState.ReadyToStart;
        }

        private void ApplyStartGateState(StartGateState state)
        {
            bool interactable = state is StartGateState.ReadyToLockIn or StartGateState.ReadyToStart;
            startGateButton.interactable = interactable;
            startGateImage.color = interactable ? Color.white : new Color(0.6f, 0.6f, 0.6f, 1f);
            startGateLabel.text = state switch
            {
                StartGateState.NoBoard => "WAITING FOR THE BOARD...",
                StartGateState.Searching => "SEARCHING FOR OPPONENT...",
                StartGateState.ReadyToLockIn => "READY",
                StartGateState.WaitingForPeer => "WAITING FOR OPPONENT...",
                _ => "START GAME",
            };
        }

        // Back from the start screen (searching, waiting, ready) to the player screen: the same reset as quitting to the main
        // menu, except that the chosen mode stays. Picking a player again re-activates the CPU opponent or the board link.
        private void OnBackClicked()
        {
            matchState.ResetMatch();
            if (arenaFrame != null) arenaFrame.DebugForgetOpponent();
            if (matchSetup != null) matchSetup.BackToPlayerSelect();
            if (matchCountdown != null) matchCountdown.ResetCountdownGate();
            var networkSync = FindAnyObjectByType<MatchNetworkSync>();
            if (networkSync != null) networkSync.DebugReset();
            if (peerReadySync != null) peerReadySync.DebugReset();
        }

        private void OnStartGateClicked()
        {
            switch (ComputeStartGateState())
            {
                case StartGateState.ReadyToLockIn:
                    peerReadySync.LockIn();
                    break;
                case StartGateState.ReadyToStart:
                    if (matchCountdown != null) matchCountdown.RequestStart();
                    else matchState.StartMatch();
                    break;
            }
        }
    }
}
