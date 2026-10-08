using System;
using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Tracking;
using BridgeRush.Networking;
using BridgeRush.Gameplay;
using BridgeRush.Input;

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
        [SerializeField] private float manaRegenPerSecond = 1f / 2.5f;

        [Header("Tick rate")]
        [SerializeField] private float mockTickRateHz = 10f;

        [Header("Tuning")]
        [SerializeField] private float globalSpeedMultiplier = 1f;
        [SerializeField] private float towerClearanceMeters = 0.07f;
        [SerializeField] private float towerDisengageHysteresisMeters = 0.05f;

        [Header("Match timer")]
        [SerializeField] private float matchDurationSeconds = 180f;

        [Header("Lane engagement")]
        [SerializeField] private float engageRangeMeters = 0.8f;
        private float EngageRangeMeters => engageRangeMeters;

        [Header("Lane width / spine")]
        [SerializeField] private float laneWidthFraction = 0.55f;
        [SerializeField] private float laneSpineFraction = 0.2f;
        [SerializeField] private float laneCenteringSmoothTime = 1.4f;
        [SerializeField] private float centeringInfluenceRadiusFraction = 0.6f;

        [Header("Enemy bot")]
        [SerializeField] private bool enemyBotEnabled = false;
        [SerializeField] private float enemyBotSpawnIntervalMinSeconds = 3f;
        [SerializeField] private float enemyBotSpawnIntervalMaxSeconds = 8f;

        [Header("Spawn presentation")]
        [SerializeField] private float spawnForwardOffsetMeters = 0.084f;

        [Header("Bridge span")]
        [SerializeField] private float bridgeFraction = 0.15f;
        [SerializeField] private BridgeVisualizer bridgeVisualizer;

        [Header("Arena boundary shape")]
        [SerializeField] private float territoryWidthMultiplier = 1.5f;
        [SerializeField] private float funnelFraction = 0.2f;
        [SerializeField] private float lookAheadMeters = 0.1f;
        [SerializeField] private float rearMarginFraction = 0.05f;
        [SerializeField] private float groundYOffsetMeters = 0f;

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

        public bool HasBridge => homeAnchorsCaptured;
        public Vector3 BridgePosition => bridgePosition;
        public Vector3 OwnHomeAnchor => ownHomeAnchor;
        public Vector3 OpponentHomeAnchor => opponentHomeAnchor;
        public Vector3 OwnBridgeEdge => ownBridgeEdge;
        public Vector3 OpponentBridgeEdge => opponentBridgeEdge;
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

            OwnTowerHP = ownTowerMaxHP;
            OpponentTowerHP = opponentTowerMaxHP;
            ManaCurrent = manaStart;
            enemyManaCurrent = manaStart;
            timeUntilNextEnemyBotAttempt = UnityEngine.Random.Range(enemyBotSpawnIntervalMinSeconds, enemyBotSpawnIntervalMaxSeconds);
            matchTimeRemaining = matchDurationSeconds;
            timeSinceLastTick = 0f;
            homeAnchorsCaptured = false;
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
            if (!matchStarted || Paused) return;

            CaptureHomeAnchorsIfNeeded(); // Check if home anchors have been captured and update the state accordingly.

            if (Role == NetworkRole.Client) return;

            if (HasEnded) return;

            ManaCurrent = Mathf.Min(manaMax, ManaCurrent + manaRegenPerSecond * Time.deltaTime);
            enemyManaCurrent = Mathf.Min(manaMax, enemyManaCurrent + manaRegenPerSecond * Time.deltaTime);
            matchTimeRemaining = Mathf.Max(0f, matchTimeRemaining - Time.deltaTime);
            //time.deltatime is used to esnure smoothness of the gamme
            ApplyOutOfBoundsDamage(Time.deltaTime);
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
