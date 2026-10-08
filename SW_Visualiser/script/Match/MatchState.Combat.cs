using UnityEngine;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        private void SimulateTick(float tickDeltaTime)
        {
            float towerRadius = towerController != null ? towerController.TowerRadius : 0f;
            float standoffMeters = towerRadius + towerClearanceMeters;
            var lane = arenaFrame.Lane;
            Vector3 liveOwnTowerPosition = OwnTowerPosition;
            Vector3 liveOpponentTowerPosition = lane.Origin + lane.Direction * lane.Length;

            foreach (var unit in units)
            {
                unit.PreviousWorldPosition = unit.WorldPosition;
                if (unit.CombatState != UnitCombatState.Walking) continue;
                var enemy = FindNearestEnemyInRange(unit);
                if (enemy != null)
                {
                    unit.CombatState = UnitCombatState.Attacking;
                    unit.AttackTargetUnitId = enemy.Id;
                    continue;
                }
                float secondsToCrossLane = unitStatsTable.GetSecondsToCrossLane(unit.Type);
                float speedMetersPerSecond = secondsToCrossLane > 0.0001f
                    ? (totalLaneLength / secondsToCrossLane) * globalSpeedMultiplier
                    : 0f;
                Vector3 targetTowerPosition = unit.Side == UnitSide.Own ? liveOpponentTowerPosition : liveOwnTowerPosition;
                Vector3 newPosition;
                if (!unit.PastBridge)
                {
                    Vector3 forwardDirection = unit.Side == UnitSide.Own ? fixedLaneDirection : -fixedLaneDirection;
                    float lookAheadLongitudinal = unit.Side == UnitSide.Own ? lookAheadMeters : -lookAheadMeters;
                    Vector3 flatWorldPosition = FlattenY(unit.WorldPosition);
                    float longitudinal = Vector3.Dot(flatWorldPosition - ownHomeAnchor, fixedLaneDirection);
                    Vector3 closestSpinePoint = ownHomeAnchor + fixedLaneDirection * longitudinal;
                    float distanceToBridge = Vector3.Distance(flatWorldPosition, bridgePosition);
                    float influenceRadius = Mathf.Max(0.0001f, centeringInfluenceRadiusFraction * totalLaneLength);
                    float centeringInfluence = Mathf.Clamp01(1f - distanceToBridge / influenceRadius);
                    Vector3 centeringTarget = Vector3.Lerp(flatWorldPosition, closestSpinePoint, centeringInfluence);
                    Vector3 centeredPosition = Vector3.SmoothDamp(flatWorldPosition, centeringTarget, ref unit.LateralVelocity, laneCenteringSmoothTime, speedMetersPerSecond, tickDeltaTime);
                    Vector3 combinedStep = (centeredPosition - flatWorldPosition) + forwardDirection * speedMetersPerSecond * tickDeltaTime;
                    combinedStep = Vector3.ClampMagnitude(combinedStep, speedMetersPerSecond * tickDeltaTime);
                    newPosition = flatWorldPosition + combinedStep;
                    newPosition = ClampToArenaBounds(newPosition, lookAheadLongitudinal);
                    newPosition.y = ComputeUnitHeight(newPosition);

                    Vector3 exitEdge = unit.Side == UnitSide.Own ? opponentBridgeEdge : ownBridgeEdge;
                    if (Vector3.Dot(newPosition - exitEdge, forwardDirection) >= 0f)
                    {
                        unit.PastBridge = true;
                    }
                }
                else
                {
                    Vector3 flatWorldPosition = FlattenY(unit.WorldPosition);
                    Vector3 toTarget = targetTowerPosition - flatWorldPosition;
                    Vector3 forwardDirection = toTarget.sqrMagnitude > 0.0001f ? toTarget.normalized : Vector3.zero;
                    newPosition = ClampToArenaBounds(flatWorldPosition + forwardDirection * speedMetersPerSecond * tickDeltaTime);
                    newPosition.y = ComputeUnitHeight(newPosition);
                }
                unit.WorldPosition = newPosition;
                Vector3 travelDelta = FlattenY(unit.WorldPosition) - FlattenY(unit.PreviousWorldPosition);
                if (travelDelta.sqrMagnitude > 0.0001f) unit.FacingDirection = travelDelta.normalized;
                if (Vector3.Distance(FlattenY(unit.WorldPosition), targetTowerPosition) <= standoffMeters)
                {
                    unit.CombatState = UnitCombatState.Attacking;
                    unit.AttackTargetUnitId = null;
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

                    if (Vector3.Distance(FlattenY(unit.WorldPosition), targetTowerPosition) > standoffMeters + towerDisengageHysteresisMeters)
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

        private UnitState FindNearestEnemyInRange(UnitState unit)
        {
            UnitState nearest = null;
            float range = EngageRangeMeters;
            float nearestDistanceSqr = range * range;
            Vector3 flatPosition = FlattenY(unit.WorldPosition);

            foreach (var other in units)
            {
                if (other.Side == unit.Side) continue;

                float distanceSqr = (FlattenY(other.WorldPosition) - flatPosition).sqrMagnitude;
                if (distanceSqr <= nearestDistanceSqr)
                {
                    nearest = other;
                    nearestDistanceSqr = distanceSqr;
                }
            }

            return nearest;
        }
    }
}
