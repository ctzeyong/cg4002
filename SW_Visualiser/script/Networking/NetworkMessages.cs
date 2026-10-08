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
