using BridgeRush.Tutorial;

// Checks TutorialFlow: the order, what advances a step, what does not, units that must be done twice, skip and reset.
static class TutorialChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; System.Console.WriteLine("FAIL: tutorial: " + what); } }

    // Does a step all the way: Repeats - 1 times Progressed, then Advanced (or Finished on the last step).
    static void Complete(TutorialFlow f, string action, string what)
    {
        int repeats = f.Current.Repeats;
        for (int i = 0; i < repeats - 1; i++)
            Check(f.Report(action) == TutorialOutcome.Progressed, what + ": repeat " + (i + 1) + " is progress, not done");
        var last = f.Report(action);
        Check(last == TutorialOutcome.Advanced || last == TutorialOutcome.Finished, what + ": completes");
    }

    public static int Run()
    {
        var names = new[] { "lock", "unlock", "bubble", "Warrior", "Rogue", "Mage", "Tank", "Hero", "lightning", "fireball" };
        Check(TutorialFlow.Steps.Count == names.Length, "ten steps");
        for (int i = 0; i < names.Length; i++) Check(TutorialFlow.Steps[i].Action == names[i], "step " + i + " is " + names[i]);
        Check(TutorialFlow.Steps[0].IsShake && TutorialFlow.Steps[1].IsShake && !TutorialFlow.Steps[2].IsShake, "lock and unlock are phone shakes, the bubble after them is not");
        Check(TutorialFlow.Steps[2].IsBubble && !TutorialFlow.Steps[0].IsBubble && !TutorialFlow.Steps[1].IsBubble, "only step 3 is the bubble");
        for (int i = 3; i < 10; i++) Check(!TutorialFlow.Steps[i].IsShake && !TutorialFlow.Steps[i].IsBubble, "step " + i + " is not a shake or a bubble");
        Check(!TutorialFlow.Steps[7].IsSpell && TutorialFlow.Steps[8].IsSpell && TutorialFlow.Steps[9].IsSpell, "only the last two are spells");
        Check(TutorialFlow.Steps[0].Repeats == 1 && TutorialFlow.Steps[1].Repeats == 1 && TutorialFlow.Steps[2].Repeats == 1, "lock, bubble and unlock are done once");
        for (int i = 3; i < 8; i++) Check(TutorialFlow.Steps[i].Repeats == 2, "unit step " + i + " is done twice");
        Check(TutorialFlow.Steps[8].Repeats == 1 && TutorialFlow.Steps[9].Repeats == 1, "spells are done once");

        var f = new TutorialFlow();
        Check(f.Index == 0 && f.Current.Action == "lock" && !f.Finished && f.Total == 10 && f.StepProgress == 0, "starts on lock tower");

        // a cast or the wrong shake does not move the lesson on
        Check(f.Report("Warrior") == TutorialOutcome.Wrong && f.Index == 0, "a unit on the lock step stays");
        Check(f.Report("unlock") == TutorialOutcome.Wrong && f.Index == 0, "unlock before lock stays");
        Check(f.Report("") == TutorialOutcome.Wrong && f.Index == 0, "an empty action is wrong, not a crash");
        Check(f.Report(null) == TutorialOutcome.Wrong && f.Index == 0, "a null action is wrong, not a crash");
        Check(f.Refusal("Warrior", false) == "WRONG CARD", "a card is refused on a shake step");

        Check(f.Report("bubble") == TutorialOutcome.Wrong && f.Index == 0, "a bubble before the lock stays");
        Check(f.Report("lock") == TutorialOutcome.Advanced && f.Index == 1, "lock advances");
        Check(f.Report("lock") == TutorialOutcome.Wrong && f.Index == 1, "locking again does not count");
        Check(f.Report("bubble") == TutorialOutcome.Wrong && f.Index == 1, "a bubble before the unlock stays");
        Check(f.Report("UNLOCK") == TutorialOutcome.Advanced && f.Index == 2 && f.Current.Action == "bubble", "unlock advances to the bubble, any case");
        Check(f.Refusal("Warrior", false) == "WRONG CARD", "a card is refused on the bubble step");
        Check(f.Report("unlock") == TutorialOutcome.Wrong && f.Index == 2, "unlocking again does not count");
        Check(f.Report("BUBBLE") == TutorialOutcome.Advanced && f.Index == 3 && f.Current.Action == "Warrior", "the bubble advances, any case");
        Check(f.Report("bubble") == TutorialOutcome.Wrong && f.Index == 3, "a second bubble does not count");

        // a unit has to be summoned twice
        Check(f.Report("Rogue") == TutorialOutcome.Wrong && f.Index == 3 && f.StepProgress == 0, "another unit stays");
        Check(f.Report("warrior") == TutorialOutcome.Progressed && f.Index == 3 && f.StepProgress == 1, "the first Warrior is progress only");
        Check(f.Current.Action == "Warrior", "still on the Warrior after one");
        Check(f.Refusal("Warrior", false) == null && f.Refusal("Rogue", false) == "WRONG CARD", "the same unit is allowed again, another is refused");
        Check(f.Report("Rogue") == TutorialOutcome.Wrong && f.StepProgress == 1, "a wrong cast keeps the count");
        Check(f.Report("Warrior") == TutorialOutcome.Advanced && f.Index == 4 && f.StepProgress == 0, "the second Warrior completes it and resets the count");
        Check(f.Report("Warrior") == TutorialOutcome.Wrong && f.Index == 4, "a finished unit does not count again");

        for (int i = 4; i < 9; i++) Complete(f, names[i], names[i]);
        Check(f.Current.Action == "fireball" && !f.Finished, "last step is the Fireball");
        Check(f.Report("fireball") == TutorialOutcome.Finished && f.Finished && f.Current == null, "the last step finishes");
        Check(f.Report("Warrior") == TutorialOutcome.Ignored, "nothing counts after the end");
        Check(!f.Skip() && f.Index == 10, "no skip after the end");

        f.Reset();
        Check(f.Index == 0 && f.StepProgress == 0 && !f.Finished, "reset starts again");
        Check(f.Skip() && f.Index == 1 && f.Current.Action == "unlock", "skip moves one step on");
        f.Skip(); f.Skip();
        Check(f.Report("Warrior") == TutorialOutcome.Progressed, "one Warrior");
        Check(f.Skip() && f.Index == 4 && f.StepProgress == 0, "skip drops the half-done count");
        Check(f.Report("Rogue") == TutorialOutcome.Progressed, "the count starts fresh on the next unit");
        f.Reset();
        Check(f.Index == 0 && f.StepProgress == 0, "reset clears the count");
        for (int i = 0; i < 10; i++) f.Skip();
        Check(f.Finished, "skipping through finishes");

        // what may be performed: only this step's action, and nothing just after a step or once it is over
        var g = new TutorialFlow();
        Check(g.Refusal("lock", false) == null && g.Refusal("LOCK", false) == null, "the current action is allowed, any case");
        Check(g.Refusal("Rogue", false) == "WRONG CARD" && g.Refusal("lightning", false) == "WRONG CARD" && g.Refusal("", false) == "WRONG CARD", "another action is refused");
        Check(g.Refusal("lock", true) == "WAIT", "nothing during the pause after a step, not even the right one");
        Check(g.Refusal("Rogue", true) == "WAIT", "the pause comes before the wrong-card check");
        g.Report("lock"); g.Report("unlock"); g.Report("bubble");
        Check(g.Refusal("Warrior", false) == null && g.Refusal("Rogue", false) == "WRONG CARD", "the first unit is allowed, the next is refused");
        while (!g.Finished) g.Skip();
        Check(g.Refusal("fireball", false) == "WAIT", "everything is refused once the lesson is over");

        return failures;
    }
}
