using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    public class GameStateRenderer : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private UnitPrefabCatalog unitPrefabCatalog;
        [SerializeField] private Transform unitContainer;
        [SerializeField] private float unitTargetHeightMeters = 0.25f;
        [SerializeField] private Color enemyTintColor = new(1f, 0.35f, 0.35f);
        [SerializeField] private bool spawnEffects = true;
        [SerializeField] private Color ownSparkColor = new(0.45f, 0.85f, 1f);

        [Header("Sound")]
        [SerializeField] private bool summonSounds = true;
        [Tooltip("Optional, indexed by unit type (Warrior, Rogue, Mage, Tank, Hero). Empty slots use the built-in synthesised sound.")]
        [SerializeField] private AudioClip[] summonClips = new AudioClip[5];

        [Header("Music")]
        [SerializeField] private bool backgroundMusic = true;
        [Range(0f, 1f)]
        [SerializeField] private float musicVolume = 0.3f;
        [Tooltip("Volume of the calm music in the menus, before the match.")]
        [Range(0f, 1f)]
        [SerializeField] private float menuMusicVolume = 0.25f;
        [Tooltip("Optional. Leave empty to use the built-in generated loop.")]
        [SerializeField] private AudioClip musicClip;
        [Tooltip("Optional. The faster loop for the last minute (double mana). Empty uses the built-in fast loop, or the clip above if that is set.")]
        [SerializeField] private AudioClip musicClipLastMinute;
        [Tooltip("Optional. Music for the menus and the result screen. Empty uses the built-in calm loop.")]
        [SerializeField] private AudioClip menuMusicClip;

        [Header("Haptics")]
        [SerializeField] private bool damageHaptics = true;
        [Tooltip("Shortest time between two pulses while damage keeps coming, so it ticks instead of buzzing.")]
        [SerializeField] private float hapticMinIntervalSeconds = 0.45f;
        [Tooltip("Damage taken since the last pulse: below this is a light tap, above Heavy Above Damage is a heavy one.")]
        [SerializeField] private float lightBelowDamage = 6f;
        [SerializeField] private float heavyAboveDamage = 25f;

        [Header("Ranged units")]
        [SerializeField] private bool rangedBolts = true;
        [SerializeField] private float boltIntervalSeconds = 0.6f;
        [SerializeField] private float boltSpeedMetersPerSecond = 2f;
        [SerializeField] private float towerAimHeightMeters = 0.25f;
        [SerializeField] private Color ownBoltColor = new(0.55f, 0.8f, 1f);
        [SerializeField] private Color enemyBoltColor = new(1f, 0.45f, 0.35f);

        private readonly Dictionary<int, UnitView> activeViews = new();
        private readonly Dictionary<int, float> boltReadyAt = new();
        private float lastOwnTowerHp = -1f;
        private float damageSincePulse;
        private float nextPulseAt;
        private void Awake()
        {
            if (matchState == null || unitPrefabCatalog == null)
            {
                Debug.LogError($"{nameof(GameStateRenderer)}: needs a Match State and a Unit Prefab Catalog.", this);
                enabled = false;
            }
        }
        private void OnEnable()
        {
            if (matchState == null) return;
            matchState.UnitSpawned += OnUnitSpawned;
            matchState.UnitDespawned += OnUnitDespawned;
        }
        private void OnDisable()
        {
            if (matchState == null) return;
            matchState.UnitSpawned -= OnUnitSpawned;
            matchState.UnitDespawned -= OnUnitDespawned;
        }
        private float UnitHeight => unitTargetHeightMeters * matchState.ArenaScale;
        private void OnUnitSpawned(UnitState unit)
        {
            if (!unitPrefabCatalog.TryGetPrefab(unit.Type, out var prefab)) return;
            var instance = Instantiate(prefab, unitContainer);
            instance.transform.position = unit.WorldPosition;
            instance.transform.rotation = Quaternion.LookRotation(unit.FacingDirection, Vector3.up);
            NormalizeUnitHeight(instance, UnitHeight);
            var view = instance.GetComponent<UnitView>();
            if (view == null)
            {
                view = instance.AddComponent<UnitView>();
            }
            view.SetCombatState(UnitCombatState.Walking);
            if (unit.Side == UnitSide.Enemy)
            {
                view.ApplyTint(enemyTintColor);
            }
            if (spawnEffects)
            {
                view.PlaySpawnIn();
                SpawnEffect.Play(unit.WorldPosition, UnitHeight,
                    unit.Side == UnitSide.Enemy ? enemyTintColor : ownSparkColor);
            }
            if (summonSounds)
            {
                int index = (int)unit.Type;
                GameAudio.PlaySummon(unit.Type, summonClips != null && index < summonClips.Length ? summonClips[index] : null);
            }
            activeViews[unit.Id] = view;
        }
        private static void NormalizeUnitHeight(GameObject instance, float targetHeightMeters)
        {
            var renderers = instance.GetComponentsInChildren<Renderer>();
            if (renderers.Length == 0) return;
            var bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);
            float currentHeight = bounds.size.y;
            if (currentHeight < 0.0001f) return;
            instance.transform.localScale *= targetHeightMeters / currentHeight;
        }
        private void OnUnitDespawned(int unitId)
        {
            if (!activeViews.TryGetValue(unitId, out var view)) return;
            activeViews.Remove(unitId);
            boltReadyAt.Remove(unitId);
            view.PlayDieAndDestroy();
        }

        // Your own tower losing HP (enemy hits, enemy lightning, standing out of bounds) gives a short pulse, never a long buzz.
        private void UpdateDamageHaptics()
        {
            float hp = matchState.OwnTowerHP;
            if (lastOwnTowerHp >= 0f && hp < lastOwnTowerHp) damageSincePulse += lastOwnTowerHp - hp;
            lastOwnTowerHp = hp;

            if (!damageHaptics || !matchState.MatchStarted || damageSincePulse <= 0f || Time.time < nextPulseAt) return;

            var strength = damageSincePulse < lightBelowDamage ? Haptics.Strength.Light
                : (damageSincePulse < heavyAboveDamage ? Haptics.Strength.Medium : Haptics.Strength.Heavy);
            Haptics.Pulse(strength);
            damageSincePulse = 0f;
            nextPulseAt = Time.time + hapticMinIntervalSeconds;
        }

        private void Update()
        {
            bool inMatch = matchState.MatchStarted && !matchState.HasEnded;
            var track = !inMatch ? MusicTrack.Menu : (matchState.DoubleManaActive ? MusicTrack.LastMinute : MusicTrack.Match);
            GameMusic.Tick(backgroundMusic, track, menuMusicClip, musicClip, musicClipLastMinute, inMatch ? musicVolume : menuMusicVolume);
            UpdateDamageHaptics();
            float interpolationT = matchState.TickIntervalSeconds > 0f
                ? Mathf.Clamp01(matchState.TimeSinceLastTick / matchState.TickIntervalSeconds)
                : 1f;
            foreach (var unit in matchState.Units)
            {
                if (!activeViews.TryGetValue(unit.Id, out var view) || view == null) continue;
                view.transform.position = Vector3.Lerp(unit.PreviousWorldPosition, unit.WorldPosition, interpolationT);
                Quaternion facing = Quaternion.LookRotation(unit.FacingDirection, Vector3.up);
                view.transform.rotation = Quaternion.Slerp(view.transform.rotation, facing, 1f - Mathf.Exp(-14f * Time.deltaTime));
                view.SetCombatState(unit.CombatState);

                if (rangedBolts && unit.IsRanged && unit.CombatState == UnitCombatState.Attacking) TryFireBolt(unit, view);
                else boltReadyAt.Remove(unit.Id);
            }
        }

        // A ranged unit that is attacking sends a bolt at its target every boltIntervalSeconds. The first goes out at once.
        private void TryFireBolt(UnitState unit, UnitView view)
        {
            if (boltReadyAt.TryGetValue(unit.Id, out float readyAt) && Time.time < readyAt) return;

            Transform targetTransform = null;
            Vector3 targetOffset = Vector3.up * (UnitHeight * 0.55f);
            Vector3 fallbackPoint;
            if (unit.AttackTargetUnitId.HasValue)
            {
                if (!activeViews.TryGetValue(unit.AttackTargetUnitId.Value, out var targetView) || targetView == null) return;
                targetTransform = targetView.transform;
                fallbackPoint = targetTransform.position + targetOffset;
            }
            else
            {
                fallbackPoint = matchState.TowerPositionAttackedBy(unit.Side) + Vector3.up * (towerAimHeightMeters * matchState.ArenaScale);
            }

            boltReadyAt[unit.Id] = Time.time + boltIntervalSeconds;
            Vector3 origin = view.transform.position + Vector3.up * (UnitHeight * 0.65f)
                + unit.FacingDirection * (UnitHeight * 0.3f);
            RangedBolt.Fire(origin, targetTransform, targetOffset, fallbackPoint,
                unit.Side == UnitSide.Enemy ? enemyBoltColor : ownBoltColor, UnitHeight * 0.22f, boltSpeedMetersPerSecond * matchState.ArenaScale);
        }
    }
}
