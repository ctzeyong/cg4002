using UnityEngine;

namespace BridgeRush.Match
{
    public enum UnitType
    {
        Warrior,
        Rogue,
        Mage,
        Tank,
        Hero
    }

    public enum UnitCombatState
    {
        Walking,
        Attacking
    }

    public enum UnitSide
    {
        Own,
        Enemy
    }

    public class UnitState
    {
        public int Id;
        public UnitType Type;
        public UnitSide Side;

        public Vector3 WorldPosition;
        public Vector3 PreviousWorldPosition;
        public Vector3 LateralVelocity;
        public Vector3 FacingDirection = Vector3.forward;

        public float MaxHP;
        public float CurrentHP;
        public float AttackDps;
        public UnitCombatState CombatState;
        public int? AttackTargetUnitId;

        public bool PastBridge;

        public UnitState(int id, UnitType type, UnitSide side, float maxHP, float attackDps, Vector3 spawnWorldPosition)
        {
            Id = id;
            Type = type;
            Side = side;
            WorldPosition = spawnWorldPosition;
            PreviousWorldPosition = spawnWorldPosition;
            MaxHP = maxHP;
            CurrentHP = maxHP;
            AttackDps = attackDps;
            CombatState = UnitCombatState.Walking;
            AttackTargetUnitId = null;
        }
    }
}
