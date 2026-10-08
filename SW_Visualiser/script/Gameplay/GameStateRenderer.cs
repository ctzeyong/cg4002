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
        [SerializeField] private float unitTargetHeightMeters = 0.15f;
        [SerializeField] private Color enemyTintColor = new(1f, 0.35f, 0.35f);
        private readonly Dictionary<int, UnitView> activeViews = new();
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
        private void OnUnitSpawned(UnitState unit)
        {
            if (!unitPrefabCatalog.TryGetPrefab(unit.Type, out var prefab)) return;
            var instance = Instantiate(prefab, unitContainer);
            NormalizeUnitHeight(instance, unitTargetHeightMeters);
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
            view.PlayDieAndDestroy();
        }
        public void ApplyOverchargeAura(float durationSeconds)
        {
            foreach (var view in activeViews.Values)
            {
                if (view != null) view.PlayOverchargeAura(durationSeconds);
            }
        }
        private void Update()
        {
            float interpolationT = matchState.TickIntervalSeconds > 0f
                ? Mathf.Clamp01(matchState.TimeSinceLastTick / matchState.TickIntervalSeconds)
                : 1f;
            foreach (var unit in matchState.Units)
            {
                if (!activeViews.TryGetValue(unit.Id, out var view) || view == null) continue;
                view.transform.position = Vector3.Lerp(unit.PreviousWorldPosition, unit.WorldPosition, interpolationT);
                view.transform.rotation = Quaternion.LookRotation(unit.FacingDirection, Vector3.up);
                view.SetCombatState(unit.CombatState);
            }
        }
    }
}
