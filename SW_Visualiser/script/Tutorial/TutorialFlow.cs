using System;
using System.Collections.Generic;

namespace BridgeRush.Tutorial
{
    public enum TutorialOutcome { Ignored, Advanced, Wrong, Finished }

    public class TutorialStep
    {
        public string Action; // what the game reports when it performs it: a unit type name, or a spell key
        public string Name;   // for the player
        public bool IsSpell;
    }

    // The order of the lesson and what counts as progress, with no Unity in it so it can be checked outside the Editor.
    // A step is done only when the game really performs its action (summon accepted, spell cast), never on recognition alone.
    public class TutorialFlow
    {
        public static readonly IReadOnlyList<TutorialStep> Steps = new[]
        {
            new TutorialStep { Action = "Warrior", Name = "Warrior" },
            new TutorialStep { Action = "Rogue", Name = "Rogue" },
            new TutorialStep { Action = "Mage", Name = "Mage" },
            new TutorialStep { Action = "Tank", Name = "Tank" },
            new TutorialStep { Action = "Hero", Name = "Hero" },
            new TutorialStep { Action = "lightning", Name = "Lightning Strike", IsSpell = true },
            new TutorialStep { Action = "fireball", Name = "Fireball", IsSpell = true },
        };

        public int Index { get; private set; }
        public int Total => Steps.Count;
        public bool Finished => Index >= Steps.Count;
        public TutorialStep Current => Finished ? null : Steps[Index];

        // The game performed `action`. Only the current step's action moves the lesson on; another valid one is Wrong.
        public TutorialOutcome Report(string action)
        {
            if (Finished) return TutorialOutcome.Ignored;
            if (!string.Equals(action, Current.Action, StringComparison.OrdinalIgnoreCase)) return TutorialOutcome.Wrong;
            Index++;
            return Finished ? TutorialOutcome.Finished : TutorialOutcome.Advanced;
        }

        // Asked before the game performs `action`. Null means go ahead. "WAIT" while a step has just been done (so a burst of
        // gestures cannot run through the lesson) or once the lesson is over; "WRONG CARD" for anything but this step's action.
        public string Refusal(string action, bool cooling)
        {
            if (Finished || cooling) return "WAIT";
            return string.Equals(action, Current.Action, StringComparison.OrdinalIgnoreCase) ? null : "WRONG CARD";
        }

        // For the presenter, when a glove gesture will not come.
        public bool Skip()
        {
            if (Finished) return false;
            Index++;
            return true;
        }

        public void Reset() => Index = 0;
    }
}
