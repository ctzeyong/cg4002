using UnityEngine;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        public bool SpawnUnit(UnitType type)
        {
            if (Role == NetworkRole.BoardClient) return BoardSendSummon(type);
            bool done = SpawnUnitCore(type);
            if (!done) OwnUnitRejected?.Invoke(type, ShortSpawnFailure(type));
            return done;
        }

        private bool SpawnUnitCore(UnitType type)
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
                OwnUnitSummoned?.Invoke(type);
                return true;
            }
            if (!TrySpendMana(manaCost)) return false;

            if (!CreateUnit(type, UnitSide.Own))
            {
                if (!GodMode) ManaCurrent += manaCost;
                return false;
            }
            OwnUnitSummoned?.Invoke(type);
            return true;
        }
        private enum SpawnBlock { None, NotStarted, BadAim, OutsideArena, NoMana }

        // Same checks as SpawnUnitCore, in the same order, so the cards and the gesture banner can say why a summon was refused.
        private SpawnBlock GetSpawnBlock(UnitType type)
        {
            if (!matchStarted) return SpawnBlock.NotStarted;
            if (summonCursor != null && !summonCursor.IsAimValid) return SpawnBlock.BadAim;
            if (arenaFrame.HasLane && !IsInsideArenaBounds(OwnTowerPosition)) return SpawnBlock.OutsideArena;
            if (ManaCurrent < GetManaCost(type)) return SpawnBlock.NoMana;
            return SpawnBlock.None;
        }

        public string DescribeSpawnFailure(UnitType type)
        {
            if (Role == NetworkRole.BoardClient) return BoardRefusalText;
            switch (GetSpawnBlock(type))
            {
                case SpawnBlock.NotStarted: return "The match hasn't started yet. Press Start Game first.";
                case SpawnBlock.BadAim: return "Aim the crosshair at the ground on your own side of the arena.";
                case SpawnBlock.OutsideArena: return "You are outside the arena. Walk back to your side.";
                case SpawnBlock.NoMana: return $"Not enough mana: need {GetManaCost(type):0.#}, you have {ManaCurrent:0.#}.";
                default: return "Couldn't place the unit there. Try aiming somewhere else.";
            }
        }

        public string ShortSpawnFailure(UnitType type)
        {
            switch (GetSpawnBlock(type))
            {
                case SpawnBlock.NotStarted: return "NOT STARTED";
                case SpawnBlock.BadAim: return "BAD AIM";
                case SpawnBlock.OutsideArena: return "OUT OF ARENA";
                case SpawnBlock.NoMana: return "NO MANA";
                default: return "CAN'T PLACE";
            }
        }
        // explicitSpawnPosition carries a remote player's own aimed crosshair position (see ApplyRemoteGestureRequest) -
        // without it, an enemy unit has no aim of its own and spawns at the tower, same as the random bot.
        public bool TrySpawnEnemyUnit(UnitType type, Vector3? explicitSpawnPosition = null)
        {
            if (!matchStarted || Role == NetworkRole.BoardClient) return false;
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
            float attackRange = 0f;
            if (unitStatsTable.TryGetEntry(type, out var entry))
            {
                maxHP = entry.MaxHP;
                attackDps = entry.AttackDps;
                attackRange = Mathf.Max(0f, entry.AttackRangeMeters) * ArenaScale;
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
                float forwardOffset = towerController != null ? towerController.TowerRadius : spawnForwardOffsetMeters * ArenaScale;
                spawnPosition = spawnAnchor + forwardDirection * forwardOffset;
                spawnPosition = ClampToArenaBounds(spawnPosition);
            }

            var unit = new UnitState(nextUnitId++, type, side, maxHP, attackDps, spawnPosition) { AttackRangeMeters = attackRange };
            units.Add(unit);
            UnitSpawned?.Invoke(unit);
            return true;
        }

        // Offline/mock only (BubbleSync's mock toggle). In a real match the Ultra96 awards bubble mana, never the phone.
        public void MockAwardMana(float amount) => ManaCurrent = Mathf.Min(manaMax, ManaCurrent + amount);

        public bool TrySpendMana(float amount)
        {
            if (GodMode) return true;
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
