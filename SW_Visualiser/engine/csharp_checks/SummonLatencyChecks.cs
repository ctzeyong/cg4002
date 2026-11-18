using System;
using BridgeRush.Match;

// Checks SummonLatencyTracker: the time from the board's accepted event to the unit's first snapshot, in either order, counted
// once per unit, with stale entries dropped and the figures kept across a new match until reset.
static class SummonLatencyChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; Console.WriteLine("FAIL: summon latency: " + what); } }
    static bool Near(float a, float b) => Math.Abs(a - b) < 0.5f;

    public static int Run()
    {
        failures = 0;

        var t = new SummonLatencyTracker();
        t.NoteAccepted(7, 10.000f);
        t.NoteSeen(7, 10.060f);
        Check(t.Count == 1 && Near(t.AverageMs, 60f), "event then snapshot 60 ms later reads 60, got " + t.AverageMs);

        t.NoteAccepted(8, 20.000f);
        t.NoteSeen(8, 20.100f);
        Check(t.Count == 2 && Near(t.AverageMs, 80f) && Near(t.MinMs, 60f) && Near(t.MaxMs, 100f), $"two summons: avg 80, min 60, max 100, got {t.AverageMs} {t.MinMs} {t.MaxMs}");

        t.NoteSeen(7, 30f);
        t.NoteSeen(8, 30f);
        Check(t.Count == 2, "a unit counts once, later snapshots are ignored");

        var early = new SummonLatencyTracker();
        early.NoteSeen(3, 5.000f);
        early.NoteAccepted(3, 5.040f);
        Check(early.Count == 1 && Near(early.AverageMs, 0f), "snapshot before its event counts as no wait, got " + early.AverageMs);

        var other = new SummonLatencyTracker();
        other.NoteSeen(50, 1f);   // an enemy unit: no event for it
        other.NoteSeen(51, 2f);
        Check(other.Count == 0, "units with no accepted event are not counted");
        other.NoteAccepted(-1, 3f);
        other.NoteSeen(-1, 3.1f);
        Check(other.Count == 0, "unit id -1 is ignored");

        var stale = new SummonLatencyTracker();
        stale.NoteAccepted(1, 0f);
        stale.NoteAccepted(2, 10f);          // 10 s later: unit 1 never showed and is dropped
        stale.NoteSeen(1, 10.05f);           // it appears much later: nothing waits for it, so it is not timed against the old event
        Check(stale.Count == 0, "an event that waited more than 5 s is dropped, got count " + stale.Count);
        stale.NoteSeen(2, 10.07f);
        Check(stale.Count == 1 && Near(stale.AverageMs, 70f), "the fresh one still counts, got " + stale.AverageMs);

        var match = new SummonLatencyTracker();
        match.NoteAccepted(1, 0f); match.NoteSeen(1, 0.05f);
        match.NoteAccepted(2, 1f);
        match.NewMatch();
        match.NoteSeen(2, 1.05f);
        Check(match.Count == 1, "a new match forgets what was waiting but keeps the figures, got " + match.Count);
        match.Reset();
        Check(match.Count == 0 && match.AverageMs == 0f && match.MaxMs == 0f, "reset clears the figures");

        var bad = new SummonLatencyTracker();
        bad.NoteAccepted(1, 5f);
        bad.NoteSeen(1, 4f);   // a clock that went backwards
        Check(bad.Count == 0, "a negative wait is not recorded");

        Console.WriteLine($"summon latency checks: {failures} failures");
        return failures;
    }
}
