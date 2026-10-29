using System;
using UnityEngine;

namespace BridgeRush.Match
{
    [CreateAssetMenu(fileName = "UnitStatsTable", menuName = "BridgeRush/Unit Stats Table")]
    public class UnitStatsTable : ScriptableObject
    {
        [Serializable]
        public struct Entry
        {
            public UnitType Type;
            public float SecondsToCrossLane;
            public float MaxHP;
            public float AttackDps;
            public float ManaCost;
            // 0 = melee (uses MatchState's engage range). Above 0 the unit stops and attacks from this far away.
            public float AttackRangeMeters;
        }

        [SerializeField]
        private Entry[] entries =
        {
            new() { Type = UnitType.Warrior, SecondsToCrossLane = 10f, MaxHP = 150f, AttackDps = 20f, ManaCost = 2f },
            new() { Type = UnitType.Rogue, SecondsToCrossLane = 6f, MaxHP = 120f, AttackDps = 35f, ManaCost = 3f },
            new() { Type = UnitType.Mage, SecondsToCrossLane = 12f, MaxHP = 90f, AttackDps = 30f, ManaCost = 4f, AttackRangeMeters = 0.35f },
            new() { Type = UnitType.Tank, SecondsToCrossLane = 15f, MaxHP = 700f, AttackDps = 25f, ManaCost = 6f },
            new() { Type = UnitType.Hero, SecondsToCrossLane = 12f, MaxHP = 450f, AttackDps = 70f, ManaCost = 8f },
        };

        // Returns true if the entry for the given unit type was found, false otherwise.
        public bool TryGetEntry(UnitType type, out Entry entry)
        {
            foreach (var candidate in entries)
            {
                if (candidate.Type != type) continue;
                entry = candidate;
                return true;
            }
            entry = default;
            return false;
        }

        // Returns the seconds it takes for a unit of the given type to cross the lane. If the unit type is not found, returns 10f as a default value.
        public float GetSecondsToCrossLane(UnitType type) =>
            TryGetEntry(type, out var entry) ? entry.SecondsToCrossLane : 10f;
    }
}
