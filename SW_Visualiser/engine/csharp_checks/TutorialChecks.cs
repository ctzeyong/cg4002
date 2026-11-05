using BridgeRush.Tutorial;

// Checks TutorialFlow: the order, what advances a step, what does not, skip and reset.
static class TutorialChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; System.Console.WriteLine("FAIL: tutorial: " + what); } }

    public static int Run()
    {
        var names = new[] { "Warrior", "Rogue", "Mage", "Tank", "Hero", "lightning", "fireball" };
        Check(TutorialFlow.Steps.Count == names.Length, "seven steps");
        for (int i = 0; i < names.Length; i++) Check(TutorialFlow.Steps[i].Action == names[i], "step " + i + " is " + names[i]);
        Check(!TutorialFlow.Steps[4].IsSpell && TutorialFlow.Steps[5].IsSpell && TutorialFlow.Steps[6].IsSpell, "only the last two are spells");

        var f = new TutorialFlow();
        Check(f.Index == 0 && f.Current.Action == "Warrior" && !f.Finished && f.Total == 7, "starts on the Warrior");

        // another valid action runs in the game but does not move the lesson on
        Check(f.Report("Rogue") == TutorialOutcome.Wrong && f.Index == 0, "a wrong action stays on the step");
        Check(f.Report("lightning") == TutorialOutcome.Wrong && f.Index == 0, "a spell on a unit step stays");
        Check(f.Report("") == TutorialOutcome.Wrong && f.Index == 0, "an empty action is wrong, not a crash");
        Check(f.Report(null) == TutorialOutcome.Wrong && f.Index == 0, "a null action is wrong, not a crash");

        Check(f.Report("warrior") == TutorialOutcome.Advanced && f.Index == 1, "case does not matter");
        Check(f.Report("Warrior") == TutorialOutcome.Wrong && f.Index == 1, "the finished step does not count twice");

        for (int i = 1; i < 6; i++) Check(f.Report(names[i]) == TutorialOutcome.Advanced, "advance " + names[i]);
        Check(f.Current.Action == "fireball" && !f.Finished, "last step is the Fireball");
        Check(f.Report("fireball") == TutorialOutcome.Finished && f.Finished && f.Current == null, "the last step finishes");
        Check(f.Report("Warrior") == TutorialOutcome.Ignored, "nothing counts after the end");
        Check(!f.Skip() && f.Index == 7, "no skip after the end");

        f.Reset();
        Check(f.Index == 0 && !f.Finished, "reset starts again");
        Check(f.Skip() && f.Index == 1 && f.Current.Action == "Rogue", "skip moves one step on");
        for (int i = 0; i < 6; i++) f.Skip();
        Check(f.Finished, "skipping through finishes");
        // what may be performed: only this step's action, and nothing just after a step or once it is over
        var g = new TutorialFlow();
        Check(g.Refusal("Warrior", false) == null && g.Refusal("warrior", false) == null, "the current action is allowed, any case");
        Check(g.Refusal("Rogue", false) == "WRONG CARD" && g.Refusal("lightning", false) == "WRONG CARD" && g.Refusal("", false) == "WRONG CARD", "another action is refused");
        Check(g.Refusal("Warrior", true) == "WAIT", "nothing during the pause after a step, not even the right one");
        Check(g.Refusal("Rogue", true) == "WAIT", "the pause comes before the wrong-card check");
        g.Report("Warrior");
        Check(g.Refusal("Warrior", false) == "WRONG CARD" && g.Refusal("Rogue", false) == null, "the same unit again is refused, the next one is allowed");
        while (!g.Finished) g.Skip();
        Check(g.Refusal("fireball", false) == "WAIT", "everything is refused once the lesson is over");

        return failures;
    }
}
