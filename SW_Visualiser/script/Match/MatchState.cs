using System;
using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Tracking;
using BridgeRush.Networking;
using BridgeRush.Gameplay;
using BridgeRush.Input;
using UnityEngine.Serialization;

namespace BridgeRush.Match
{
    public partial class MatchState : MonoBehaviour
    {
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private UnitStatsTable unitStatsTable;
        [SerializeField] private TowerController towerController;
        [SerializeField] private SummonCursor summonCursor;

        [Header("Towers")]
        [SerializeField] private float ownTowerMaxHP = 1000f;
        [SerializeField] private float opponentTowerMaxHP = 1000f;
        [SerializeField] private float towerCounterDps = 15f;
        [SerializeField] private float outOfBoundsDamagePerSecond = 20f;
        [Tooltip("How long a player may stand outside the arena before the tower starts losing HP. The count starts again on coming back inside. The board engine has the same setting (outOfBoundsGraceSeconds in rules.json).")]
        [SerializeField] private float outOfBoundsGraceSeconds = 3f;

        // Mana rules. VS PLAYER runs on the board, which reads engine/rules.json, so these Board* numbers are what the board uses
        // (manaMax, manaStart, manaRegenPerSecond, doubleManaLastSeconds, doubleManaMultiplier); the engine test fails if they
        // drift from rules.json. The Inspector fields below drive VS CPU, the tutorial and the host phone and default to the same
        // numbers. They have NEW names on purpose: the saved scene still holds the old fields (manaRegenPerSecond 0.2), which
        // would otherwise load and override these. If an Inspector value differs from the board's, a warning is logged on start.
        private const float BoardManaMax = 10f;
        private const float BoardManaStart = 5f;
        private const float BoardManaRegenPerSecond = 0.33f;
        private const float BoardDoubleManaLastSeconds = 60f;
        private const float BoardDoubleManaMultiplier = 2f;

        [Header("Mana (VS CPU, tutorial, host phone; keep equal to engine/rules.json)")]
        [Tooltip("Most mana you can hold. The board uses 10.")]
        [SerializeField] private float maxMana = BoardManaMax;
        [Tooltip("Mana at the start of a match. The board uses 5.")]
        [SerializeField] private float startMana = BoardManaStart;
        [Tooltip("Mana gained per second. The board uses 0.33, so keep it at 0.33 for every game mode to feel the same.")]
        [SerializeField] private float manaPerSecond = BoardManaRegenPerSecond;
        [Tooltip("Mana regenerates faster for the last this many seconds of the match. The board uses 60.")]
        [SerializeField] private float doubleManaFinalSeconds = BoardDoubleManaLastSeconds;
        [Tooltip("How many times faster mana regenerates in the final seconds. The board uses 2.")]
        [SerializeField] private float doubleManaFactor = BoardDoubleManaMultiplier;

        [Header("Tick rate")]
        [SerializeField] private float mockTickRateHz = 10f;

        [Header("Tuning")]
        [SerializeField] private float globalSpeedMultiplier = 1f;
        // How far past the tower's footprint a melee unit stops to attack, in metres at scale 1. The distance is 3D (a unit stands
        // groundYOffsetMeters above the tower's base, 0.1 m), so on the floor it stops at sqrt((footprint + this)^2 - 0.1^2): with a
        // footprint of 0.119 m and 0.08 here that is 0.172 m, on the edge of the tower's base ring (the red or blue circle, 1.4
        // footprint radii = 0.166 m). The unit hits from the circle, in view, not from inside the tower. Ranged units (the Mage)
        // are not affected: they stop at their attack range. Not a serialized field, so an old value saved in the scene cannot
        // override it. Keep engine/rules.json MatchState.towerClearanceMeters equal.
        private float towerClearanceMeters = 0.08f;
        [SerializeField] private float towerDisengageHysteresisMeters = 0.05f;

        [Header("Match timer")]
        [SerializeField] private float matchDurationSeconds = 180f;

        [Header("Lane engagement")]
        [Tooltip("How far away a unit notices an enemy unit and starts walking toward it.")]
        [FormerlySerializedAs("engageRangeMeters")]
        [SerializeField] private float noticeRangeMeters = 0.4f;
        [Tooltip("How close a melee unit must get before it hits. Ranged units use their own Attack Range Meters.")]
        [SerializeField] private float hitReachMeters = 0.15f;
        private float NoticeRangeMeters => noticeRangeMeters * ArenaScale;
        private float HitReachMeters => hitReachMeters * ArenaScale;
        public float EffectiveNoticeRangeMeters => NoticeRangeMeters;
        public float EffectiveHitReachMeters => HitReachMeters;
        private float TowerClearance => towerClearanceMeters * ArenaScale;
        private float DisengageHysteresis => towerDisengageHysteresisMeters * ArenaScale;

        [Header("Arena scale")]
        [Tooltip("Lane length the distances in metres are tuned for. At this lane length the scale is 1.")]
        [SerializeField] private float referenceLaneMeters = 2f;
        [Tooltip("How strongly sizes and ranges follow the lane length. 0 = never scale, 1 = scale fully, 0.5 = gentle (square root).")]
        [Range(0f, 1f)]
        [SerializeField] private float arenaScaleExponent = 0.5f;
        [SerializeField] private float arenaScaleMin = 0.7f;
        [SerializeField] private float arenaScaleMax = 1.8f;
        [SerializeField] private float arenaScaleSmoothSeconds = 0.4f;

        [Header("Lane width / spine")]
        [SerializeField] private float laneWidthFraction = 0.55f;
        [SerializeField] private float laneSpineFraction = 0.2f;
        [SerializeField] private float laneCenteringSmoothTime = 1.4f;
        [SerializeField] private float centeringInfluenceRadiusFraction = 0.6f;

        [Header("Enemy bot")]
        [SerializeField] private bool enemyBotEnabled = true;
        [SerializeField] private float enemyBotSpawnIntervalMinSeconds = 5f;
        [SerializeField] private float enemyBotSpawnIntervalMaxSeconds = 8f;

        [Header("Spawn presentation")]
        [SerializeField] private float spawnForwardOffsetMeters = 0.084f;

        [Header("Bridge span")]
        [SerializeField] private float bridgeFraction = 0.15f;
        [SerializeField] private bool twoBridges = true;
        [SerializeField] private float bridgeLateralOffsetFraction = 0.3f;
        [SerializeField] private BridgeVisualizer bridgeVisualizer;

        [Header("Arena boundary shape")]
        [SerializeField] private float territoryWidthMultiplier = 1.5f;
        [SerializeField] private float funnelFraction = 0.2f;
        [SerializeField] private float lookAheadMeters = 0.1f;
        [SerializeField] private float rearMarginFraction = 0.05f;
        [SerializeField] private float groundYOffsetMeters = 0.1f;

        [Header("Network role")]
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchNetworkSync networkSync;

        private readonly List<UnitState> units = new();
        private int nextUnitId;
        private float timeSinceLastTick;
        private float matchTimeRemaining;
        private float enemyManaCurrent;
        private float timeUntilNextEnemyBotAttempt;
        private bool matchStarted;
        private bool homeAnchorsCaptured;
        private Vector3 ownHomeAnchor;
        private Vector3 opponentHomeAnchor;
        private Vector3 bridgePosition;
        private Vector3 ownBridgeEdge;
        private Vector3 opponentBridgeEdge;
        private float[] bridgeLateralOffsets = { 0f };
        private bool bridgeTieToggle;
        private float liveArenaScale = 1f;
        private float arenaScaleVelocity;
        private bool arenaScaleFrozen;
        private bool doubleManaAnnounced;
        private float frozenArenaScale = 1f;
        private float totalLaneLength;
        private Vector3 fixedLaneDirection;
        private static readonly UnitType[] AllUnitTypes = (UnitType[])Enum.GetValues(typeof(UnitType));

        // BoardClient: the Ultra96 game engine runs the match and this phone only draws its snapshots (see MatchState.Engine.cs).
        public enum NetworkRole { Local, Host, Client, BoardClient }
        public NetworkRole Role { get; private set; } = NetworkRole.Local;

        public void SetNetworkRole(NetworkRole role)
        {
            Role = role;
            // A real PvP match never runs the bot, regardless of how enemyBotEnabled was left set.
            if (role != NetworkRole.Local) enemyBotEnabled = false;
        }

        // Only VS CPU (via MatchSetup.ActivateCpuOpponent) or the debug menu should ever turn this on.
        public void SetEnemyBotEnabled(bool enabled) => enemyBotEnabled = enabled;

        public event Action StateTicked;

        public IReadOnlyList<UnitState> Units => units;

        public bool MatchStarted => matchStarted;

        public bool Paused { get; private set; }
        public void TogglePause() => Paused = !Paused;

        private bool ownTowerLocked;
        private Vector3 lockedOwnTowerPosition;
        public bool OwnTowerLocked => ownTowerLocked;

        // The last second and a half of the own-device lane origin, so a lock can use where the phone was just before the shake that
        // asked for it (a shake moves the phone, and blurs the camera so the lane can be lost at the moment it is recognised).
        private const int OriginHistory = 120;
        private readonly float[] originTimes = new float[OriginHistory];
        private readonly Vector3[] originPositions = new Vector3[OriginHistory];
        private int originCount;
        private int originNext;

        private void RecordLaneOrigin()
        {
            if (arenaFrame == null || !arenaFrame.HasLane) return;
            originTimes[originNext] = Time.unscaledTime;
            originPositions[originNext] = arenaFrame.Lane.Origin;
            originNext = (originNext + 1) % OriginHistory;
            if (originCount < OriginHistory) originCount++;
        }

        /// <summary>Lock the own tower where the phone was at `unscaledTime` (the newest recorded spot at or before it), falling
        /// back to the oldest recorded one, then to the live lane. Does nothing without a lane ever having been seen.</summary>
        public void LockOwnTowerAt(float unscaledTime)
        {
            if (originCount == 0) { LockOwnTower(); return; }
            int best = -1;
            for (int n = 0; n < originCount; n++)
            {
                int index = (originNext - 1 - n + 2 * OriginHistory) % OriginHistory; // newest first
                best = index;
                if (originTimes[index] <= unscaledTime) break;
            }
            lockedOwnTowerPosition = originPositions[best];
            ownTowerLocked = true;
        }

        public void LockOwnTower()
        {
            // Only lock if we have a valid lane to lock to
            if (!arenaFrame.HasLane) return;
            lockedOwnTowerPosition = arenaFrame.Lane.Origin;
            ownTowerLocked = true;
        }
        public void UnlockOwnTower() => ownTowerLocked = false;
        public Vector3 OwnTowerPosition => ownTowerLocked
            ? lockedOwnTowerPosition
            : (arenaFrame.HasLane ? arenaFrame.Lane.Origin : Vector3.zero);

        // Multiplies every distance in metres (unit size, ranges, tower size, lightning radius). Follows the live lane
        // smoothly until the match starts, then is frozen so nothing resizes mid-game.
        public float ArenaScale => arenaScaleFrozen ? frozenArenaScale : liveArenaScale;
        public float ScaledLightningRadius => spellSpec.LightningRadiusMeters * ArenaScale;

        private float ComputeArenaScale(float laneLengthMeters)
        {
            if (referenceLaneMeters < 0.0001f || laneLengthMeters < 0.0001f) return 1f;
            float scale = Mathf.Pow(laneLengthMeters / referenceLaneMeters, Mathf.Clamp01(arenaScaleExponent));
            return Mathf.Clamp(scale, Mathf.Min(arenaScaleMin, arenaScaleMax), Mathf.Max(arenaScaleMin, arenaScaleMax));
        }

        private void UpdateLiveArenaScale()
        {
            if (arenaScaleFrozen) return;
            float target = arenaFrame != null && arenaFrame.HasLane ? ComputeArenaScale(arenaFrame.Lane.Length) : 1f;
            liveArenaScale = Mathf.SmoothDamp(liveArenaScale, target, ref arenaScaleVelocity, Mathf.Max(0.01f, arenaScaleSmoothSeconds));
        }

        // The last minute of the match: mana comes back faster. Worked out from the time left, so the client sees it too.
        public bool DoubleManaActive =>
            matchStarted && Result == MatchResult.None && matchTimeRemaining > 0f && matchTimeRemaining <= doubleManaFinalSeconds;
        public float ManaRegenMultiplier => DoubleManaActive ? Mathf.Max(1f, doubleManaFactor) : 1f;
        public float DoubleManaMultiplier => Mathf.Max(1f, doubleManaFactor);
        public float DoubleManaLastSeconds => doubleManaFinalSeconds;

        // Test aid (Debug menu): jump the clock so the last minute starts now. Host and VS CPU only.
        public void DebugSetTimeRemaining(float seconds)
        {
            if (!DoesNotSimulate) matchTimeRemaining = Mathf.Max(0f, seconds);
        }

        // Fired once when the double mana minute begins, on every phone.
        public event Action DoubleManaStarted;

        private void CheckDoubleMana()
        {
            if (doubleManaAnnounced || !DoubleManaActive) return;
            doubleManaAnnounced = true;
            DoubleManaStarted?.Invoke();
        }

        public bool HasBridge => homeAnchorsCaptured;
        public Vector3 BridgePosition => bridgePosition;
        public Vector3 OwnHomeAnchor => ownHomeAnchor;
        public Vector3 OpponentHomeAnchor => opponentHomeAnchor;
        public Vector3 OwnBridgeEdge => ownBridgeEdge;
        public Vector3 OpponentBridgeEdge => opponentBridgeEdge;
        public bool TwoBridges => twoBridges; // fixed: the DEBUG menu switch for it was removed 2026-11-06
        public int BridgeCount => bridgeLateralOffsets.Length;
        public float BridgeWidthMeters => LaneWidthMeters * laneSpineFraction;
        public Vector3 BridgePositionOf(int index) => bridgePosition + BridgeLateralAxis * bridgeLateralOffsets[index];
        public Vector3 BridgeOwnEdgeOf(int index) => ownBridgeEdge + BridgeLateralAxis * bridgeLateralOffsets[index];
        public Vector3 BridgeOpponentEdgeOf(int index) => opponentBridgeEdge + BridgeLateralAxis * bridgeLateralOffsets[index];
        private Vector3 BridgeLateralAxis => Vector3.Cross(Vector3.up, fixedLaneDirection).normalized;
        public float LaneWidthMeters => laneWidthFraction * totalLaneLength;
        public float IslandWidthMeters => LaneWidthMeters * Mathf.Max(0f, territoryWidthMultiplier);
        public float LaneSpineFraction => laneSpineFraction;
        public float TotalLaneLength => totalLaneLength;
        public float RearMarginMeters => totalLaneLength * Mathf.Max(0f, rearMarginFraction);
        public float OwnTowerHP { get; private set; }
        public float OwnTowerMaxHP => ownTowerMaxHP;
        public float OpponentTowerHP { get; private set; }
        public float OpponentTowerMaxHP => opponentTowerMaxHP;
        public void DamageOpponentTower(float amount)
        {
            if (DoesNotSimulate) return;
            OpponentTowerHP = Mathf.Max(0f, OpponentTowerHP - amount);
        }
        public void DamageOwnTower(float amount) => OwnTowerHP = Mathf.Max(0f, OwnTowerHP - amount);
        public float ManaCurrent { get; private set; }
        public float ManaMax => maxMana;

        // Debug only: keeps your mana and your tower HP full and makes summons and spells free. Runs on the phone that
        // simulates the match (VS CPU or the host); a client's mana and HP come from the host's snapshots, so it does nothing there.
        public bool GodMode { get; set; }

        // Tutorial only: asked before every summon or spell with the unit type name or the spell key. Returns null to allow it
        // or a short reason to refuse it, which the cards and the result banner show like any other refusal.
        public Func<string, string> ActionFilter { get; set; }

        // Tutorial only: the clock stands still, nothing ends the match, and the opponent tower is refilled when it falls, so
        // the lesson is never cut short. TutorialCoach sets it and clears it when the lesson is done.
        public bool EndlessMatch { get; set; }

        private void ApplyGodMode()
        {
            ManaCurrent = maxMana;
            OwnTowerHP = ownTowerMaxHP;
        }
        public float TickIntervalSeconds => 1f / Mathf.Max(0.01f, Role == NetworkRole.BoardClient && boardTickHz > 0 ? boardTickHz : mockTickRateHz);
        public float TimeSinceLastTick => timeSinceLastTick;

        public float MatchTimeRemainingSeconds => Mathf.Max(0f, matchTimeRemaining);

        public enum MatchResult { None, Win, Loss, Draw }
        public MatchResult Result { get; private set; } = MatchResult.None;
        public bool HasEnded => Result != MatchResult.None;

        public void CheckForMatchEnd()
        {
            if (HasEnded || EndlessMatch) return;

            bool ownDestroyed = OwnTowerHP <= 0f;
            bool opponentDestroyed = OpponentTowerHP <= 0f;
            bool timeUp = matchTimeRemaining <= 0f;
            if (!ownDestroyed && !opponentDestroyed && !timeUp) return;

            if (ownDestroyed && opponentDestroyed) Result = MatchResult.Draw;
            else if (opponentDestroyed) Result = MatchResult.Win;
            else if (ownDestroyed) Result = MatchResult.Loss;
            else if (OwnTowerHP > OpponentTowerHP) Result = MatchResult.Win;
            else if (OpponentTowerHP > OwnTowerHP) Result = MatchResult.Loss;
            else Result = MatchResult.Draw;
        }

        public event Action<UnitState> UnitSpawned;
        // The local player summoned a unit (accepted by SpawnUnit), from any input. Drives the card animation.
        public event Action<UnitType> OwnUnitSummoned;
        // A summon was refused, with a short reason for the card (NO MANA, BAD AIM...).
        public event Action<UnitType, string> OwnUnitRejected;
        public event Action<int> UnitDespawned;

        private void Awake()
        {
            // VS PLAYER only runs on the board, so find EngineLink even if its slot was left empty in the Inspector.
            if (engineLink == null) engineLink = FindAnyObjectByType<EngineLink>();
            WarnIfManaDiffersFromBoard();
            ResetMatch();
        }

        private void WarnIfManaDiffersFromBoard()
        {
            if (Mathf.Approximately(maxMana, BoardManaMax) && Mathf.Approximately(startMana, BoardManaStart)
                && Mathf.Approximately(manaPerSecond, BoardManaRegenPerSecond)
                && Mathf.Approximately(doubleManaFinalSeconds, BoardDoubleManaLastSeconds)
                && Mathf.Approximately(doubleManaFactor, BoardDoubleManaMultiplier)) return;
            Debug.LogWarning($"[MatchState] The mana rules in the Inspector (max {maxMana}, start {startMana}, {manaPerSecond}/s, x{doubleManaFactor} for the last {doubleManaFinalSeconds}s) differ from the board's engine/rules.json (max {BoardManaMax}, start {BoardManaStart}, {BoardManaRegenPerSecond}/s, x{BoardDoubleManaMultiplier} for the last {BoardDoubleManaLastSeconds}s). VS CPU and the tutorial will not match VS PLAYER.");
        }

        public void ResetMatch()
        {
            for (int i = units.Count - 1; i >= 0; i--)
            {
                UnitDespawned?.Invoke(units[i].Id);
            }
            units.Clear();
            ClearStrikes();

            OwnTowerHP = ownTowerMaxHP;
            OpponentTowerHP = opponentTowerMaxHP;
            ManaCurrent = startMana;
            enemyManaCurrent = startMana;
            timeUntilNextEnemyBotAttempt = UnityEngine.Random.Range(enemyBotSpawnIntervalMinSeconds, enemyBotSpawnIntervalMaxSeconds);
            matchTimeRemaining = matchDurationSeconds;
            timeSinceLastTick = 0f;
            homeAnchorsCaptured = false;
            arenaScaleFrozen = false;
            doubleManaAnnounced = false;
            matchStarted = false;
            ownTowerLocked = false;
            ownOutsideSeconds = 0f;
            opponentOutsideSeconds = 0f;
            Result = MatchResult.None;
        }

        public void StartMatch()
        {
            matchStarted = true;
        }

        private void Update()
        {
            RecordLaneOrigin();
            UpdateLiveArenaScale();
            if (Role == NetworkRole.BoardClient)
            {
                UpdateBoardClient();
                return;
            }
            if (!matchStarted || Paused) return;

            CaptureHomeAnchorsIfNeeded(); // Check if home anchors have been captured and update the state accordingly.
            CheckDoubleMana();

            // The client does not simulate, but its clock must run so the units glide between the host's updates.
            if (Role == NetworkRole.Client)
            {
                timeSinceLastTick += Time.deltaTime;
                return;
            }

            if (HasEnded) return;

            if (GodMode) ApplyGodMode();
            float manaRegen = manaPerSecond * ManaRegenMultiplier * Time.deltaTime;
            ManaCurrent = Mathf.Min(maxMana, ManaCurrent + manaRegen);
            enemyManaCurrent = Mathf.Min(maxMana, enemyManaCurrent + manaRegen);
            if (!EndlessMatch) matchTimeRemaining = Mathf.Max(0f, matchTimeRemaining - Time.deltaTime);
            //time.deltatime is used to esnure smoothness of the gamme
            ApplyOutOfBoundsDamage(Time.deltaTime);
            UpdateStrikes(Time.deltaTime);
            if (enemyBotEnabled) UpdateEnemyBot(Time.deltaTime);

            timeSinceLastTick += Time.deltaTime;
            float tickInterval = TickIntervalSeconds;
            while (timeSinceLastTick >= tickInterval)
            {   
                // Simulate a tick at the specified interval (10 Hz) and invoke the StateTicked event.
                SimulateTick(tickInterval);
                timeSinceLastTick -= tickInterval;
                StateTicked?.Invoke();
            }

            if (GodMode) ApplyGodMode();
            if (EndlessMatch && OpponentTowerHP <= 0f) OpponentTowerHP = opponentTowerMaxHP;
            CheckForMatchEnd();
        }

        private UnitState FindById(int id)
        {
            foreach (var unit in units)
            {
                if (unit.Id == id) return unit;
            }
            return null;
        }
    }
}
