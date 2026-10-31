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

        [Header("Mana")]
        [SerializeField] private float manaMax = 10f;
        [SerializeField] private float manaStart = 5f;
        [SerializeField] private float manaRegenPerSecond = 1f;

        [Header("Double mana")]
        [Tooltip("Mana regenerates faster for the last this many seconds of the match.")]
        [SerializeField] private float doubleManaLastSeconds = 60f;
        [SerializeField] private float doubleManaMultiplier = 2f;

        [Header("Tick rate")]
        [SerializeField] private float mockTickRateHz = 10f;

        [Header("Tuning")]
        [SerializeField] private float globalSpeedMultiplier = 1f;
        [SerializeField] private float towerClearanceMeters = 0.07f;
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

        public enum NetworkRole { Local, Host, Client }
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
            matchStarted && Result == MatchResult.None && matchTimeRemaining > 0f && matchTimeRemaining <= doubleManaLastSeconds;
        public float ManaRegenMultiplier => DoubleManaActive ? Mathf.Max(1f, doubleManaMultiplier) : 1f;
        public float DoubleManaMultiplier => Mathf.Max(1f, doubleManaMultiplier);
        public float DoubleManaLastSeconds => doubleManaLastSeconds;

        // Test aid (Debug menu): jump the clock so the last minute starts now. Host and VS CPU only.
        public void DebugSetTimeRemaining(float seconds)
        {
            if (Role != NetworkRole.Client) matchTimeRemaining = Mathf.Max(0f, seconds);
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
        public bool TwoBridges { get => twoBridges; set => twoBridges = value; }
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
            if (Role == NetworkRole.Client) return;
            OpponentTowerHP = Mathf.Max(0f, OpponentTowerHP - amount);
        }
        public void DamageOwnTower(float amount) => OwnTowerHP = Mathf.Max(0f, OwnTowerHP - amount);
        public float ManaCurrent { get; private set; }
        public float ManaMax => manaMax;

        // Debug only: keeps your mana and your tower HP full and makes summons and spells free. Runs on the phone that
        // simulates the match (VS CPU or the host); a client's mana and HP come from the host's snapshots, so it does nothing there.
        public bool GodMode { get; set; }

        private void ApplyGodMode()
        {
            ManaCurrent = manaMax;
            OwnTowerHP = ownTowerMaxHP;
        }
        public float TickIntervalSeconds => 1f / Mathf.Max(0.01f, mockTickRateHz);
        public float TimeSinceLastTick => timeSinceLastTick;

        public float MatchTimeRemainingSeconds => Mathf.Max(0f, matchTimeRemaining);

        public enum MatchResult { None, Win, Loss, Draw }
        public MatchResult Result { get; private set; } = MatchResult.None;
        public bool HasEnded => Result != MatchResult.None;

        public void CheckForMatchEnd()
        {
            if (HasEnded) return;

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
            ResetMatch();
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
            ManaCurrent = manaStart;
            enemyManaCurrent = manaStart;
            timeUntilNextEnemyBotAttempt = UnityEngine.Random.Range(enemyBotSpawnIntervalMinSeconds, enemyBotSpawnIntervalMaxSeconds);
            matchTimeRemaining = matchDurationSeconds;
            timeSinceLastTick = 0f;
            homeAnchorsCaptured = false;
            arenaScaleFrozen = false;
            doubleManaAnnounced = false;
            matchStarted = false;
            ownTowerLocked = false;
            Result = MatchResult.None;
        }

        public void StartMatch()
        {
            matchStarted = true;
        }

        private void Update()
        {
            UpdateLiveArenaScale();
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
            float manaRegen = manaRegenPerSecond * ManaRegenMultiplier * Time.deltaTime;
            ManaCurrent = Mathf.Min(manaMax, ManaCurrent + manaRegen);
            enemyManaCurrent = Mathf.Min(manaMax, enemyManaCurrent + manaRegen);
            matchTimeRemaining = Mathf.Max(0f, matchTimeRemaining - Time.deltaTime);
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
