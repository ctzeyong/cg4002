using System;
using System.Collections.Generic;

namespace BridgeRush.Networking
{
    [Serializable]
    public class UnitSnapshot
    {
        public int id;
        public string type;
        public string owner;
        public float along;
        public float lat;
        public float hp;
        public float maxHp;
        public string state;
        // Id of the unit being attacked (-1 for the tower or nothing) and the unit's attack range (0 = melee), so the
        // client can draw ranged bolts without running combat.
        public int target = -1;
        public float range;
        // Which way the unit faces, as a direction along and across the lane (like along and lat), so the client turns
        // units exactly as the host does, including toward a target while attacking.
        public float faceAlong;
        public float faceLat;
    }

    [Serializable]
    public class StrikeSnapshot
    {
        public int id;
        public string owner;
        public float along;
        public float lat;
        public float delay;
        // "lightning" (or empty) or "fireball". A fireball also carries where it starts and the burn time left on the floor.
        public string kind;
        public float fromAlong;
        public float fromLat;
        public float burn;
    }

    [Serializable]
    public class StateSnapshot
    {
        public float hostTowerHP;
        public float hostTowerMaxHP;
        public float player2TowerHP;
        public float player2TowerMaxHP;
        public float hostMana;
        public float player2Mana;
        public float matchTimeRemaining;
        public List<UnitSnapshot> units = new();
        public List<StrikeSnapshot> strikes = new();
    }

    [Serializable]
    public class GestureRequest
    {
        public string action;
        public string unitType;
        public string spell;
        // Canonical along/lat of the client's crosshair at the moment of the spawn request - see MatchState.ToCanonical/FromCanonical.
        public bool hasAim;
        public float along;
        public float lat;
    }
}
