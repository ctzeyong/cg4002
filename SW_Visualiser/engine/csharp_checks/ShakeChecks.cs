using System;
using BridgeRush.Input;

// Checks ShakeDetector: a hard back-and-forth shake triggers it; walking, running, slow arm sweeps, a single jolt and
// a shake while on cooldown do not. Readings are in g at 60 Hz with gravity on the Y axis, plus some deterministic noise.
static class ShakeChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; Console.WriteLine("FAIL: shake: " + what); } }

    delegate float Motion(float t);

    // Runs a motion on one axis from t0 to t1 and counts triggers.
    static int Run(ShakeDetector d, Motion x, Motion y, float t0, float t1, Motion z = null)
    {
        int hits = 0;
        var rng = new Random(7);
        for (float t = t0; t < t1; t += 1f / 60f)
        {
            float n() => (float)(rng.NextDouble() - 0.5) * 0.06f;
            if (d.Feed(t, x(t) + n(), -1f + y(t) + n(), (z == null ? 0f : z(t)) + n())) hits++;
        }
        return hits;
    }

    static Motion Zero = t => 0f;
    // A shake along X: `hz` back-and-forth cycles per second, `amp` g, between a and b seconds.
    static Motion Shake(float hz, float amp, float a, float b) => t => t >= a && t < b ? amp * (float)Math.Sin(2 * Math.PI * hz * (t - a)) : 0f;
    // One-sided thumps (a footstep): a short positive pulse every 1/hz seconds on Y.
    static Motion Steps(float hz, float amp) => t => { float ph = (t * hz) % 1f; return ph < 0.12f ? amp * (float)Math.Sin(ph / 0.12f * Math.PI) : -0.1f; };

    public static int Run()
    {
        // a deliberate shake: 4 Hz, 3.5 g, for 1.2 s
        Check(Run(new ShakeDetector(), Shake(4f, 3.5f, 1f, 2.2f), Zero, 0f, 4f) == 1, "a hard 4 Hz shake triggers once");
        Check(Run(new ShakeDetector(), Shake(3f, 3.5f, 1f, 2.4f), Zero, 0f, 4f) == 1, "a slower 3 Hz shake triggers");
        Check(Run(new ShakeDetector(), Shake(5f, 3f, 1f, 2.2f), Zero, 0f, 4f) == 1, "a quick 5 Hz shake triggers");

        // not shakes
        Check(Run(new ShakeDetector(), Zero, Steps(2f, 1.5f), 0f, 10f) == 0, "walking (2 steps/s, 1.5 g) does not trigger");
        Check(Run(new ShakeDetector(), Zero, Steps(3f, 3.5f), 0f, 10f) == 0, "running (3 steps/s, 3.5 g) does not trigger");
        Check(Run(new ShakeDetector(), Shake(1f, 1.5f, 0f, 10f), Zero, 0f, 10f) == 0, "a slow 1 Hz arm sweep does not trigger");
        Check(Run(new ShakeDetector(), Shake(4f, 0.45f, 0f, 10f), Zero, 0f, 10f) == 0, "a tiny 4 Hz sideways tremble (0.45 g) does not trigger");
        Check(Run(new ShakeDetector(), Shake(4f, 1.0f, 1f, 2.2f), Zero, 0f, 4f) == 1, "a light left-right shake (1.0 g) triggers: it need not be intense");
        Check(Run(new ShakeDetector(), Shake(4f, 1.0f, 1f, 2.2f), t => 0.3f * Shake(4f, 1f, 1f, 2.2f)(t), 0f, 4f) == 1, "a side shake with a little up-and-down still triggers");
        Check(Run(new ShakeDetector(), Shake(4f, 1.9f, 1f, 2.2f), Zero, 0f, 4f) == 1, "a quick, gentle 4 Hz shake (1.9 g) triggers: no violent shake is needed");
        Check(Run(new ShakeDetector(), Zero, Zero, 0f, 10f, Shake(3.5f, 1.3f, 0f, 10f)) == 0, "a forward-and-back arm swing (1.3 g) does not trigger");
        Check(Run(new ShakeDetector(), Zero, Shake(4f, 3f, 1f, 2.2f), 0f, 4f) == 0, "a hard UP-AND-DOWN shake does not count: only left-right does");
        Check(Run(new ShakeDetector(), Zero, Zero, 0f, 4f, Shake(4f, 3f, 1f, 2.2f)) == 0, "a hard FORWARD-AND-BACK shake does not count");
        Check(Run(new ShakeDetector(), Shake(1f, 0.4f, 0f, 10f), Steps(2f, 1.5f), 0f, 10f) == 0, "walking with a little sideways sway does not trigger");

        // when the shake began, so the tower can lock where the phone was just before it
        var timing = new ShakeDetector();
        int fired = Run(timing, Shake(4f, 3f, 1f, 2.2f), Zero, 0f, 4f);
        Check(fired == 1 && timing.LastShakeStart >= 1.0f && timing.LastShakeStart <= 1.5f, "the shake start is reported near when the shaking began (" + timing.LastShakeStart + ")");
        Check(Run(new ShakeDetector(), Shake(4f, 3.5f, 1f, 1.3f), Zero, 0f, 4f) == 0, "a short flick (about 1 cycle) does not trigger");
        Check(Run(new ShakeDetector(), Shake(1.5f, 3.5f, 0f, 10f), Zero, 0f, 10f) == 0, "a slow hard 1.5 Hz swing does not trigger");

        // a jolt, a pause, then another is not a chain
        Motion twoFlicks = t => (t >= 1f && t < 1.25f || t >= 2.5f && t < 2.75f) ? 3.5f * (float)Math.Sin(2 * Math.PI * 4f * t) : 0f;
        Check(Run(new ShakeDetector(), twoFlicks, Zero, 0f, 5f) == 0, "two separate flicks do not trigger");

        // cooldown: shaking on straight after a trigger does not fire a second time within the cooldown, but does later
        var d = new ShakeDetector();
        int first = Run(d, Shake(4f, 3.5f, 1f, 2.3f), Zero, 0f, 2.3f);
        Check(first == 1, "cooldown: first shake triggers");
        int quick = Run(d, Shake(4f, 3.5f, 2.3f, 3.4f), Zero, 2.3f, 3.4f);
        Check(quick == 0, "cooldown: shaking on without a pause does not trigger again");
        // a long continuous shake (4 s) must lock once and not lock-then-unlock
        Check(Run(new ShakeDetector(), Shake(4f, 3f, 1f, 5f), Zero, 0f, 7f) == 1, "a 4 s continuous shake triggers only once");
        int later = Run(d, Shake(4f, 3.5f, 6f, 7.3f), Zero, 3.4f, 9f);
        Check(later == 1, "cooldown: a shake a few seconds later triggers again (got " + later + ", first " + first + ", quick " + quick + ")");

        // Reset (paused, or no match) drops a half-done shake
        var r = new ShakeDetector();
        int hits = 0;
        for (float t = 0; t < 4f; t += 1f / 60f)
        {
            if (t >= 1.2f) r.Reset(); // held reset for the rest of the shake
            if (r.Feed(t, Shake(4f, 3.5f, 1f, 2.2f)(t), -1f, 0f)) hits++;
        }
        Check(hits == 0, "a detector that is reset every frame (paused) never fires");

        // a shake that is more up-and-down than sideways is rejected; one that is mostly sideways with some tilt is accepted
        var diag = new ShakeDetector();
        int dh = 0;
        for (float t = 0; t < 4f; t += 1f / 60f)
        {
            float v = Shake(4f, 3.5f, 1f, 2.2f)(t);
            if (diag.Feed(t, v * 0.6f, -1f + v * 0.8f, 0f)) dh++;
        }
        Check(dh == 0, "a diagonal shake that is mostly up-and-down does not trigger");
        var tilt = new ShakeDetector();
        int th = 0;
        for (float t = 0; t < 4f; t += 1f / 60f)
        {
            float v = Shake(4f, 3.5f, 1f, 2.2f)(t);
            if (tilt.Feed(t, v * 0.9f, -1f + v * 0.3f, 0f)) th++;
        }
        Check(th == 1, "a shake that is mostly sideways with some tilt triggers");

        Console.WriteLine($"shake checks: {failures} failures");
        return failures;
    }
}
