using System;
using System.Collections.Generic;

namespace BridgeRush.Tutorial
{
    // Progressed: one more of a step that needs several (the first of two summons). Advanced: the step is done.
    public enum TutorialOutcome { Ignored, Progressed, Advanced, Wrong, Finished }

    public class TutorialStep
    {
        public string Action; // what the game reports when it performs it: a unit type name, a spell key, "lock" / "unlock" or "bubble"
        public string Name;   // for the player
        public bool IsSpell;
        public bool IsShake;  // done by shaking the phone (lock / unlock the own tower), not by a card or a glove gesture
        public bool IsBubble; // done by holding the crosshair on a capture bubble until it fills, not by a card or a glove gesture
        public int Repeats = 1; // how many times it must be done before the lesson moves on
    }

    // The order of the lesson and what counts as progress, with no Unity in it so it can be checked outside the Editor.
    // A step is done only when the game really performs its action (summon accepted, spell cast), never on recognition alone.
    public class TutorialFlow
    {
        // Every unit is summoned this many times before the lesson moves on.
        public const int UnitRepeats = 2;

        public static readonly IReadOnlyList<TutorialStep> Steps = new[]
        {
            new TutorialStep { Action = "lock", Name = "Lock Tower", IsShake = true },
            new TutorialStep { Action = "unlock", Name = "Unlock Tower", IsShake = true },
            new TutorialStep { Action = "bubble", Name = "Capture a Bubble", IsBubble = true },
            new TutorialStep { Action = "Warrior", Name = "Warrior", Repeats = UnitRepeats },
            new TutorialStep { Action = "Rogue", Name = "Rogue", Repeats = UnitRepeats },
            new TutorialStep { Action = "Mage", Name = "Mage", Repeats = UnitRepeats },
            new TutorialStep { Action = "Tank", Name = "Tank", Repeats = UnitRepeats },
            new TutorialStep { Action = "Hero", Name = "Hero", Repeats = UnitRepeats },
            new TutorialStep { Action = "lightning", Name = "Lightning Strike", IsSpell = true },
            new TutorialStep { Action = "fireball", Name = "Fireball", IsSpell = true },
        };

        public int Index { get; private set; }
        // How many times the current step has been done so far (0 until the first, then up to Repeats - 1).
        public int StepProgress { get; private set; }
        public int Total => Steps.Count;
        public bool Finished => Index >= Steps.Count;
        public TutorialStep Current => Finished ? null : Steps[Index];

        // The game performed `action`. Only the current step's action counts; another valid one is Wrong. A step with
        // Repeats above 1 stays current (Progressed) until it has been done that many times.
        public TutorialOutcome Report(string action)
        {
            if (Finished) return TutorialOutcome.Ignored;
            if (!string.Equals(action, Current.Action, StringComparison.OrdinalIgnoreCase)) return TutorialOutcome.Wrong;
            StepProgress++;
            if (StepProgress < Current.Repeats) return TutorialOutcome.Progressed;
            StepProgress = 0;
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
            StepProgress = 0;
            Index++;
            return true;
        }

        public void Reset() { Index = 0; StepProgress = 0; }
    }
}
