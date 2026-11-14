using TMPro;
using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;
using BridgeRush.Match;
using BridgeRush.Tracking;
using BridgeRush.Networking;
using BridgeRush.Input;

namespace BridgeRush.UI
{
    public class MatchHudUI : MonoBehaviour
    {
        [SerializeField] private Canvas canvas;

        // For the parts of the HUD that build themselves on the same canvas (the tutorial coach).
        public Canvas HudCanvas => canvas;
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

        // The tower is locked and unlocked by shaking the phone. This banner shows the state (see BuildLockBanner).
        private readonly TowerShakeInput shakeInput = new TowerShakeInput();
        private CanvasGroup lockBannerGroup;
        private RectTransform lockBannerRect;
        private Image lockBannerBg;
        private TextMeshProUGUI lockBannerText;
        private Sprite lockedSprite;
        private Sprite unlockedSprite;
        private string lockNote = "";
        private float lockNoteUntil = -1f;
        private float lockPopStart = -10f;
        private const float LockNoteSeconds = 1.8f;

        // Everything at the top hangs from topRoot, which is moved down by the phone's notch (HudLayout.SafeTop). The timer and
        // the two tower-HP bars live in matchTopGroup, which only shows once a match is running.
        private RectTransform topRoot;
        private RectTransform matchTopGroup;
        private RectTransform manaBarRoot;
        private Image manaFill;
        private TextMeshProUGUI manaText;
        private Image ownFill;
        private Image opponentFill;
        private TextMeshProUGUI ownHpText;
        private TextMeshProUGUI opponentHpText;

        private TextMeshProUGUI timerText;

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

            // A soft dark edge over the camera feed, under everything else, so the HUD reads and the arena is framed.
            var vignetteGo = UIFactory.CreateRect("Vignette", canvas.transform);
            UIFactory.StretchFull(vignetteGo.GetComponent<RectTransform>());
            var vignetteImage = vignetteGo.AddComponent<Image>();
            vignetteImage.sprite = UITextureFactory.CreateVignette();
            vignetteImage.raycastTarget = false;

            var topGo = UIFactory.CreateRect("TopSafeArea", canvas.transform);
            topRoot = topGo.GetComponent<RectTransform>();
            UIFactory.StretchFull(topRoot);
            var groupGo = UIFactory.CreateRect("MatchTop", topRoot);
            matchTopGroup = groupGo.GetComponent<RectTransform>();
            UIFactory.StretchFull(matchTopGroup);

            BuildStatusCluster();
            BuildTimer();
            BuildDebugButton();
            BuildTrackingOverlay();
            BuildOffscreenArrow();
            BuildLockBanner();
            BuildResultOverlay();
            BuildDamageFlash();
            BuildDoubleManaBanner();
            BuildOptionsWindow();
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
            topRoot.offsetMax = new Vector2(0f, -HudLayout.SafeTop(canvas));
            manaBarRoot.anchoredPosition = new Vector2(0f, HudLayout.ManaBarBottom(canvas));
            float hpBottom = HudLayout.HpBarsBottom(canvas);
            ownHpRoot.anchoredPosition = new Vector2(ownHpRoot.anchoredPosition.x, hpBottom);
            opponentHpRoot.anchoredPosition = new Vector2(opponentHpRoot.anchoredPosition.x, hpBottom);
            bool inMatch = matchState.MatchStarted;
            if (matchTopGroup.gameObject.activeSelf != inMatch) matchTopGroup.gameObject.SetActive(inMatch);
            if (manaBarRoot.gameObject.activeSelf != inMatch) manaBarRoot.gameObject.SetActive(inMatch);
            UpdateStatusCluster();
            UpdateTimer();
            if (optionsWindow != null && optionsWindow.IsOpen) optionsWindow.Refresh();
            UpdateTrackingOverlay();
            UpdateOffscreenArrow();
            UpdateLockBanner();
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
            bg.sprite = CardArt.Panel(CardArt.Gold);
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
        private Image resultBackdrop;
        private RectTransform resultCrown;
        private Image resultCrownImage;
        private float resultShownAt = -10f;
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
            resultBackdrop = bg;

            resultCrown = UIFactory.CreateCrown(go.transform, "ResultCrown", 300f, HudPalette.ButtonGold);
            resultCrown.anchorMin = resultCrown.anchorMax = new Vector2(0.5f, 0.74f);
            resultCrownImage = resultCrown.GetComponent<Image>();

            resultText = UIFactory.CreateText(go.transform, string.Empty, 96, FontStyles.Bold, HudPalette.Gold);
            var resultRect = resultText.rectTransform;
            resultRect.anchorMin = new Vector2(0.1f, 0.5f);
            resultRect.anchorMax = new Vector2(0.9f, 0.65f);
            resultRect.offsetMin = Vector2.zero;
            resultRect.offsetMax = Vector2.zero;

            resultSubText = UIFactory.CreateText(go.transform, string.Empty, 38, FontStyles.Normal, HudPalette.Cream);
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
            buttonRect.sizeDelta = new Vector2(460f, 112f);

            var buttonImage = buttonGo.AddComponent<Image>();
            buttonImage.sprite = CardArt.Button(CardArt.Gold);
            buttonImage.type = Image.Type.Sliced;

            var button = buttonGo.AddComponent<Button>();
            button.targetGraphic = buttonImage;
            button.onClick.AddListener(RestartToSetup);

            var buttonLabel = UIFactory.CreateText(buttonGo.transform, "PLAY AGAIN", 40, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(buttonLabel.rectTransform);
            UIFactory.AddPressFeedback(buttonGo);

            resultOverlayRoot = go;
            resultOverlayRoot.SetActive(false);
        }

        private void RestartToSetup()
        {
            // On the board engine Play Again is the rematch press: the next match starts when both players have pressed it.
            if (matchState.IsBoardClient && matchState.HasEnded)
            {
                matchState.BoardPressRematch();
                return;
            }
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

            if (!resultOverlayRoot.activeSelf)
            {
                resultOverlayRoot.SetActive(true);
                resultShownAt = Time.unscaledTime;
                Haptics.Pulse(matchState.Result == MatchState.MatchResult.Win ? Haptics.Strength.Heavy : Haptics.Strength.Medium);
            }

            Color tint;
            switch (matchState.Result)
            {
                case MatchState.MatchResult.Win:
                    resultText.text = "VICTORY";
                    resultText.color = HudPalette.Gold;
                    tint = new Color(0.16f, 0.1f, 0f, 0.84f);   // warm gold-brown
                    break;
                case MatchState.MatchResult.Loss:
                    resultText.text = "DEFEAT";
                    resultText.color = HudPalette.Opponent;
                    tint = new Color(0.17f, 0.01f, 0.01f, 0.86f); // dark red
                    break;
                default:
                    resultText.text = "DRAW";
                    resultText.color = HudPalette.Cream;
                    tint = new Color(0f, 0f, 0f, 0.78f);
                    break;
            }

            // The word slams in with an overshoot, then breathes; the backdrop fades to the result's colour and the rest follows.
            float age = Time.unscaledTime - resultShownAt;
            float t = Mathf.Clamp01(age / 0.55f);
            float back = 1f + 2.70158f * Mathf.Pow(t - 1f, 3f) + 1.70158f * Mathf.Pow(t - 1f, 2f); // ease-out-back
            float breathe = age > 0.55f ? 1f + 0.025f * Mathf.Sin((age - 0.55f) * 2.2f) : 1f;
            float scale = Mathf.Max(0.01f, back) * breathe;
            resultText.rectTransform.localScale = new Vector3(scale, scale, 1f);
            // The crown drops in a beat after the word: gold for a win, a dim red one for a loss, grey for a draw.
            float crownScale = Mathf.Max(0.01f, 1f + 2.70158f * Mathf.Pow(Mathf.Clamp01((age - 0.15f) / 0.55f) - 1f, 3f) + 1.70158f * Mathf.Pow(Mathf.Clamp01((age - 0.15f) / 0.55f) - 1f, 2f));
            resultCrown.localScale = new Vector3(crownScale, crownScale, 1f);
            resultCrown.localRotation = Quaternion.Euler(0f, 0f, 4f * Mathf.Sin(age * 1.6f));
            resultCrownImage.color = matchState.Result == MatchState.MatchResult.Win ? HudPalette.ButtonGold
                : matchState.Result == MatchState.MatchResult.Loss ? new Color(0.75f, 0.28f, 0.25f) : new Color(0.7f, 0.7f, 0.7f);
            resultBackdrop.color = Color.Lerp(new Color(tint.r, tint.g, tint.b, 0f), tint, Mathf.Clamp01(age / 0.4f));
            float follow = Mathf.Clamp01((age - 0.45f) / 0.35f);
            resultSubText.color = new Color(HudPalette.Cream.r, HudPalette.Cream.g, HudPalette.Cream.b, follow);

            resultSubText.text = $"{Mathf.CeilToInt(matchState.OwnTowerHP)} HP  vs  {Mathf.CeilToInt(matchState.OpponentTowerHP)} HP";
        }

        private void BuildLockBanner()
        {
            lockedSprite = CardArt.Panel(CardArt.Fireball);
            unlockedSprite = CardArt.Panel(CardArt.Unit);

            var go = UIFactory.CreateRect("TowerLockBanner", topRoot);
            lockBannerRect = go.GetComponent<RectTransform>();
            lockBannerRect.anchorMin = new Vector2(0f, 1f);
            lockBannerRect.anchorMax = new Vector2(0f, 1f);
            lockBannerRect.pivot = new Vector2(0f, 1f);
            lockBannerRect.anchoredPosition = new Vector2(20f, -112f);
            lockBannerRect.sizeDelta = new Vector2(430f, 104f);

            lockBannerBg = go.AddComponent<Image>();
            lockBannerBg.type = Image.Type.Sliced;
            lockBannerBg.sprite = lockedSprite;
            lockBannerBg.raycastTarget = false;

            lockBannerGroup = go.AddComponent<CanvasGroup>();
            lockBannerGroup.alpha = 0f;
            lockBannerGroup.interactable = false;
            lockBannerGroup.blocksRaycasts = false;

            lockBannerText = UIFactory.CreateText(go.transform, "", 34, FontStyles.Bold, HudPalette.Cream);
            lockBannerText.raycastTarget = false;
            UIFactory.StretchFull(lockBannerText.rectTransform);
        }

        // Shake to lock or unlock, once the match is running. The banner stays up while locked; an unlock or a refused lock
        // shows a short note and fades.
        private void UpdateLockBanner()
        {
            bool live = matchState.MatchStarted && !matchState.Paused;
            if (!live) shakeInput.Reset();
            else if (shakeInput.Poll()) ToggleTowerLock();

            bool locked = matchState.OwnTowerLocked;
            float now = Time.unscaledTime;
            bool noteShowing = now < lockNoteUntil;

            if (!matchState.MatchStarted || (!locked && !noteShowing))
            {
                lockBannerGroup.alpha = 0f;
                return;
            }

            if (locked)
            {
                lockBannerBg.sprite = lockedSprite;
                lockBannerText.text = "TOWER LOCKED\n<size=70%><color=#E8D9B0>Shake to unlock</color></size>";
                lockBannerGroup.alpha = 1f;
            }
            else
            {
                lockBannerBg.sprite = unlockedSprite;
                lockBannerText.text = lockNote;
                lockBannerGroup.alpha = Mathf.Clamp01((lockNoteUntil - now) / 0.4f);
            }

            float popAge = now - lockPopStart;
            float pop = popAge < 0.3f ? 1f + 0.18f * Mathf.Sin(popAge / 0.3f * Mathf.PI) : 1f;
            lockBannerRect.localScale = Vector3.one * pop;
        }

        private void ToggleTowerLock()
        {
            lockPopStart = Time.unscaledTime;
            if (matchState.OwnTowerLocked)
            {
                matchState.UnlockOwnTower();
                lockNote = "TOWER UNLOCKED";
                lockNoteUntil = lockPopStart + LockNoteSeconds;
                Haptics.Pulse(Haptics.Strength.Light);
                return;
            }

            // Lock where the phone was a moment before the shake began, not where it is mid-shake.
            matchState.LockOwnTowerAt(shakeInput.ShakeStartTime - 0.2f);
            if (matchState.OwnTowerLocked)
            {
                Haptics.Pulse(Haptics.Strength.Heavy);
                return;
            }

            // LockOwnTower refuses without a lane to lock to.
            lockNote = "CAN'T LOCK\n<size=70%>Scan the arena first</size>";
            lockNoteUntil = lockPopStart + LockNoteSeconds;
            Haptics.Pulse(Haptics.Strength.Medium);
        }

        private const float BarInset = 11f;   // clear of the bar's rim and coloured band (CardArt.BarFrame)
        private const float BarFillSpeed = 0.8f;
        
        private RectTransform ownHpRoot;
        private RectTransform opponentHpRoot;
        private float manaShown;
        private float ownShown;
        private float opponentShown;

        // Portrait phone layout: the mana bar full width just above the cards, where the thumbs are, and the two tower-HP bars
        // side by side right above it (you on the left, the enemy on the right, like a fighting game).
        private void BuildStatusCluster()
        {
            const float margin = 20f;
            const float hpHeight = HudLayout.HpBarHeight;
            float hpWidth = (HudLayout.ReferenceWidth - 3f * margin) * 0.5f;

            ownFill = BuildBar(matchTopGroup, hpWidth, hpHeight, HudPalette.Own, HudPalette.OwnDeep, "YOU", out ownHpText, true, crownSide: -1, theme: CardArt.Unit);
            ownHpRoot = BarRoot(ownFill);
            PlaceBottom(ownHpRoot, 0f, margin);
            opponentFill = BuildBar(matchTopGroup, hpWidth, hpHeight, HudPalette.Opponent, HudPalette.OpponentDeep, "ENEMY", out opponentHpText, true, crownSide: 1, theme: CardArt.Fireball);
            opponentHpRoot = BarRoot(opponentFill);
            PlaceBottom(opponentHpRoot, 1f, margin);

            var manaGo = UIFactory.CreateRect("ManaBar", canvas.transform);
            manaBarRoot = manaGo.GetComponent<RectTransform>();
            manaBarRoot.anchorMin = new Vector2(0.5f, 0f);
            manaBarRoot.anchorMax = new Vector2(0.5f, 0f);
            manaBarRoot.pivot = new Vector2(0.5f, 0f);
            manaBarRoot.sizeDelta = new Vector2(HudLayout.ReferenceWidth - 2f * margin, 68f);
            manaFill = BuildBar(manaBarRoot, HudLayout.ReferenceWidth - 2f * margin, 68f, HudPalette.Mana, HudPalette.ManaDeep, "MANA", out manaText, true, theme: CardArt.Unit);
            var manaBar = BarRoot(manaFill);
            manaBar.anchorMin = manaBar.anchorMax = new Vector2(0.5f, 0.5f);
            manaBar.anchoredPosition = Vector2.zero;
            AddManaTicks(manaFill.transform.parent, 10); // the fill's parent is the bar's "Inner" holder
            manaBarRoot.gameObject.SetActive(false);
        }

        // BuildBar returns the fill image: Bar > Inner > Fill. This is the Bar.
        private static RectTransform BarRoot(Image fill) => fill.transform.parent.parent.GetComponent<RectTransform>();

        // Anchors a bar to the bottom-left (side 0) or bottom-right (side 1) of its container. The height is set every frame in
        // Update, from HudLayout.HpBarsBottom.
        private static void PlaceBottom(RectTransform rect, float side, float margin)
        {
            rect.anchorMin = rect.anchorMax = new Vector2(side, 0f);
            rect.pivot = new Vector2(side, 0f);
            rect.anchoredPosition = new Vector2(side < 0.5f ? margin : -margin, 0f);
        }

        // Thin dividers so the mana bar reads as ten whole points, which is what a card costs.
        private static void AddManaTicks(Transform inner, int points)
        {
            if (inner == null) return;
            for (int i = 1; i < points; i++)
            {
                var tick = UIFactory.CreateRect("Tick" + i, inner);
                var rect = tick.GetComponent<RectTransform>();
                rect.anchorMin = new Vector2(i / (float)points, 0.12f);
                rect.anchorMax = new Vector2(i / (float)points, 0.88f);
                rect.pivot = new Vector2(0.5f, 0.5f);
                rect.sizeDelta = new Vector2(3f, 0f);
                var image = tick.AddComponent<Image>();
                image.color = new Color(0f, 0f, 0f, 0.35f);
                image.raycastTarget = false;
            }
        }

        private Image BuildBar(Transform parent, float width, float height, Color fillColor, Color fillDeep,
            string name, out TextMeshProUGUI valueLabel, bool withValue = false, int crownSide = 0, CardArt.Theme theme = null)
        {
            var go = UIFactory.CreateRect("Bar", parent);
            var rect = go.GetComponent<RectTransform>();
            rect.sizeDelta = new Vector2(width, height);
            var layoutElement = go.AddComponent<LayoutElement>();
            layoutElement.preferredWidth = width;
            layoutElement.preferredHeight = height;

            var track = go.AddComponent<Image>();
            track.sprite = CardArt.BarFrame(theme ?? CardArt.Unit);
            track.type = Image.Type.Sliced;
            track.raycastTarget = false;

            var inner = UIFactory.CreateRect("Inner", go.transform);
            var innerRect = inner.GetComponent<RectTransform>();
            UIFactory.StretchFull(innerRect);
            innerRect.offsetMin = new Vector2(BarInset, BarInset);
            innerRect.offsetMax = new Vector2(-BarInset, -BarInset);
            int innerRadius = 7;   // the frame's inner corner

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

            var nameLabel = UIFactory.CreateText(go.transform, name, height * 0.52f, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(nameLabel.rectTransform);
            nameLabel.alignment = TextAlignmentOptions.Left;
            nameLabel.margin = new Vector4(crownSide < 0 ? height * 1.2f : (name == "MANA" ? height * 1.15f : height * 0.5f), 0f, 0f, 0f);

            if (name == "MANA" || withValue)
            {
                valueLabel = UIFactory.CreateText(go.transform, "0/0", height * 0.56f, FontStyles.Bold, HudPalette.Cream);
                UIFactory.StretchFull(valueLabel.rectTransform);
                valueLabel.alignment = TextAlignmentOptions.Right;
                valueLabel.margin = new Vector4(0f, 0f, crownSide > 0 ? height * 1.2f : height * 0.5f, 0f);
            }
            else
            {
                valueLabel = null;
            }

            // A crown at the outer end of a tower-HP bar: blue for you, red for the enemy.
            if (crownSide != 0)
            {
                var crown = UIFactory.CreateCrown(go.transform, "Crown", height * 1.25f, crownSide < 0 ? new Color(0.5f, 0.78f, 1f) : new Color(1f, 0.52f, 0.46f));
                crown.anchorMin = crown.anchorMax = new Vector2(crownSide < 0 ? 0f : 1f, 0.5f);
                crown.anchoredPosition = new Vector2(crownSide < 0 ? height * 0.62f : -height * 0.62f, 2f);
            }

            // The mana bar carries the same faceted gem as the card costs, on its left end.
            if (name == "MANA")
            {
                var gemGo = UIFactory.CreateRect("ManaGem", go.transform);
                var gemRect = gemGo.GetComponent<RectTransform>();
                gemRect.anchorMin = gemRect.anchorMax = new Vector2(0f, 0.5f);
                gemRect.anchoredPosition = new Vector2(height * 0.5f, 0f);
                gemRect.sizeDelta = new Vector2(height * 1.3f, height * 1.3f);
                var gemImage = gemGo.AddComponent<Image>();
                gemImage.sprite = CardArt.Gem(CardArt.Unit);
                gemImage.raycastTarget = false;
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
            // Your tower under 30%: the bar and its number flash red so it cannot be missed.
            float flash = 0.5f + 0.5f * Mathf.Sin(Time.unscaledTime * 9f);
            bool ownLow = matchState.MatchStarted && ownT < 0.3f;
            ownFill.color = ownLow ? Color.Lerp(Color.white, new Color(1f, 0.5f, 0.45f), flash) : Color.white;
            ownHpText.color = ownLow ? Color.Lerp(HudPalette.Cream, new Color(1f, 0.4f, 0.35f), flash) : HudPalette.Cream;
            // A full mana bar turns its number gold: spend it, it is being wasted.
            manaText.color = matchState.ManaCurrent >= matchState.ManaMax - 0.01f ? HudPalette.Gold : HudPalette.Cream;
            ownHpText.text = Mathf.CeilToInt(matchState.OwnTowerHP).ToString();
            opponentHpText.text = Mathf.CeilToInt(matchState.OpponentTowerHP).ToString();
        }

        private void BuildTimer()
        {
            var go = UIFactory.CreateRect("Timer", matchTopGroup);
            var rect = go.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(0.5f, 1f);
            rect.anchorMax = new Vector2(0.5f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, -20f);
            rect.sizeDelta = new Vector2(240f, 76f);

            var bg = go.AddComponent<Image>();
            bg.sprite = CardArt.Panel(CardArt.Unit);
            bg.type = Image.Type.Sliced;

            timerText = UIFactory.CreateText(go.transform, "0:00", 46, FontStyles.Bold, HudPalette.Cream);
            UIFactory.StretchFull(timerText.rectTransform);
        }

        private void UpdateTimer()
        {
            int remaining = Mathf.CeilToInt(matchState.MatchTimeRemainingSeconds);
            timerText.text = $"{remaining / 60:0}:{remaining % 60:00}";
            timerText.color = remaining <= 30 ? new Color(1f, 0.4f, 0.35f) : HudPalette.Cream;
            // The last ten seconds tick: the clock swells at each second.
            float tick = matchState.MatchStarted && remaining <= 10 && remaining > 0 ? matchState.MatchTimeRemainingSeconds % 1f : 0f;
            float timerScale = 1f + 0.22f * tick * tick;
            timerText.transform.parent.localScale = new Vector3(timerScale, timerScale, 1f);
        }

        private RectTransform debugButtonRect;
        private DebugMenu debugMenu;
        private OptionsWindow optionsWindow;

        private HowToPlayWindow howToPlayWindow;

        // The settings button (a gear, top right) and the window it opens (Haptics, Music, Sound, HOW TO PLAY, DEBUG MENU,
        // Quit to main menu).
        private void BuildOptionsWindow()
        {
            howToPlayWindow = new HowToPlayWindow(canvas.transform, matchState);
            optionsWindow = new OptionsWindow(canvas.transform, () => matchSetup != null && matchSetup.HasChosenMode, QuitToMainMenu,
                () => howToPlayWindow.Open(), OpenDebugMenu);

            // The touch area is a clear 150 x 150 square in the top-right corner, bigger than the medallion drawn in it (100 x 100), so
            // the button is easy to hit. Nothing else at the top takes touches, so the extra area covers no other control.
            var go = UIFactory.CreateRect("SettingsButton", topRoot);
            var rect = go.GetComponent<RectTransform>();
            rect.anchorMin = new Vector2(1f, 1f);
            rect.anchorMax = new Vector2(1f, 1f);
            rect.pivot = new Vector2(1f, 1f);
            rect.anchoredPosition = new Vector2(-6f, -6f);
            rect.sizeDelta = new Vector2(150f, 150f);

            var hit = go.AddComponent<Image>();
            hit.color = new Color(0f, 0f, 0f, 0f);
            var button = go.AddComponent<Button>();
            button.targetGraphic = hit;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() => optionsWindow.Toggle());
            UIFactory.AddPressFeedback(go);

            var medalGo = UIFactory.CreateRect("Medallion", go.transform);
            var medalRect = medalGo.GetComponent<RectTransform>();
            medalRect.anchorMin = medalRect.anchorMax = new Vector2(1f, 1f);
            medalRect.pivot = new Vector2(1f, 1f);
            medalRect.anchoredPosition = new Vector2(-14f, -4f);   // 20 from the right edge and 10 from the top, as before
            medalRect.sizeDelta = new Vector2(100f, 100f);
            var bg = medalGo.AddComponent<Image>();
            bg.sprite = CardArt.Medallion(CardArt.Unit);   // a round medallion in the card frame
            bg.raycastTarget = false;

            var gearGo = UIFactory.CreateRect("Gear", medalGo.transform);
            var gearRect = gearGo.GetComponent<RectTransform>();
            gearRect.sizeDelta = new Vector2(64f, 64f);
            var gear = gearGo.AddComponent<Image>();
            gear.sprite = UITextureFactory.CreateGearIcon();
            gear.color = HudPalette.Cream;
            gear.raycastTarget = false;
        }

        private void OpenDebugMenu()
        {
            if (debugMenu == null) debugMenu = FindAnyObjectByType<DebugMenu>();
            if (debugMenu != null) debugMenu.SetMenuOpen(true);
        }

        // Quit to main menu: the same full reset the debug menu's Restart Game did, back to the TUTORIAL / VS CPU / VS PLAYER screen.
        // On the board engine MatchNetworkSync.DebugReset also drops the engine link, so the next VS PLAYER starts clean.
        private void QuitToMainMenu()
        {
            matchState.ResetMatch();
            arenaFrame.DebugForgetOpponent();
            matchSetup?.DebugReset();
            matchCountdown?.ResetCountdownGate();
            matchNetworkSync?.DebugReset();
            peerReadySync?.DebugReset();
        }

        // The debug menu is opened from the settings window now. This invisible rectangle is where its panel is drawn
        // (DebugMenu asks for DebugButtonGuiRect), just under the gear button, at the HUD's scale.
        private void BuildDebugButton()
        {
            var go = UIFactory.CreateRect("DebugPanelAnchor", topRoot);
            debugButtonRect = go.GetComponent<RectTransform>();
            debugButtonRect.anchorMin = new Vector2(1f, 1f);
            debugButtonRect.anchorMax = new Vector2(1f, 1f);
            debugButtonRect.pivot = new Vector2(1f, 1f);
            debugButtonRect.anchoredPosition = new Vector2(-20f, -180f);
            debugButtonRect.sizeDelta = new Vector2(DebugButtonWidth, 56f);
        }

        public const float DebugButtonWidth = 220f;

        // Reused: the debug menu asks for this rectangle many times in every OnGUI, and a new array each time is garbage on a phone.
        private readonly Vector3[] debugCorners = new Vector3[4];

        // The DEBUG button's rectangle in screen pixels with the origin at the top left (what OnGUI draws in), so the menu
        // panel sits under it whatever the canvas scale is. Width 0 before the button exists.
        public Rect DebugButtonGuiRect
        {
            get
            {
                if (debugButtonRect == null) return new Rect(0f, 0f, 0f, 0f);
                debugButtonRect.GetWorldCorners(debugCorners);
                var corners = debugCorners;
                Camera cam = canvas.renderMode == RenderMode.ScreenSpaceOverlay ? null : canvas.worldCamera;
                Vector2 bottomLeft = RectTransformUtility.WorldToScreenPoint(cam, corners[0]);
                Vector2 topRight = RectTransformUtility.WorldToScreenPoint(cam, corners[2]);
                return new Rect(bottomLeft.x, Screen.height - topRight.y, topRight.x - bottomLeft.x, topRight.y - bottomLeft.y);
            }
        }

        private void BuildTrackingOverlay()
        {
            var go = UIFactory.CreateRect("TrackingOverlay", canvas.transform);
            var rect = go.GetComponent<RectTransform>();
            UIFactory.StretchFull(rect);

            trackingMessage = UIFactory.CreateText(go.transform, string.Empty, 40, FontStyles.Bold, HudPalette.Cream);
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
                SetOverlay(string.Empty);
                return;
            }

            if (matchSetup != null && matchSetup.Mode == MatchSetup.GameMode.VsPlayer && matchNetworkSync != null && matchNetworkSync.BoardMissing)
            {
                SetOverlay("VS PLAYER needs the board's game engine, and this build has none set up");
            }
            else if (!arenaFrame.HasLane)
            {
                SetOverlay("Searching for opponent marker...");
            }
            else if (matchState.MatchStarted && matchState.OwnTowerOutsideArena)
            {
                SetOverlay("OUT OF ARENA - step back in");
            }
            else if (matchState.IsBoardClient && matchState.BoardStatusMessage.Length > 0)
            {
                SetOverlay(matchState.BoardStatusMessage);
            }
            else if (!matchState.MatchStarted && peerReadySync != null && !peerReadySync.PeerReady)
            {
                SetOverlay("Waiting for the other player to also find you...");
            }
            else if (!matchState.MatchStarted)
            {
                SetOverlay("Opponent found! Stand your ground, face your opponent and press Start Game");
            }
            else
            {
                SetOverlay(string.Empty);
            }  
        }

        private void SetOverlay(string message)
        {
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
