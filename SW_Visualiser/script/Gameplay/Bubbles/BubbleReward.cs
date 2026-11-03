using System;
using UnityEngine;

namespace BridgeRush.Gameplay
{
    public enum BubbleRewardType { Mana, Buff }

    // What a captured bubble is worth. Mana is the only type in use; Buff is reserved so adding one later is a new case
    // in BubbleSync's reward handling, not a change to the capture flow.
    [Serializable]
    public struct BubbleReward
    {
        public BubbleRewardType Type;
        [Tooltip("Mana: how much. Buff: strength.")]
        public float Amount;
        [Tooltip("Buff only: which buff.")]
        public string BuffId;

        public static BubbleReward Mana(float amount) => new() { Type = BubbleRewardType.Mana, Amount = amount };

        public override string ToString() => Type == BubbleRewardType.Mana ? $"{Amount:0.##} mana" : $"buff '{BuffId}'";
    }
}
