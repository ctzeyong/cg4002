using UnityEngine;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        private void SimulateTick(float tickDeltaTime)
        {
            float towerRadius = towerController != null ? towerController.TowerRadius : 0f;
            var lane = arenaFrame.Lane;
            Vector3 liveOwnTowerPosition = OwnTowerPosition;
            Vector3 liveOpponentTowerPosition = lane.Origin + lane.Direction * lane.Length;

            foreach (var unit in units)
            {
                if (unit.BridgeIndex < 0) unit.BridgeIndex = ChooseBridge(unit.WorldPosition);
            }

            foreach (var unit in units)
            {
                unit.PreviousWorldPosition = unit.WorldPosition;
                if (unit.CombatState != UnitCombatState.Walking) continue;

                // Notice an enemy, walk toward it, and only start hitting once it is within this unit's hit reach.
                var enemy = ResolveEnemyTarget(unit);
                if (enemy != null)
                {
                    if (FlatDistance(unit, enemy) <= HitReachOf(unit))
                    {
                        unit.CombatState = UnitCombatState.Attacking;
                        unit.AttackTargetUnitId = enemy.Id;
                        unit.ChaseTargetUnitId = null;
                        continue;
                    }
                    unit.ChaseTargetUnitId = enemy.Id;
                }
                else
                {
                    unit.ChaseTargetUnitId = null;
                }

                float secondsToCrossLane = unitStatsTable.GetSecondsToCrossLane(unit.Type);
                float speedMetersPerSecond = secondsToCrossLane > 0.0001f
                    ? (totalLaneLength / secondsToCrossLane) * globalSpeedMultiplier
                    : 0f;
                Vector3 targetTowerPosition = unit.Side == UnitSide.Own ? liveOpponentTowerPosition : liveOwnTowerPosition;
                Vector3 laneForward = unit.Side == UnitSide.Own ? fixedLaneDirection : -fixedLaneDirection;
                int bridgeIndex = Mathf.Clamp(unit.BridgeIndex, 0, bridgeLateralOffsets.Length - 1);
                UnitState chaseTarget = unit.ChaseTargetUnitId.HasValue ? FindById(unit.ChaseTargetUnitId.Value) : null;
                Vector3 newPosition;
                if (chaseTarget != null)
                {
                    Vector3 flatWorldPosition = FlattenY(unit.WorldPosition);
                    Vector3 toChase = FlattenY(chaseTarget.WorldPosition) - flatWorldPosition;
                    Vector3 chaseDirection = toChase.sqrMagnitude > 0.0001f ? toChase.normalized : Vector3.zero;
                    float step = Mathf.Min(speedMetersPerSecond * tickDeltaTime, Mathf.Max(0f, toChase.magnitude - HitReachOf(unit) * 0.9f));
                    newPosition = ClampToArenaBounds(flatWorldPosition + chaseDirection * step, 0f, unit.PastBridge ? -1 : bridgeIndex);
                    newPosition.y = ComputeUnitHeight(newPosition);
                }
                else if (!unit.PastBridge)
                {
                    Vector3 forwardDirection = laneForward;
                    float scaledLookAhead = lookAheadMeters * ArenaScale;
                    float lookAheadLongitudinal = unit.Side == UnitSide.Own ? scaledLookAhead : -scaledLookAhead;
                    Vector3 flatWorldPosition = FlattenY(unit.WorldPosition);
                    float longitudinal = Vector3.Dot(flatWorldPosition - ownHomeAnchor, fixedLaneDirection);
                    Vector3 closestSpinePoint = ownHomeAnchor + fixedLaneDirection * longitudinal + BridgeLateralAxis * bridgeLateralOffsets[bridgeIndex];
                    float distanceToBridge = Vector3.Distance(flatWorldPosition, BridgePositionOf(bridgeIndex));
                    float influenceRadius = Mathf.Max(0.0001f, centeringInfluenceRadiusFraction * totalLaneLength);
                    float centeringInfluence = Mathf.Clamp01(1f - distanceToBridge / influenceRadius);
                    Vector3 centeringTarget = Vector3.Lerp(flatWorldPosition, closestSpinePoint, centeringInfluence);
                    Vector3 centeredPosition = Vector3.SmoothDamp(flatWorldPosition, centeringTarget, ref unit.LateralVelocity, laneCenteringSmoothTime, speedMetersPerSecond, tickDeltaTime);
                    Vector3 combinedStep = (centeredPosition - flatWorldPosition) + forwardDirection * speedMetersPerSecond * tickDeltaTime;
                    combinedStep = Vector3.ClampMagnitude(combinedStep, speedMetersPerSecond * tickDeltaTime);
                    newPosition = flatWorldPosition + combinedStep;
                    newPosition = ClampToArenaBounds(newPosition, lookAheadLongitudinal, bridgeIndex);
                    newPosition.y = ComputeUnitHeight(newPosition);
                }
                else
                {
                    Vector3 flatWorldPosition = FlattenY(unit.WorldPosition);
                    Vector3 toTarget = targetTowerPosition - flatWorldPosition;
                    Vector3 forwardDirection = toTarget.sqrMagnitude > 0.0001f ? toTarget.normalized : Vector3.zero;
                    newPosition = ClampToArenaBounds(flatWorldPosition + forwardDirection * speedMetersPerSecond * tickDeltaTime);
                    newPosition.y = ComputeUnitHeight(newPosition);
                }

                if (!unit.PastBridge)
                {
                    Vector3 exitEdge = unit.Side == UnitSide.Own ? opponentBridgeEdge : ownBridgeEdge;
                    if (Vector3.Dot(newPosition - exitEdge, laneForward) >= 0f) unit.PastBridge = true;
                }

                unit.WorldPosition = newPosition;
                Vector3 travelDelta = FlattenY(unit.WorldPosition) - FlattenY(unit.PreviousWorldPosition);
                if (travelDelta.sqrMagnitude > 0.000001f) unit.FacingDirection = travelDelta.normalized;
                if (Vector3.Distance(FlattenY(unit.WorldPosition), targetTowerPosition) <= StandoffFor(unit, towerRadius))
                {
                    unit.CombatState = UnitCombatState.Attacking;
                    unit.AttackTargetUnitId = null;
                    unit.ChaseTargetUnitId = null;
                }
            }

            foreach (var unit in units)
            {
                // A unit killed earlier in this tick must not still perform an attack.
                if (unit.CurrentHP <= 0f) continue;
                if (unit.CombatState != UnitCombatState.Attacking) continue;

                if (unit.AttackTargetUnitId == null)
                {
                    Vector3 targetTowerPosition = unit.Side == UnitSide.Own ? liveOpponentTowerPosition : liveOwnTowerPosition;

                    Vector3 toTower = targetTowerPosition - FlattenY(unit.WorldPosition);
                    if (toTower.sqrMagnitude > 0.0001f) unit.FacingDirection = toTower.normalized;

                    if (Vector3.Distance(FlattenY(unit.WorldPosition), targetTowerPosition) > StandoffFor(unit, towerRadius) + DisengageHysteresis)
                    {
                        unit.CombatState = UnitCombatState.Walking;
                        continue;
                    }

                    if (unit.Side == UnitSide.Own)
                        OpponentTowerHP = Mathf.Max(0f, OpponentTowerHP - unit.AttackDps * tickDeltaTime);
                    else
                        OwnTowerHP = Mathf.Max(0f, OwnTowerHP - unit.AttackDps * tickDeltaTime);

                    unit.CurrentHP -= towerCounterDps * tickDeltaTime;
                    continue;
                }

                var target = FindById(unit.AttackTargetUnitId.Value);
                if (target == null || target.CurrentHP <= 0f)
                {
                    unit.CombatState = UnitCombatState.Walking;
                    unit.AttackTargetUnitId = null;
                    continue;
                }

                Vector3 toTargetUnit = FlattenY(target.WorldPosition) - FlattenY(unit.WorldPosition);
                if (toTargetUnit.sqrMagnitude > 0.0001f) unit.FacingDirection = toTargetUnit.normalized;

                // A target that has moved out of reach is chased again; the unit only hits while it is within reach.
                if (toTargetUnit.magnitude > HitReachOf(unit) + DisengageHysteresis)
                {
                    unit.CombatState = UnitCombatState.Walking;
                    unit.AttackTargetUnitId = null;
                    unit.ChaseTargetUnitId = target.Id;
                    continue;
                }

                // Each attacker damages only the single enemy stored in AttackTargetUnitId.
                // Multiple attackers may focus the same target, but damage is never applied
                // to unrelated units in the surrounding group.
                target.CurrentHP -= unit.AttackDps * tickDeltaTime;
            }

            for (int i = units.Count - 1; i >= 0; i--)
            {
                if (units[i].CurrentHP > 0f) continue;
                int deadUnitId = units[i].Id;
                units.RemoveAt(i);
                UnitDespawned?.Invoke(deadUnitId);
            }
        }

        // How far from the tower's centre a unit stops to attack it: melee units stand at the tower's edge plus the
        // clearance, ranged units stand back by their attack range.
        private float StandoffFor(UnitState unit, float towerRadius) =>
            towerRadius + (unit.IsRanged ? unit.AttackRangeMeters : TowerClearance);

        // Melee units notice at the notice range. Ranged units notice at least as far as they can shoot.
        private float NoticeRangeOf(UnitState unit) => unit.IsRanged ? Mathf.Max(NoticeRangeMeters, unit.AttackRangeMeters) : NoticeRangeMeters;

        // Melee units hit at the hit reach, ranged units at their attack range.
        private float HitReachOf(UnitState unit) => unit.IsRanged ? unit.AttackRangeMeters : HitReachMeters;

        private float FlatDistance(UnitState a, UnitState b) => Vector3.Distance(FlattenY(a.WorldPosition), FlattenY(b.WorldPosition));

        // Two bridges are separated by water: units on different bridges cannot reach each other across it,
        // except a ranged unit shooting from inside its own range.
        private bool BlockedByWater(UnitState unit, UnitState other, float distance)
        {
            if (bridgeLateralOffsets.Length <= 1 || other.BridgeIndex == unit.BridgeIndex) return false;
            if (!IsOnBridgeSpan(unit.WorldPosition) && !IsOnBridgeSpan(other.WorldPosition)) return false;
            return !(unit.IsRanged && distance <= unit.AttackRangeMeters);
        }

        // Keeps the enemy the unit is already chasing while it stays in notice range, otherwise picks the nearest one.
        private UnitState ResolveEnemyTarget(UnitState unit)
        {
            float notice = NoticeRangeOf(unit);

            if (unit.ChaseTargetUnitId.HasValue)
            {
                var current = FindById(unit.ChaseTargetUnitId.Value);
                if (current != null && current.CurrentHP > 0f && current.Side != unit.Side)
                {
                    float distance = FlatDistance(unit, current);
                    if (distance <= notice + DisengageHysteresis && !BlockedByWater(unit, current, distance)) return current;
                }
                unit.ChaseTargetUnitId = null;
            }

            UnitState nearest = null;
            float nearestDistance = notice;
            foreach (var other in units)
            {
                if (other.Side == unit.Side || other.CurrentHP <= 0f) continue;

                float distance = FlatDistance(unit, other);
                if (distance > nearestDistance || BlockedByWater(unit, other, distance)) continue;
                nearest = other;
                nearestDistance = distance;
            }
            return nearest;
        }
    }
}
