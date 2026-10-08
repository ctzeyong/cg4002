using UnityEngine;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        public bool SpawnUnit(UnitType type)
        {
            if (!matchStarted) return false;

            if (summonCursor != null && !summonCursor.IsAimValid)
            {
                return false;
            }
            if (arenaFrame.HasLane && !IsInsideArenaBounds(OwnTowerPosition))
            {
                return false;
            }
            float manaCost = GetManaCost(type);

            if (Role == NetworkRole.Client)
            {
                if (ManaCurrent < manaCost) return false;
                ManaCurrent -= manaCost;

                bool hasAim = summonCursor != null && summonCursor.IsAimValid;
                float along = 0f, lat = 0f;
                if (hasAim) (along, lat) = ToCanonical(summonCursor.AimedWorldPosition);
                networkSync?.PublishGestureRequest("spawn", type.ToString(), null, hasAim, along, lat);
                return true;
            }
            if (!TrySpendMana(manaCost)) return false;

            if (!CreateUnit(type, UnitSide.Own))
            {
                ManaCurrent += manaCost;
                return false;
            }
            return true;
        }
        // explicitSpawnPosition carries a remote player's own aimed crosshair position (see ApplyRemoteGestureRequest) -
        // without it, an enemy unit has no aim of its own and spawns at the tower, same as the random bot.
        public bool TrySpawnEnemyUnit(UnitType type, Vector3? explicitSpawnPosition = null)
        {
            if (!matchStarted) return false;
            Vector3 opponentCurrentPosition = Vector3.zero;
            bool hasOpponentPosition = false;
            if (arenaFrame.HasLane)
            {
                var lane = arenaFrame.Lane;
                opponentCurrentPosition = lane.Origin + lane.Direction * lane.Length;
                hasOpponentPosition = true;
            }
            if (hasOpponentPosition && !IsInsideArenaBounds(opponentCurrentPosition))
            {
                return false;
            }
            float manaCost = GetManaCost(type);

            if (enemyManaCurrent < manaCost) return false;
            enemyManaCurrent -= manaCost;
            if (!CreateUnit(type, UnitSide.Enemy, explicitSpawnPosition))
            {
                enemyManaCurrent += manaCost;
                return false;
            }
            return true;
        }
        private float GetManaCost(UnitType type) =>
            unitStatsTable.TryGetEntry(type, out var entry) ? entry.ManaCost : 0f;

        private bool CreateUnit(UnitType type, UnitSide side, Vector3? explicitSpawnPosition = null)
        {
            if (!arenaFrame.HasLane) return false;

            float maxHP = 1f;
            float attackDps = 0f;
            if (unitStatsTable.TryGetEntry(type, out var entry))
            {
                maxHP = entry.MaxHP;
                attackDps = entry.AttackDps;
            }

            var lane = arenaFrame.Lane;
            Vector3 spawnPosition;
            if (explicitSpawnPosition.HasValue)
            {
                // A remote player's own aimed position, already inside their territory on their end - re-clamp
                // defensively since it crossed the network, but don't re-derive it from this phone's lane.
                spawnPosition = ClampToArenaBounds(explicitSpawnPosition.Value);
            }
            else if (side == UnitSide.Own && summonCursor != null && summonCursor.IsAimValid)
            {
                // Already validated against the territory's own (fixed-width) bounds by the reticle -
                // do not additionally clamp against the narrower funnel shape, or a corner spawn near
                // the bridge snaps inward the instant it's created.
                spawnPosition = summonCursor.AimedWorldPosition;
            }
            else
            {
                Vector3 spawnAnchor = side == UnitSide.Own ? OwnTowerPosition : lane.Origin + lane.Direction * lane.Length;
                Vector3 forwardDirection = side == UnitSide.Own ? lane.Direction : -lane.Direction;
                float forwardOffset = towerController != null ? towerController.TowerRadius : spawnForwardOffsetMeters;
                spawnPosition = spawnAnchor + forwardDirection * forwardOffset;
                spawnPosition = ClampToArenaBounds(spawnPosition);
            }

            var unit = new UnitState(nextUnitId++, type, side, maxHP, attackDps, spawnPosition);
            units.Add(unit);
            UnitSpawned?.Invoke(unit);
            return true;
        }

        public bool TrySpendMana(float amount)
        {
            if (ManaCurrent < amount) return false;
            ManaCurrent -= amount;
            return true;
        }

        private void UpdateEnemyBot(float deltaTime)
        {
            timeUntilNextEnemyBotAttempt -= deltaTime;
            if (timeUntilNextEnemyBotAttempt > 0f) return;

            timeUntilNextEnemyBotAttempt = UnityEngine.Random.Range(enemyBotSpawnIntervalMinSeconds, enemyBotSpawnIntervalMaxSeconds);
            var type = AllUnitTypes[UnityEngine.Random.Range(0, AllUnitTypes.Length)];
            TrySpawnEnemyUnit(type);
        }
    }
}
