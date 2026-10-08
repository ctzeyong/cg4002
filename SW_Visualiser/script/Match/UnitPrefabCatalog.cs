using System;
using UnityEngine;

namespace BridgeRush.Match
{
    [CreateAssetMenu(fileName = "UnitPrefabCatalog", menuName = "BridgeRush/Unit Prefab Catalog")]
    public class UnitPrefabCatalog : ScriptableObject
    {
        [Serializable]
        public struct Entry
        {
            public UnitType Type;
            public GameObject Prefab;
        }

        [SerializeField] private Entry[] entries;

        public bool TryGetPrefab(UnitType type, out GameObject prefab)
        {
            foreach (var entry in entries)
            {
                if (entry.Type == type)
                {
                    prefab = entry.Prefab;
                    return prefab != null;
                }
            }
            prefab = null;
            return false;
        }
    }
}
