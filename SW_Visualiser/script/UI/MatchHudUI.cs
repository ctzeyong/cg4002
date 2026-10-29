using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Networking;

namespace BridgeRush.UI
{
    public class MatchHudUI : MonoBehaviour
    {
        [SerializeField] private Canvas canvas;
        [SerializeField] private MatchState matchState;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private Camera hudCamera;
        [Tooltip("Optional. Explains why Start Game is still hidden in a VS PLAYER match.")]
        [SerializeField] private PeerReadySync peerReadySync;
        [Tooltip("Optional. Wired up, these let Play Again fully reset back to the mode-select screen.")]
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchCountdown matchCountdown;
        [SerializeField] private MatchNetworkSync matchNetworkSync;

        [Header("Damage feedback")]
        [SerializeField] private float damageFlashDurationSeconds = 0.35f;
        [SerializeField, Range(0f, 1f)] private float damageFlashMaxAlpha = 0.8f;
        [SerializeField] private float damageBorderThickness = 34f;
        [SerializeField] private float damageGlowThickness = 76f;

        private GameObject lockTowerButton;
        private Image lockTowerBg;
        private TextMeshProUGUI lockTowerText;
        private Sprite lockedSprite;
        private Sprite unlockedSprite;
        private bool lockSpriteApplied;

        private static readonly (string Symbol, string Action, int Mana, string Stats)[] CommandReference =
        {
            ("Chop", "Warrior", 2, "150 HP / 20 DPS - Melee"),
            ("Jab", "Rogue", 3, "120 HP / 35 DPS - Melee"),
            ("Circle", "Mage", 4, "90 HP / 30 DPS - Ranged"),
            ("Lightning", "Lightning Strike", 5, "180 burst - Area"),
            ("Punch", "Fireball", 6, "120 dmg + burning floor - Area"),
            ("Shield", "Tank", 6, "700 HP / 25 DPS - Melee"),
            ("Star", "Hero", 8, "450 HP / 70 DPS - Melee"),
        };

        private Image manaFill;
        private TextMeshProUGUI manaText;
        private Image ownFill;
        private Image opponentFill;

        private TextMeshProUGUI timerText;

        private GameObject commandsPanel;
        private bool commandsExpanded;
        private TextMeshProUGUI commandsToggleText;

        private Image dimOverlay;
        private TextMeshProUGUI trackingMessage;

        private RectTransform offscreenArrow;

        private CanvasGroup doubleManaGroup;
        private RectTransform doubleManaRect;
        private float doubleManaElapsed = -1f;
        private TextMeshProUGUI doubleManaBadge;
        private const float DoubleManaBannerSeconds = 4.5f;

        private CanvasGroup damageFlashGroup;
        private float damageFlashRemaining;
        private float previousOwnTowerHP;

        private void Awake()
        {
            if (canvas == null || matchState == null || arenaFrame == null)
            {
                Debug.LogError($"{nameof(MatchHudUI)}: needs a Canvas, a Match State and an Arena Frame.", this);
                enabled = false;
                return;
            }

            if (hudCamera == null) hudCamera = Camera.main;

            BuildStatusCluster();
            BuildTimer();
            BuildCommandsDrawer();
            BuildTrackingOverlay();
            BuildOffscreenArrow();
            BuildLockTowerButton();
            BuildResultOverlay();
            BuildDamageFlash();
            BuildDoubleManaBanner();
            previousOwnTowerHP = matchState.OwnTowerHP;
        }

        private void OnEnable()
        {
            if (matchState != null) matchState.DoubleManaStarted += OnDoubleManaStarted;
        }

        private void OnDisable()
        {
            if (matchState != null) matchState.DoubleManaStarted -= OnDoubleManaStarted;
        }

        private void Update()
        {
            UpdateStatusCluster();
            UpdateTimer();
            UpdateTrackingOverlay();
            UpdateOffscreenArrow();
            UpdateLockTowerButton();
            UpdateResultOverlay();
            UpdateDamageFlash();
            UpdateDoubleManaBanner();
        }

        // The big announcement when the last minute begins: it pops in, holds, then fades.
        private void BuildDoubleManaBanner()
        {
            var go = UIFactory.CreateRect("DoubleManaBanner", canvas.transform);
            doubleManaRect = go.GetComponent<RectTransform>();
            doubleManaRect.anchorMin = new Vector2(0.5f, 0.7f);
            doubleManaRect.anchorMax = new Vector2(0.5f, 0.7f);
            doubleManaRect.pivot = new Vector2(0.5f, 0.5f);
            doubleManaRect.sizeDelta = new Vector2(560f, 140f);

            var bg = go.AddComponent<Image>();
            bg.sprite = UITextureFactory.CreateRoundedPanel(72, 24, HudPalette.Wood, HudPalette.WoodDark, 4, HudPalette.Gold);
            bg.type = Image.Type.Sliced;
            bg.raycastTarget = false;

            var title = UIFactory.CreateText(go.transform, "2x MANA!", 58f, FontStyles.Bold, HudPalette.Gold);
            var titleRect = title.rectTransform;
            titleRect.anchorMin = new Vector2(0f, 0.42f);
            titleRect.anchorMax = new Vector2(1f, 1f);
            titleRect.offsetMin = Vector2.zero;
            titleRect.offsetMax = Vector2.zero;

            var subtitle = UIFactory.CreateText(go.transform, "Final minute: mana regenerates twice as fast", 22f, FontStyles.Normal, HudPalette.Cream);
            var subtitleRect = subtitle.rectTransform;
            subtitleRect.anchorMin = new Vector2(0f, 0.08f);
            subtitleRect.anchorMax = new Vector2(1f, 0.42f);
            subtitleRect.offsetMin = Vector2.zero;
            subtitleRect.offsetMax = Vector2.zero;

            doubleManaGroup = go.AddComponent<CanvasGroup>();
            doubleManaGroup.alpha = 0f;
            doubleManaGroup.interactable = false;
            doubleManaGroup.blocksRaycasts = false;

            // A small "x2" beside the mana bar for as long as the bonus lasts.
            var badgeParent = manaText.transform.parent;
            doubleManaBadge = UIFactory.CreateText(badgeParent, "x2", 34f, FontStyles.Bold, HudPalette.Gold);
            var badgeRect = doubleManaBadge.rectTransform;
            badgeRect.anchorMin = new Vector2(1f, 0.5f);
            badgeRect.anchorMax = new Vector2(1f, 0.5f);
            badgeRect.pivot = new Vector2(0f, 0.5f);
            badgeRect.anchoredPosition = new Vector2(10f, 0f);
            badgeRect.sizeDelta = new Vector2(70f, 44f);
            doubleManaBadge.alignment = TextAlignmentOptions.Left;
            doubleManaBadge.enabled = false;
        }

        private void OnDoubleManaStarted()
        {
            doubleManaElapsed = 0f;
            GameAudio.PlayAnnouncement();
            Haptics.Pulse(Haptics.Strength.Medium);
        }

        private void UpdateDoubleManaBanner()
        {
            if (doubleManaBadge != null)
            {
                bool active = matchState.DoubleManaActive;
                doubleManaBadge.enabled = active;
                if (active) doubleManaBadge.rectTransform.localScale = Vector3.one * (1f + 0.12f * Mathf.Sin(Time.unscaledTime * 6f));
            }

            if (doubleManaElapsed < 0f) return;
            doubleManaElapsed += Time.unscaledDeltaTime;
            if (doubleManaElapsed >= DoubleManaBannerSeconds)
            {
                doubleManaElapsed = -1f;
                doubleManaGroup.alpha = 0f;
                return;
            }

            float fadeIn = Mathf.Clamp01(doubleManaElapsed / 0.25f);
            float fadeOut = Mathf.Clamp01((DoubleManaBannerSeconds - doubleManaElapsed) / 0.7f);
            doubleManaGroup.alpha = Mathf.Min(fadeIn, fadeOut);
            float pop = doubleManaElapsed < 0.35f ? Mathf.Lerp(0.6f, 1.08f, doubleManaElapsed / 0.35f) : Mathf.Lerp(1.08f, 1f, Mathf.Clamp01((doubleManaElapsed - 0.35f) / 0.2f));
            float beat = 1f + 0.03f * Mathf.Sin(doubleManaElapsed * 10f);
            doubleManaRect.localScale = Vector3.one * pop * beat;
        }

        private void BuildDamageFlash()
        {
            var root = UIFactory.CreateRect("DamageFlash", canvas.transform);
            UIFactory.StretchFull(root.GetComponent<RectTransform>());

            damageFlashGroup = root.AddComponent<CanvasGroup>();
            damageFlashGroup.alpha = 0f;
            damageFlashGroup.interactable = false;
            damageFlashGroup.blocksRaycasts = false;

            Color glowColor = new(1f, 0.02f, 0.02f, 0.24f);
            Color edgeColor = new(1f, 0.02f, 0.02f, 0.92f);

            AddDamageEdge(root.transform, "GlowTop", Edge.Top, damageGlowThickness, glowColor);
            AddDamageEdge(root.transform, "GlowBottom", Edge.Bottom, damageGlowThickness, glowColor);
            AddDamageEdge(root.transform, "GlowLeft", Edge.Left, damageGlowThickness, glowColor);
            AddDamageEdge(root.transform, "GlowRight", Edge.Right, damageGlowThickness, glowColor);
            AddDamageEdge(root.transform, "EdgeTop", Edge.Top, damageBorderThickness, edgeColor);
            AddDamageEdge(root.transform, "EdgeBottom", Edge.Bottom, damageBorderThickness, edgeColor);
            AddDamageEdge(root.transform, "EdgeLeft", Edge.Left, damageBorderThickness, edgeColor);
            AddDamageEdge(root.transform, "EdgeRight", Edge.Right, damageBorderThickness, edgeColor);
        }

        private enum Edge { Top, Bottom, Left, Right }

        private static void AddDamageEdge(Transform parent, string objectName, Edge edge, float thickness, Color color)
        {
            var go = UIFactory.CreateRect(objectName, parent);
            var rect = go.GetComponent<RectTransform>();

            switch (edge)
            {
                case Edge.Top:
                    rect.anchorMin = new Vector2(0f, 1f);
                    rect.anchorMax = Vector2.one;
                    rect.pivot = new Vector2(0.5f, 1f);
                    rect.sizeDelta = new Vector2(0f, thickness);
                    break;
                case Edge.Bottom:
                    rect.anchorMin = Vector2.zero;
                    rect.anchorMax = new Vector2(1f, 0f);
                    rect.pivot = new Vector2(0.5f, 0f);
                    rect.sizeDelta = new Vector2(0f, thickness);
                    break;
                case Edge.Left:
                    rect.anchorMin = Vector2.zero;
                    rect.anchorMax = new Vector2(0f, 1f);
                    rect.pivot = new Vector2(0f, 0.5f);
                    rect.sizeDelta = new Vector2(thickness, 0f);
                    break;
                case Edge.Right:
                    rect.anchorMin = new Vector2(1f, 0f);
                    rect.anchorMax = Vector2.one;
                    rect.pivot = new Vector2(1f, 0.5f);
                    rect.sizeDelta = new Vector2(thickness, 0f);
                    break;
            }

            rect.anchoredPosition = Vector2.zero;
            var image = go.AddComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
        }

        private void UpdateDamageFlash()
        {
            float currentHP = matchState.OwnTowerHP;
            if (matchState.MatchStarted && currentHP < previousOwnTowerHP - 0.0001f)
            {
                damageFlashRemaining = Mathf.Max(0.01f, damageFlashDurationSeconds);
            }
            previousOwnTowerHP = currentHP;

            if (damageFlashRemaining <= 0f)
            {
                damageFlashGroup.alpha = 0f;
                return;
            }

            damageFlashRemaining = Mathf.Max(0f, damageFlashRemaining - Time.unscaledDeltaTime);
            float duration = Mathf.Max(0.01f, damageFlashDurationSeconds);
            float fade = damageFlashRemaining / duration;
            damageFlashGroup.alpha = damageFlashMaxAlpha * fade * fade;
        }

        private GameObject resultOverlayRoot;
        private TextMeshProUGUI resultText;
        private TextMeshProUGUI resultSubText;

        private void BuildResultOverlay()
        {
            var go = UIFactory.CreateRect("ResultOverlay", canvas.transform);
            var rect = go.GetComponent<RectTransform>();
            UIFactory.StretchFull(rect);

            var bg = go.AddComponent<Image>();
            bg.color = new Color(0f, 0f, 0f, 0.78f);
            bg.raycastTarget = false;

            resultText = UIFactory.CreateText(go.transform, string.Empty, 72, FontStyles.Bold, HudPalette.Gold);
            var resultRect = resultText.rectTransform;
            resultRect.anchorMin = new Vector2(0.1f, 0.5f);
            resultRect.anchorMax = new Vector2(0.9f, 0.65f);
            resultRect.offsetMin = Vector2.zero;
            resultRect.offsetMax = Vector2.zero;

            resultSubText = UIFactory.CreateText(go.transform, string.Empty, 24, FontStyles.Normal, HudPalette.Cream);
            var subRect = resultSubText.rectTransform;
            subRect.anchorMin = new Vector2(0.1f, 0.42f);
            subRect.anchorMax = new Vector2(0.9f, 0.5f);
            subRect.offsetMin = Vector2.zero;
            subRect.offsetMax = Vector2.zero;

            var buttonGo = UIFactory.CreateRect("PlayAgainButton", go.transform);
            var buttonRect = buttonGo.GetComponent<RectTransform>();
            buttonRect.anchorMin = new Vector2(0.5f, 0.32f);
            buttonRect.anchorMax = new Vector2(0.5f, 0.32f);
            buttonRect.pivot = new Vector2(0.5f, 0.5f);
            buttonRect.sizeDelta = new Vector2(320f, 84f);

            var buttonImage = buttonGo.AddComponent<Image>();
            buttonImage.sprite = UITextureFactory.CreateRoundedPanel(96, 18, HudPalette.Wood, HudPalette.WoodDark, 3, HudPalette.Gold);
            buttonImage.type = Image.Type.Sliced;

            var button = buttonGo.AddComponent<Button>();
            button.targetGraphic = buttonImage;
            button.onClick.AddListener(RestartToSetup);

            var buttonLabel = UIFactory.CreateText(buttonGo.transform, "PLAY AGAIN", 26, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(buttonLabel.rectTransform);

            resultOverlayRoot = go;
            resultOverlayRoot.SetActive(false);
        }

        private void RestartToSetup()
        {
            matchState.ResetMatch();
            arenaFrame.DebugForgetOpponent();
            matchSetup?.DebugReset();
            matchCountdown?.ResetCountdownGate();
            matchNetworkSync?.DebugReset();
        }

        private void UpdateResultOverlay()
        {
            if (!matchState.HasEnded)
            {
                if (resultOverlayRoot.activeSelf) resultOverlayRoot.SetActive(false);
                return;
            }

            if (!resultOverlayRoot.activeSelf) resultOverlayRoot.SetActive(true);

            switch (matchState.Result)
            {
                case MatchState.MatchResult.Win:
                    resultText.text = "VICTORY";
                    resultText.color = HudPalette.Gold;
                    break;
                case MatchState.MatchResult.Loss:
                    resultText.text = "DEFEAT";
                    resultText.color = HudPalette.Opponent;
                    break;
                default:
                    resultText.text = "DRAW";
                    resultText.color = HudPalette.Cream;
                    break;
            }

            resultSubText.text = $"{Mathf.CeilToInt(matchState.OwnTowerHP)} HP  vs  {Mathf.CeilToInt(matchState.OpponentTowerHP)} HP";
        }

        private void BuildLockTowerButton()
        {
            lockedSprite = UITextureFactory.CreateRoundedPanel(64, 12, HudPalette.Opponent, HudPalette.OpponentDeep, 2, HudPalette.GoldDark);
            unlockedSprite = UITextureFactory.CreateRoundedPanel(64, 12, HudPalette.Wood, HudPalette.WoodDark, 2, HudPalette.GoldDark);

            lockTowerButton = UIFactory.CreateRect("LockTowerButton", canvas.transform);
            var rect = lockTowerButton.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(0f, 1f);
            rect.pivot = new Vector2(0f, 1f);
            rect.anchoredPosition = new Vector2(20f, -170f);
            rect.sizeDelta = new Vector2(180f, 56f);

            lockTowerBg = lockTowerButton.AddComponent<Image>();
            lockTowerBg.type = Image.Type.Sliced;
            lockTowerBg.sprite = unlockedSprite;

            var button = lockTowerButton.AddComponent<Button>();
            button.targetGraphic = lockTowerBg;
            button.onClick.AddListener(() =>
            {
                if (matchState.OwnTowerLocked) matchState.UnlockOwnTower();
                else matchState.LockOwnTower();
            });

            lockTowerText = UIFactory.CreateText(lockTowerButton.transform, "LOCK TOWER", 16, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(lockTowerText.rectTransform);

            lockTowerButton.SetActive(false);
        }

        private void UpdateLockTowerButton()
        {
            lockTowerButton.SetActive(matchState.MatchStarted);
            if (!matchState.MatchStarted) return;

            bool locked = matchState.OwnTowerLocked;
            if (locked == lockSpriteApplied) return;

            lockSpriteApplied = locked;
            lockTowerText.text = locked ? "UNLOCK TOWER" : "LOCK TOWER";
            lockTowerBg.sprite = locked ? lockedSprite : unlockedSprite;
        }

        private const float BarInset = 4f;
        private const float BarFillSpeed = 0.8f;
        private const float BarWidth = 320f;

        private float manaShown;
        private float ownShown;
        private float opponentShown;

        private void BuildStatusCluster()
        {
            var root = UIFactory.CreateRect("StatusCluster", canvas.transform);
            var rect = root.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0f, 0.5f);
            rect.anchorMax = new Vector2(0f, 0.5f);
            rect.pivot = new Vector2(0f, 0.5f);
            rect.anchoredPosition = new Vector2(14f, -120f);

            var layout = root.AddComponent<VerticalLayoutGroup>();
            layout.spacing = 8f;
            layout.childControlWidth = false;
            layout.childControlHeight = false;
            var fitter = root.AddComponent<ContentSizeFitter>();
            fitter.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            manaFill = BuildBar(root.transform, BarWidth, 54f, HudPalette.Mana, HudPalette.ManaDeep, "MANA", out manaText);
            ownFill = BuildBar(root.transform, BarWidth, 40f, HudPalette.Own, HudPalette.OwnDeep, "YOU", out _);
            opponentFill = BuildBar(root.transform, BarWidth, 40f, HudPalette.Opponent, HudPalette.OpponentDeep, "ENEMY", out _);
        }

        private Image BuildBar(Transform parent, float width, float height, Color fillColor, Color fillDeep,
            string name, out TextMeshProUGUI valueLabel)
        {
            var go = UIFactory.CreateRect("Bar", parent);
            var rect = go.GetComponent<RectTransform>();
            rect.sizeDelta = new Vector2(width, height);
            var layoutElement = go.AddComponent<LayoutElement>();
            layoutElement.preferredWidth = width;
            layoutElement.preferredHeight = height;

            var track = go.AddComponent<Image>();
            track.sprite = UITextureFactory.CreateRoundedPanel(64, (int)(height * 0.5f), new Color(0.07f, 0.06f, 0.05f, 0.85f), new Color(0.14f, 0.11f, 0.08f, 0.85f), 3, HudPalette.GoldDark);
            track.type = Image.Type.Sliced;
            track.raycastTarget = false;

            var inner = UIFactory.CreateRect("Inner", go.transform);
            var innerRect = inner.GetComponent<RectTransform>();
            UIFactory.StretchFull(innerRect);
            innerRect.offsetMin = new Vector2(BarInset, BarInset);
            innerRect.offsetMax = new Vector2(-BarInset, -BarInset);
            int innerRadius = (int)((height - BarInset * 2f) * 0.5f);

            var fillGo = UIFactory.CreateRect("Fill", inner.transform);
            var fillRect = fillGo.GetComponent<RectTransform>();
            UIFactory.StretchFull(fillRect);
            var fillImage = fillGo.AddComponent<Image>();
            fillImage.sprite = UITextureFactory.CreateRoundedPanel(64, innerRadius, fillColor, fillDeep, 0, Color.clear);
            fillImage.type = Image.Type.Sliced;
            fillImage.raycastTarget = false;

            var gloss = UIFactory.CreateRect("Gloss", fillGo.transform);
            var glossRect = gloss.GetComponent<RectTransform>();
            glossRect.anchorMin = new Vector2(0f, 0.5f);
            glossRect.anchorMax = new Vector2(1f, 1f);
            glossRect.offsetMin = new Vector2(innerRadius * 0.4f, 0f);
            glossRect.offsetMax = new Vector2(-innerRadius * 0.4f, -2f);
            var glossImage = gloss.AddComponent<Image>();
            glossImage.sprite = UITextureFactory.CreateRoundedPanel(64, innerRadius / 2, new Color(1f, 1f, 1f, 0.28f), new Color(1f, 1f, 1f, 0.06f), 0, Color.clear);
            glossImage.type = Image.Type.Sliced;
            glossImage.raycastTarget = false;

            var nameLabel = UIFactory.CreateText(go.transform, name, height * 0.4f, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(nameLabel.rectTransform);
            nameLabel.alignment = TextAlignmentOptions.Left;
            nameLabel.margin = new Vector4(height * 0.5f, 0f, 0f, 0f);

            if (name == "MANA")
            {
                valueLabel = UIFactory.CreateText(go.transform, "0/0", height * 0.5f, FontStyles.Bold, HudPalette.Cream);
                UIFactory.StretchFull(valueLabel.rectTransform);
                valueLabel.alignment = TextAlignmentOptions.Right;
                valueLabel.margin = new Vector4(0f, 0f, height * 0.5f, 0f);
            }
            else
            {
                valueLabel = null;
            }

            return fillImage;
        }

        private static void SetBar(Image fill, float t)
        {
            fill.enabled = t > 0.001f;
            fill.rectTransform.anchorMax = new Vector2(Mathf.Clamp01(t), 1f);
        }

        private void UpdateStatusCluster()
        {
            float step = Time.deltaTime * BarFillSpeed;

            float manaT = matchState.ManaMax > 0f ? matchState.ManaCurrent / matchState.ManaMax : 0f;
            manaShown = Mathf.MoveTowards(manaShown, Mathf.Clamp01(manaT), step);
            SetBar(manaFill, manaShown);
            manaText.text = $"{Mathf.FloorToInt(matchState.ManaCurrent)}/{matchState.ManaMax:0}";
            float pulse = 0.85f + Mathf.PingPong(Time.time * 0.8f, 0.3f);
            manaFill.color = matchState.DoubleManaActive ? new Color(pulse, pulse * 0.88f, pulse * 0.55f, 1f) : new Color(pulse, pulse, pulse, 1f);

            float ownT = matchState.OwnTowerMaxHP > 0f ? Mathf.Clamp01(matchState.OwnTowerHP / matchState.OwnTowerMaxHP) : 0f;
            float opponentT = matchState.OpponentTowerMaxHP > 0f ? Mathf.Clamp01(matchState.OpponentTowerHP / matchState.OpponentTowerMaxHP) : 0f;
            ownShown = Mathf.MoveTowards(ownShown, ownT, step);
            opponentShown = Mathf.MoveTowards(opponentShown, opponentT, step);
            SetBar(ownFill, ownShown);
            SetBar(opponentFill, opponentShown);
        }

        private void BuildTimer()
        {
            var go = UIFactory.CreateRect("Timer", canvas.transform);
            var rect = go.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 1f);
            rect.anchorMax = new Vector2(0.5f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, -170f);
            rect.sizeDelta = new Vector2(190f, 64f);

            var bg = go.AddComponent<Image>();
            bg.sprite = UITextureFactory.CreateRoundedPanel(72, 16, HudPalette.Wood, HudPalette.WoodDark, 3, HudPalette.Gold);
            bg.type = Image.Type.Sliced;

            timerText = UIFactory.CreateText(go.transform, "0:00", 30, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(timerText.rectTransform);
        }

        private void UpdateTimer()
        {
            int remaining = Mathf.CeilToInt(matchState.MatchTimeRemainingSeconds);
            timerText.text = $"{remaining / 60:0}:{remaining % 60:00}";
            timerText.color = remaining <= 30 ? new Color(1f, 0.4f, 0.35f) : HudPalette.Cream;
        }

        private void BuildCommandsDrawer()
        {
            var toggleGo = UIFactory.CreateRect("CommandsToggle", canvas.transform);
            var toggleRect = toggleGo.GetComponent<RectTransform>();
            toggleRect.anchorMin = new Vector2(1f, 1f);
            toggleRect.anchorMax = new Vector2(1f, 1f);
            toggleRect.pivot = new Vector2(1f, 1f);
            toggleRect.anchoredPosition = new Vector2(-20f, -170f);
            toggleRect.sizeDelta = new Vector2(220f, 56f);

            var toggleBg = toggleGo.AddComponent<Image>();
            toggleBg.sprite = UITextureFactory.CreateRoundedPanel(64, 12, HudPalette.Wood, HudPalette.WoodDark, 2, HudPalette.GoldDark);
            toggleBg.type = Image.Type.Sliced;
            var toggleButton = toggleGo.AddComponent<Button>();
            toggleButton.targetGraphic = toggleBg;
            toggleButton.onClick.AddListener(() =>
            {
                commandsExpanded = !commandsExpanded;
                commandsPanel.SetActive(commandsExpanded);
                commandsToggleText.text = commandsExpanded ? "COMMANDS v" : "COMMANDS >";
            });
            commandsToggleText = UIFactory.CreateText(toggleGo.transform, "COMMANDS >", 20, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(commandsToggleText.rectTransform);

            commandsPanel = UIFactory.CreateRect("CommandsPanel", canvas.transform);
            var panelRect = commandsPanel.GetComponent<RectTransform>();
            panelRect.anchorMin = new Vector2(1f, 1f);
            panelRect.anchorMax = new Vector2(1f, 1f);
            panelRect.pivot = new Vector2(1f, 1f);
            panelRect.anchoredPosition = new Vector2(-20f, -232f);
            panelRect.sizeDelta = new Vector2(460f, CommandReference.Length * 36f + 20f);

            var panelBg = commandsPanel.AddComponent<Image>();
            panelBg.sprite = UITextureFactory.CreateRoundedPanel(64, 14, HudPalette.Wood, HudPalette.WoodDark, 2, HudPalette.GoldDark);
            panelBg.type = Image.Type.Sliced;

            var layout = commandsPanel.AddComponent<VerticalLayoutGroup>();
            layout.padding = new RectOffset(14, 14, 10, 10);
            layout.spacing = 3f;
            layout.childControlWidth = true;
            layout.childControlHeight = false;

            foreach (var entry in CommandReference)
            {
                var rowGo = UIFactory.CreateRect("Row", commandsPanel.transform);
                var rowLayoutElement = rowGo.AddComponent<LayoutElement>();
                rowLayoutElement.preferredHeight = 33f;

                var row = UIFactory.CreateText(rowGo.transform, $"{entry.Symbol} -> {entry.Action}  ({entry.Stats})    [{entry.Mana}]",
                    16f, FontStyles.Normal, HudPalette.Cream);
                row.alignment = TextAlignmentOptions.Left;
                UIFactory.StretchFull(row.rectTransform);
            }

            commandsPanel.SetActive(false);
        }

        private void BuildTrackingOverlay()
        {
            var go = UIFactory.CreateRect("TrackingOverlay", canvas.transform);
            var rect = go.GetComponent<RectTransform>();
            UIFactory.StretchFull(rect);

            dimOverlay = go.AddComponent<Image>();
            dimOverlay.color = new Color(0f, 0f, 0f, 0f);
            dimOverlay.raycastTarget = false;

            trackingMessage = UIFactory.CreateText(go.transform, string.Empty, 28, FontStyles.Bold, HudPalette.Cream);
            var msgRect = trackingMessage.rectTransform;
            msgRect.anchorMin = new Vector2(0.08f, 0.58f);
            msgRect.anchorMax = new Vector2(0.92f, 0.74f);
            msgRect.offsetMin = Vector2.zero;
            msgRect.offsetMax = Vector2.zero;
            trackingMessage.textWrappingMode = TextWrappingModes.Normal;
        }

        private void UpdateTrackingOverlay()
        {
            if (matchSetup != null && !matchSetup.IsSetupComplete)
            {
                SetOverlay(0f, string.Empty);
                return;
            }

            if (!arenaFrame.HasLane)
            {
                SetOverlay(0.55f, "Searching for opponent marker...");
            }
            else if (arenaFrame.IsLongStale)
            {
                SetOverlay(0.45f, "Opponent lost for a while - last known position may be very out of date");
            }
            else if (!arenaFrame.IsOpponentTracked)
            {
                SetOverlay(0.3f, "Opponent tracking lost - holding last known position");
            }
            else if (!matchState.MatchStarted && peerReadySync != null && !peerReadySync.PeerReady)
            {
                SetOverlay(0.3f, "Waiting for the other player to also find you...");
            }
            else if (!matchState.MatchStarted)
            {
                SetOverlay(0f, "Opponent found - walk to your spot, then press Start Game");
            }
            else
            {
                SetOverlay(0f, string.Empty);
            }  
        }

        private void SetOverlay(float dimAlpha, string message)
        {
            dimOverlay.color = new Color(0f, 0f, 0f, dimAlpha);
            trackingMessage.text = message;
        }

        private void BuildOffscreenArrow()
        {
            var go = UIFactory.CreateRect("OffscreenArrow", canvas.transform);
            offscreenArrow = go.GetComponent<RectTransform>();
            offscreenArrow.sizeDelta = new Vector2(56f, 56f);

            var text = UIFactory.CreateText(go.transform, ">", 40, FontStyles.Bold, HudPalette.Gold);
            UIFactory.StretchFull(text.rectTransform);
            go.SetActive(false);
        }

        private void UpdateOffscreenArrow()
        {
            if (!arenaFrame.HasLane || hudCamera == null)
            {
                offscreenArrow.gameObject.SetActive(false);
                return;
            }

            var lane = arenaFrame.Lane;
            Vector3 opponentWorldPos = lane.Origin + lane.Direction * lane.Length;
            Vector3 screenPos = hudCamera.WorldToScreenPoint(opponentWorldPos);
            bool behindCamera = screenPos.z < 0f;

            bool onScreen = !behindCamera &&
                screenPos.x >= 0f && screenPos.x <= Screen.width &&
                screenPos.y >= 0f && screenPos.y <= Screen.height;
            if (onScreen)
            {
                offscreenArrow.gameObject.SetActive(false);
                return;
            }

            offscreenArrow.gameObject.SetActive(true);

            Vector2 point = new(screenPos.x, screenPos.y);
            if (behindCamera)
            {
                point = new Vector2(Screen.width - point.x, Screen.height - point.y);
            }

            Vector2 center = new(Screen.width / 2f, Screen.height / 2f);
            Vector2 direction = (point - center).normalized;
            if (direction.sqrMagnitude < 0.0001f) direction = Vector2.right;

            const float margin = 60f;
            float halfW = Screen.width / 2f - margin;
            float halfH = Screen.height / 2f - margin;
            float scaleX = Mathf.Abs(direction.x) > 0.0001f ? halfW / Mathf.Abs(direction.x) : float.MaxValue;
            float scaleY = Mathf.Abs(direction.y) > 0.0001f ? halfH / Mathf.Abs(direction.y) : float.MaxValue;
            Vector2 edgePoint = center + direction * Mathf.Min(scaleX, scaleY);

            offscreenArrow.anchorMin = new Vector2(edgePoint.x / Screen.width, edgePoint.y / Screen.height);
            offscreenArrow.anchorMax = offscreenArrow.anchorMin;
            offscreenArrow.pivot = new Vector2(0.5f, 0.5f);
            offscreenArrow.anchoredPosition = Vector2.zero;
            offscreenArrow.localRotation = Quaternion.Euler(0f, 0f, Mathf.Atan2(direction.y, direction.x) * Mathf.Rad2Deg);
        }
    }
}
