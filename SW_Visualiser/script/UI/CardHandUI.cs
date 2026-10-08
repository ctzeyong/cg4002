using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
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

        private enum StartGateState { Hidden, Searching, ReadyToLockIn, WaitingForPeer, ReadyToStart }

        private class CardEntry
        {
            public System.Func<float> GetCost;
            public System.Action Cast;
            public Image Glow;
            public Image Panel;
            public TextMeshProUGUI CostText;
        }

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
        }

        private void Update()
        {
            var gateState = ComputeStartGateState();
            startGateRoot.SetActive(gateState != StartGateState.Hidden);
            if (gateState != StartGateState.Hidden) ApplyStartGateState(gateState);

            bool started = matchState.MatchStarted;
            handRoot.gameObject.SetActive(started);
            if (!started) return;

            foreach (var card in cards)
            {
                float cost = card.GetCost();
                bool ready = matchState.ManaCurrent >= cost;

                card.CostText.text = cost.ToString("0");

                if (ready)
                {
                    float pulse = 0.55f + Mathf.PingPong(Time.time * 1.4f, 0.45f);
                    var glowColor = card.Glow.color;
                    glowColor.a = pulse;
                    card.Glow.color = glowColor;
                    card.Panel.color = Color.white;
                }
                else
                {
                    var glowColor = card.Glow.color;
                    glowColor.a = 0f;
                    card.Glow.color = glowColor;
                    card.Panel.color = new Color(0.55f, 0.55f, 0.55f, 1f);
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
            handRoot.anchoredPosition = new Vector2(0f, HandBottomMargin);

            var layout = handGo.AddComponent<HorizontalLayoutGroup>();
            layout.spacing = CardSpacing;
            layout.childAlignment = TextAnchor.LowerCenter;
            layout.childControlWidth = false;
            layout.childControlHeight = false;
            var fitter = handGo.AddComponent<ContentSizeFitter>();
            fitter.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            AddUnitCard(handRoot, "O", "Warrior", UnitType.Warrior);
            AddUnitCard(handRoot, "^", "Rogue", UnitType.Rogue);
            AddUnitCard(handRoot, "~", "Mage", UnitType.Mage);
            AddUnitCard(handRoot, "^^", "Tank", UnitType.Tank);
            AddUnitCard(handRoot, "*", "Hero", UnitType.Hero);

            AddSpellCard(handRoot, "Z", "Lightning", "180 dmg",
                () => spellCaster.LightningStrikeManaCost, () => spellCaster.CastLightningStrike());
            AddSpellCard(handRoot, "oo", "Overcharge", "8s buff",
                () => spellCaster.OverchargeManaCost, () => spellCaster.CastOvercharge());
        }

        private void AddUnitCard(Transform parent, string glyph, string name, UnitType type)
        {
            System.Func<float> getCost = () =>
                unitStatsTable.TryGetEntry(type, out var e) ? e.ManaCost : 0f;
            System.Func<string> getStats = () =>
                unitStatsTable.TryGetEntry(type, out var e2)
                    ? $"HP {e2.MaxHP:0}  DPS {e2.AttackDps:0}"
                    : string.Empty;

            var entry = BuildCard(parent, glyph, name, getStats, isSpell: false);
            entry.GetCost = getCost;
            entry.Cast = () => matchState.SpawnUnit(type);
            cards.Add(entry);
        }

        private void AddSpellCard(Transform parent, string glyph, string name, string statsLine,
            System.Func<float> getCost, System.Action cast)
        {
            var entry = BuildCard(parent, glyph, name, () => statsLine, isSpell: true);
            entry.GetCost = getCost;
            entry.Cast = cast;
            cards.Add(entry);
        }

        private CardEntry BuildCard(Transform parent, string glyph, string name, System.Func<string> getStats, bool isSpell)
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

            var panelGo = UIFactory.CreateRect("Panel", cardRect);
            var panelRect = panelGo.GetComponent<RectTransform>();
            panelRect.anchorMin = Vector2.zero;
            panelRect.anchorMax = Vector2.one;
            panelRect.offsetMin = Vector2.zero;
            panelRect.offsetMax = Vector2.zero;
            var panelImage = panelGo.AddComponent<Image>();
            panelImage.sprite = UITextureFactory.CreateRoundedPanel(96, 14,
                Color.Lerp(HudPalette.Wood, accentDeep, 0.12f), HudPalette.WoodDark, 3, accent);
            panelImage.type = Image.Type.Sliced;

            var button = cardGo.AddComponent<Button>();
            button.targetGraphic = panelImage;
            button.transition = Selectable.Transition.None;

            var costGo = UIFactory.CreateRect("Cost", cardRect);
            var costRect = costGo.GetComponent<RectTransform>();
            costRect.anchorMin = new Vector2(0f, 1f);
            costRect.anchorMax = new Vector2(0f, 1f);
            costRect.pivot = new Vector2(0.5f, 0.5f);
            costRect.anchoredPosition = new Vector2(0f, 0f);
            costRect.sizeDelta = new Vector2(40f, 40f);
            var costImage = costGo.AddComponent<Image>();
            costImage.sprite = UITextureFactory.CreateRoundedPanel(48, 24, HudPalette.Mana, HudPalette.ManaDeep, 2, Color.white);
            costImage.type = Image.Type.Sliced;
            var costText = UIFactory.CreateText(costGo.transform, "0", 20, FontStyles.Bold, new Color(0.02f, 0.2f, 0.24f));
            UIFactory.StretchFull(costText.rectTransform);

            var glyphText = UIFactory.CreateText(cardRect, glyph, 44, FontStyles.Normal, HudPalette.Cream);
            var glyphRect = glyphText.rectTransform;
            glyphRect.anchorMin = new Vector2(0f, 0.55f);
            glyphRect.anchorMax = new Vector2(1f, 1f);
            glyphRect.offsetMin = Vector2.zero;
            glyphRect.offsetMax = Vector2.zero;

            var nameText = UIFactory.CreateText(cardRect, name, 15, FontStyles.Bold, HudPalette.Cream);
            var nameRect = nameText.rectTransform;
            nameRect.anchorMin = new Vector2(0f, 0.32f);
            nameRect.anchorMax = new Vector2(1f, 0.5f);
            nameRect.offsetMin = new Vector2(2f, 0f);
            nameRect.offsetMax = new Vector2(-2f, 0f);

            var statsText = UIFactory.CreateText(cardRect, getStats(), 12f, FontStyles.Normal, new Color(0.9f, 0.85f, 0.7f));
            var statsRect = statsText.rectTransform;
            statsRect.anchorMin = new Vector2(0f, 0f);
            statsRect.anchorMax = new Vector2(1f, 0.28f);
            statsRect.offsetMin = new Vector2(2f, 2f);
            statsRect.offsetMax = new Vector2(-2f, 0f);

            var entry = new CardEntry
            {
                Glow = glowImage,
                Panel = panelImage,
                CostText = costText
            };

            button.onClick.AddListener(() => entry.Cast?.Invoke());

            return entry;
        }

        private void BuildStartGate()
        {
            startGateRoot = UIFactory.CreateRect("StartGate", canvas.transform);
            var rect = startGateRoot.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 0.38f);
            rect.anchorMax = new Vector2(0.5f, 0.38f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.sizeDelta = new Vector2(420f, 110f);

            startGateImage = startGateRoot.AddComponent<Image>();
            startGateImage.sprite = UITextureFactory.CreateRoundedPanel(96, 18,
                new Color(0.22f, 0.58f, 0.3f), new Color(0.09f, 0.35f, 0.16f), 3, new Color(0.85f, 0.98f, 0.88f));
            startGateImage.type = Image.Type.Sliced;

            startGateButton = startGateRoot.AddComponent<Button>();
            startGateButton.targetGraphic = startGateImage;
            startGateButton.transition = Selectable.Transition.None;
            startGateButton.onClick.AddListener(OnStartGateClicked);

            startGateLabel = UIFactory.CreateText(startGateRoot.transform, "START GAME", 30, FontStyles.Bold, new Color(0.03f, 0.16f, 0.06f));
            UIFactory.StretchFull(startGateLabel.rectTransform);
        }

        private StartGateState ComputeStartGateState()
        {
            if (matchState.MatchStarted) return StartGateState.Hidden;
            if (matchCountdown != null && matchCountdown.HasRequestedStart) return StartGateState.Hidden;

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
                StartGateState.Searching => "SEARCHING FOR OPPONENT...",
                StartGateState.ReadyToLockIn => "READY",
                StartGateState.WaitingForPeer => "WAITING FOR OPPONENT...",
                _ => "START GAME",
            };
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
