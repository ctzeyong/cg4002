using System;
using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Networking;
using BridgeRush.Tracking;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        // This method is called on the host when a remote gesture request is received from the client. 
        // It checks if the request is valid and applies the requested action (spawn or spell) if possible. 
        // Returns true if the request was successfully applied, false otherwise.
        public bool ApplyRemoteGestureRequest(GestureRequest request)
        {
            if (Role != NetworkRole.Host || request == null) return false;

            if (request.action == "spawn")
            {
                if (!Enum.TryParse(request.unitType, out UnitType type)) return false;
                Vector3? aimPosition = request.hasAim ? FromCanonical(request.along, request.lat) : null;
                return TrySpawnEnemyUnit(type, aimPosition);
            }
            if (request.action == "spell")
            {
                if (request.spell == "lightning") return CastRemoteLightning(request);
                if (request.spell == "fireball") return CastRemoteFireball(request);
                return false;
            }
            return false;
        }

        // This method builds a snapshot of the current game state, including tower health, mana, match time, and unit states.
        // It is used to synchronize the game state between the host and client in a networked match.
        public StateSnapshot BuildStateSnapshot()
        {
            var snapshot = new StateSnapshot
            {
                hostTowerHP = OwnTowerHP,
                hostTowerMaxHP = OwnTowerMaxHP,
                player2TowerHP = OpponentTowerHP,
                player2TowerMaxHP = OpponentTowerMaxHP,
                hostMana = ManaCurrent,
                player2Mana = enemyManaCurrent,
                matchTimeRemaining = matchTimeRemaining,
                units = new List<UnitSnapshot>(units.Count),
                strikes = new List<StrikeSnapshot>()
            };

            foreach (var unit in units)
            {
                var (along, lat) = ToCanonical(unit.WorldPosition);
                var (faceAlong, faceLat) = ToCanonicalDirection(unit.FacingDirection);
                snapshot.units.Add(new UnitSnapshot
                {
                    id = unit.Id,
                    type = unit.Type.ToString(),
                    owner = unit.Side == UnitSide.Own ? "player1" : "player2",
                    along = along,
                    lat = lat,
                    hp = unit.CurrentHP,
                    maxHp = unit.MaxHP,
                    state = unit.CombatState.ToString(),
                    target = unit.AttackTargetUnitId ?? -1,
                    range = unit.AttackRangeMeters,
                    faceAlong = faceAlong,
                    faceLat = faceLat
                });
            }
            AppendStrikeSnapshots(snapshot);
            return snapshot;
        }

        // This method applies a state snapshot received from the host to update the client's game state.
        // It updates tower health, mana, match time, and unit states based on the snapshot
        public void ApplyRemoteState(StateSnapshot snapshot)
        {
            if (Role != NetworkRole.Client || snapshot == null) return;

            OwnTowerHP = snapshot.player2TowerHP;
            OpponentTowerHP = snapshot.hostTowerHP;
            ManaCurrent = snapshot.player2Mana;
            matchTimeRemaining = snapshot.matchTimeRemaining;
            timeSinceLastTick = 0f;
            CheckForMatchEnd();

            var seenIds = new HashSet<int>();
            if (snapshot.units != null)
            {
                foreach (var u in snapshot.units)
                {
                    seenIds.Add(u.id);
                    var combatState = u.state == nameof(UnitCombatState.Attacking) ? UnitCombatState.Attacking : UnitCombatState.Walking;
                    Vector3 worldPos = FromCanonical(u.along, u.lat);

                    var existing = FindById(u.id);
                    if (existing == null)
                    {
                        if (!Enum.TryParse(u.type, out UnitType type)) continue;
                        var side = u.owner == "player2" ? UnitSide.Own : UnitSide.Enemy;
                        var unit = new UnitState(u.id, type, side, u.maxHp, 0f, worldPos)
                        {
                            CurrentHP = u.hp,
                            CombatState = combatState,
                            AttackRangeMeters = u.range,
                            AttackTargetUnitId = u.target >= 0 ? u.target : (int?)null
                        };
                        if (HasFacing(u)) unit.FacingDirection = FromCanonicalDirection(u.faceAlong, u.faceLat);
                        units.Add(unit);
                        if (u.id >= nextUnitId) nextUnitId = u.id + 1;
                        UnitSpawned?.Invoke(unit);
                    }
                    else
                    {
                        existing.PreviousWorldPosition = existing.WorldPosition;
                        existing.WorldPosition = worldPos;
                        if (HasFacing(u))
                        {
                            existing.FacingDirection = FromCanonicalDirection(u.faceAlong, u.faceLat);
                        }
                        else
                        {
                            // A host that does not send facing: turn the unit the way it moved.
                            Vector3 travelled = FlattenY(worldPos) - FlattenY(existing.PreviousWorldPosition);
                            if (travelled.sqrMagnitude > 0.000001f) existing.FacingDirection = travelled.normalized;
                        }
                        existing.AttackRangeMeters = u.range;
                        existing.AttackTargetUnitId = u.target >= 0 ? u.target : (int?)null;
                        existing.CurrentHP = u.hp;
                        existing.CombatState = combatState;
                    }
                }
            }

            ApplyStrikeSnapshots(snapshot);

            for (int i = units.Count - 1; i >= 0; i--)
            {
                if (seenIds.Contains(units[i].Id)) continue;
                int deadId = units[i].Id;
                units.RemoveAt(i);
                UnitDespawned?.Invoke(deadId);
            }
        }

        private static bool HasFacing(UnitSnapshot u) => u.faceAlong * u.faceAlong + u.faceLat * u.faceLat > 0.0001f;

        // A direction on the floor as (along the lane, across the lane), in the same canonical frame as ToCanonical.
        private (float along, float lat) ToCanonicalDirection(Vector3 direction)
        {
            Vector3 canonicalDirection = LocalIsPlayer1 ? fixedLaneDirection : -fixedLaneDirection;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, canonicalDirection).normalized;
            Vector3 flat = new Vector3(direction.x, 0f, direction.z);
            return (Vector3.Dot(flat, canonicalDirection), Vector3.Dot(flat, lateralAxis));
        }

        private Vector3 FromCanonicalDirection(float along, float lat)
        {
            Vector3 canonicalDirection = LocalIsPlayer1 ? fixedLaneDirection : -fixedLaneDirection;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, canonicalDirection).normalized;
            Vector3 direction = canonicalDirection * along + lateralAxis * lat;
            direction.y = 0f;
            return direction.sqrMagnitude > 0.000001f ? direction.normalized : Vector3.forward;
        }

        // Determines if the local player is considered Player 1 based on the match setup and selected player.
        private bool LocalIsPlayer1 =>
            matchSetup == null || matchSetup.Selected != MatchSetup.SelectedPlayer.Player2;

        // projects a world position onto this phone's own lane, returning the longitudinal and lateral offsets in canonical coordinates.
        private (float along, float lat) ToCanonical(Vector3 worldPosition)
        {
            Vector3 canonicalOrigin = LocalIsPlayer1 ? ownHomeAnchor : opponentHomeAnchor;
            Vector3 canonicalDirection = LocalIsPlayer1 ? fixedLaneDirection : -fixedLaneDirection;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, canonicalDirection).normalized;

            Vector3 flat = FlattenY(worldPosition);
            float longitudinal = Vector3.Dot(flat - canonicalOrigin, canonicalDirection);
            Vector3 onSpine = canonicalOrigin + canonicalDirection * longitudinal;
            float lateral = Vector3.Dot(flat - onSpine, lateralAxis);

            float along = totalLaneLength > 0.0001f ? longitudinal / totalLaneLength : 0f;
            float lat = LaneWidthMeters > 0.0001f ? lateral / LaneWidthMeters : 0f;
            return (along, lat);
        }

        //reverse of ToCanonical, converts canonical coordinates back to world position, taking into account the lane's origin and direction.
        private Vector3 FromCanonical(float along, float lat)
        {
            Vector3 canonicalOrigin = LocalIsPlayer1 ? ownHomeAnchor : opponentHomeAnchor;
            Vector3 canonicalDirection = LocalIsPlayer1 ? fixedLaneDirection : -fixedLaneDirection;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, canonicalDirection).normalized;

            Vector3 onSpine = canonicalOrigin + canonicalDirection * (along * totalLaneLength);
            Vector3 flat = onSpine + lateralAxis * (lat * LaneWidthMeters);
            return new Vector3(flat.x, ComputeUnitHeight(flat), flat.z);
        }
    }
}
