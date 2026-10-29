using System;
using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Networking;
using BridgeRush.Gameplay;
using BridgeRush.Input;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        // The one place the spell numbers live. SpellCaster registers its Inspector values here at startup, so the
        // local cast and the host's handling of a client's cast always use the same cost, damage and radius.
        public struct SpellSpec
        {
            public float LightningManaCost;
            public float LightningUnitDamage;
            public float LightningTowerDamage;
            public float LightningRadiusMeters;
            public float LightningEdgeDamageFraction;
            public float LightningTelegraphSeconds;

            public float FireballManaCost;
            public float FireballUnitDamage;
            public float FireballTowerDamage;
            public float FireballRadiusMeters;
            public float FireballEdgeDamageFraction;
            public float FireballSpeedMetersPerSecond;
            public float FireballMinFlightSeconds;
            public float FireballBurnSeconds;
            public float FireballBurnDps;

            public static SpellSpec Default => new SpellSpec
            {
                LightningManaCost = 5f,
                LightningUnitDamage = 180f,
                LightningTowerDamage = 60f,
                LightningRadiusMeters = 0.2f,
                LightningEdgeDamageFraction = 0.5f,
                LightningTelegraphSeconds = 0.5f,

                FireballManaCost = 6f,
                FireballUnitDamage = 120f,
                FireballTowerDamage = 40f,
                FireballRadiusMeters = 0.12f,
                FireballEdgeDamageFraction = 0.5f,
                FireballSpeedMetersPerSecond = 1.2f,
                FireballMinFlightSeconds = 0.4f,
                FireballBurnSeconds = 4f,
                FireballBurnDps = 12f
            };
        }

        private SpellSpec spellSpec = SpellSpec.Default;
        public SpellSpec Spells => spellSpec;
        public void RegisterSpellSpec(SpellSpec spec) => spellSpec = spec;

        public SummonCursor AimCursor => summonCursor;

        private class PendingStrike
        {
            public int Id;
            public UnitSide Caster;
            public Vector3 Center;
            public float Remaining;
            public float Age;
            public bool Applied;

            public bool Fireball;
            public Vector3 Start;
            public float Duration;
            public float BurnLeft;
        }

        private readonly List<PendingStrike> strikes = new();
        private readonly HashSet<int> seenStrikeIds = new();
        private int nextStrikeId;

        private const float StrikeKeepSeconds = 0.75f;

        // A lightning telegraph began: where, how long until it lands, and whether the local player threw it.
        // Fired on the host and in VS CPU when the strike is queued, and on the client when a snapshot first lists it.
        public event Action<Vector3, float, bool> LightningStrikeStarted;

        // A fireball was thrown: where it starts, where it lands, the flight time left, the burn time left on the floor,
        // and whether the local player threw it. Fired the same way as LightningStrikeStarted.
        public event Action<Vector3, Vector3, float, float, bool> FireballStarted;

        // The tower a unit of this side is attacking (the other side's tower). Used to aim ranged bolts.
        public Vector3 TowerPositionAttackedBy(UnitSide attacker) =>
            !arenaFrame.HasLane ? Vector3.zero : (attacker == UnitSide.Own ? OpponentTowerWorldPosition : OwnTowerPosition);

        // The caster's own tower: where a fireball starts its flight.
        private Vector3 CasterTowerPosition(UnitSide caster) =>
            !arenaFrame.HasLane ? Vector3.zero : (caster == UnitSide.Own ? OwnTowerPosition : OpponentTowerWorldPosition);

        public float ScaledFireballRadius => spellSpec.FireballRadiusMeters * ArenaScale;

        private Vector3 OpponentTowerWorldPosition
        {
            get
            {
                var lane = arenaFrame.Lane;
                return lane.Origin + lane.Direction * lane.Length;
            }
        }

        private void ClearStrikes()
        {
            strikes.Clear();
            seenStrikeIds.Clear();
            nextStrikeId = 0;
        }

        // Host and local only. The strike lands after the telegraph, hitting everything of the other side in the radius.
        public void QueueLightning(UnitSide caster, Vector3 center)
        {
            if (Role == NetworkRole.Client) return;

            var strike = new PendingStrike
            {
                Id = nextStrikeId++,
                Caster = caster,
                Center = FlattenY(center),
                Remaining = spellSpec.LightningTelegraphSeconds
            };
            strikes.Add(strike);
            LightningStrikeStarted?.Invoke(strike.Center, strike.Remaining, caster == UnitSide.Own);
        }

        // Host and local only. The ball flies from the caster's tower to the aim point, always to the aim point (it does not
        // stop for units on the way), explodes there and leaves a burning patch on the floor.
        public void QueueFireball(UnitSide caster, Vector3 target)
        {
            if (Role == NetworkRole.Client) return;

            Vector3 start = FlattenY(CasterTowerPosition(caster));
            Vector3 end = FlattenY(target);
            float speed = Mathf.Max(0.01f, spellSpec.FireballSpeedMetersPerSecond * ArenaScale);
            float flight = Mathf.Max(spellSpec.FireballMinFlightSeconds, Vector3.Distance(start, end) / speed);

            var strike = new PendingStrike
            {
                Id = nextStrikeId++,
                Caster = caster,
                Center = end,
                Remaining = flight,
                Fireball = true,
                Start = start,
                Duration = flight,
                BurnLeft = spellSpec.FireballBurnSeconds
            };
            strikes.Add(strike);
            FireballStarted?.Invoke(start, end, flight, spellSpec.FireballBurnSeconds, caster == UnitSide.Own);
        }

        private void UpdateStrikes(float deltaTime)
        {
            for (int i = strikes.Count - 1; i >= 0; i--)
            {
                var strike = strikes[i];
                strike.Remaining -= deltaTime;
                strike.Age += deltaTime;
                if (!strike.Applied && strike.Remaining <= 0f)
                {
                    strike.Applied = true;
                    if (strike.Fireball)
                    {
                        ApplyArea(strike.Caster, strike.Center, ScaledFireballRadius, spellSpec.FireballUnitDamage,
                            spellSpec.FireballTowerDamage, spellSpec.FireballEdgeDamageFraction);
                    }
                    else
                    {
                        ApplyArea(strike.Caster, strike.Center, ScaledLightningRadius, spellSpec.LightningUnitDamage,
                            spellSpec.LightningTowerDamage, spellSpec.LightningEdgeDamageFraction);
                    }
                }

                if (strike.Fireball && strike.Applied && strike.BurnLeft > 0f)
                {
                    ApplyBurn(strike.Caster, strike.Center, Mathf.Min(deltaTime, strike.BurnLeft));
                    strike.BurnLeft -= deltaTime;
                }

                float lifetime = strike.Fireball ? strike.Duration + spellSpec.FireballBurnSeconds : spellSpec.LightningTelegraphSeconds;
                if (strike.Applied && strike.Age > lifetime + StrikeKeepSeconds) strikes.RemoveAt(i);
            }
        }

        // Damages every unit of the other side inside the radius (full at the centre, less at the rim), and the other
        // side's tower if it is in range.
        private void ApplyArea(UnitSide caster, Vector3 center, float scaledRadius, float unitDamage, float towerDamage, float edgeFraction)
        {
            float radius = Mathf.Max(0.0001f, scaledRadius);
            UnitSide targetSide = caster == UnitSide.Own ? UnitSide.Enemy : UnitSide.Own;

            foreach (var unit in units)
            {
                if (unit.Side != targetSide || unit.CurrentHP <= 0f) continue;
                float distance = Vector3.Distance(FlattenY(unit.WorldPosition), center);
                if (distance > radius) continue;
                float falloff = Mathf.Lerp(1f, Mathf.Clamp01(edgeFraction), distance / radius);
                unit.CurrentHP -= unitDamage * falloff;
            }

            if (!arenaFrame.HasLane) return;
            float towerRadius = towerController != null ? towerController.TowerRadius : 0f;
            Vector3 towerPosition = targetSide == UnitSide.Enemy ? OpponentTowerWorldPosition : OwnTowerPosition;
            if (Vector3.Distance(FlattenY(towerPosition), center) > radius + towerRadius) return;

            if (targetSide == UnitSide.Enemy) OpponentTowerHP = Mathf.Max(0f, OpponentTowerHP - towerDamage);
            else OwnTowerHP = Mathf.Max(0f, OwnTowerHP - towerDamage);
        }

        // The burning floor: units of the other side standing in the patch take burn damage for as long as they are in it.
        private void ApplyBurn(UnitSide caster, Vector3 center, float seconds)
        {
            float radius = Mathf.Max(0.0001f, ScaledFireballRadius);
            UnitSide targetSide = caster == UnitSide.Own ? UnitSide.Enemy : UnitSide.Own;

            foreach (var unit in units)
            {
                if (unit.Side != targetSide || unit.CurrentHP <= 0f) continue;
                if (Vector3.Distance(FlattenY(unit.WorldPosition), center) > radius) continue;
                unit.CurrentHP -= spellSpec.FireballBurnDps * seconds;
            }
        }

        // Client: ask the host to throw lightning at this world position (sent as lane fractions, never world coordinates).
        public void RequestRemoteLightning(Vector3 aimWorld)
        {
            var (along, lat) = ToCanonical(aimWorld);
            networkSync?.PublishGestureRequest("spell", null, "lightning", true, along, lat);
        }

        public void RequestRemoteFireball(Vector3 aimWorld)
        {
            var (along, lat) = ToCanonical(aimWorld);
            networkSync?.PublishGestureRequest("spell", null, "fireball", true, along, lat);
        }

        // Host: the client's lightning. Mana is only taken when the aim is usable.
        private bool CastRemoteLightning(GestureRequest request)
        {
            if (!request.hasAim) return false;
            if (enemyManaCurrent < spellSpec.LightningManaCost) return false;

            Vector3 center = FromCanonical(request.along, request.lat);
            if (!IsInsideSpellArea(center)) return false;

            enemyManaCurrent -= spellSpec.LightningManaCost;
            QueueLightning(UnitSide.Enemy, center);
            return true;
        }

        // Host: the client's fireball, checked the same way.
        private bool CastRemoteFireball(GestureRequest request)
        {
            if (!request.hasAim) return false;
            if (enemyManaCurrent < spellSpec.FireballManaCost) return false;

            Vector3 target = FromCanonical(request.along, request.lat);
            if (!IsInsideSpellArea(target)) return false;

            enemyManaCurrent -= spellSpec.FireballManaCost;
            QueueFireball(UnitSide.Enemy, target);
            return true;
        }

        private void AppendStrikeSnapshots(StateSnapshot snapshot)
        {
            foreach (var strike in strikes)
            {
                var (along, lat) = ToCanonical(strike.Center);
                var entry = new StrikeSnapshot
                {
                    id = strike.Id,
                    kind = strike.Fireball ? "fireball" : "lightning",
                    owner = strike.Caster == UnitSide.Own ? "player1" : "player2",
                    along = along,
                    lat = lat,
                    delay = Mathf.Max(0f, strike.Remaining)
                };
                if (strike.Fireball)
                {
                    var (fromAlong, fromLat) = ToCanonical(strike.Start);
                    entry.fromAlong = fromAlong;
                    entry.fromLat = fromLat;
                    entry.burn = strike.Applied ? Mathf.Max(0f, strike.BurnLeft) : spellSpec.FireballBurnSeconds;
                }
                snapshot.strikes.Add(entry);
            }
        }

        private void ApplyStrikeSnapshots(StateSnapshot snapshot)
        {
            if (snapshot.strikes == null) return;
            foreach (var strike in snapshot.strikes)
            {
                if (!seenStrikeIds.Add(strike.id)) continue;
                Vector3 center = FlattenY(FromCanonical(strike.along, strike.lat));
                bool byLocal = strike.owner == "player2";
                if (strike.kind == "fireball")
                {
                    Vector3 start = FlattenY(FromCanonical(strike.fromAlong, strike.fromLat));
                    FireballStarted?.Invoke(start, center, strike.delay, strike.burn, byLocal);
                }
                else
                {
                    LightningStrikeStarted?.Invoke(center, strike.delay, byLocal);
                }
            }
        }
    }
}
