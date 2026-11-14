using System;
using BridgeRush.UI;

// Checks FrameRateMeter: a steady 30 fps and 60 fps read right, a stutter drags the 1% low (not the average), bad frame
// times are ignored, and Reset starts again.
static class FrameRateChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; Console.WriteLine("FAIL: frame rate: " + what); } }
    static bool Near(float a, float b, float tol) => Math.Abs(a - b) <= tol;

    public static int Run()
    {
        failures = 0;

        var steady = new FrameRateMeter();
        for (int i = 0; i < 600; i++) steady.Tick(1f / 30f);
        Check(Near(steady.AverageFps, 30f, 0.2f), "steady 30 fps averages 30, got " + steady.AverageFps);
        Check(Near(steady.Fps, 30f, 0.5f), "steady 30 fps reads 30 over the last second, got " + steady.Fps);
        Check(steady.OnePercentLowFps >= 28f && steady.OnePercentLowFps <= 31f, "steady 30 fps has a 1% low near 30, got " + steady.OnePercentLowFps);

        var fast = new FrameRateMeter();
        for (int i = 0; i < 1200; i++) fast.Tick(1f / 60f);
        Check(Near(fast.AverageFps, 60f, 0.5f), "steady 60 fps averages 60, got " + fast.AverageFps);

        // 1000 frames at 60 fps with 20 frames of 100 ms (2%): the average barely moves, the 1% low falls to about 10 fps.
        var stutter = new FrameRateMeter();
        for (int i = 0; i < 1000; i++) stutter.Tick(i % 50 == 0 ? 0.1f : 1f / 60f);
        Check(stutter.AverageFps > 40f, "a stutter hardly moves the average, got " + stutter.AverageFps);
        Check(stutter.OnePercentLowFps <= 11f, "a stutter pulls the 1% low to about 10, got " + stutter.OnePercentLowFps);

        var few = new FrameRateMeter();
        for (int i = 0; i < 50; i++) few.Tick(1f / 60f);
        Check(few.OnePercentLowFps == 0f, "no 1% low before 100 frames");

        var bad = new FrameRateMeter();
        bad.Tick(0f); bad.Tick(-1f); bad.Tick(float.NaN); bad.Tick(float.PositiveInfinity);
        Check(bad.Frames == 0, "zero, negative, NaN and infinite frame times are ignored");
        bad.Tick(5f);   // a 5 s hitch lands in the last bucket without throwing
        Check(bad.Frames == 1, "a very long frame is counted");

        steady.Reset();
        Check(steady.Frames == 0 && steady.AverageFps == 0f && steady.Fps == 0f, "reset clears everything");

        Console.WriteLine($"frame rate checks: {failures} failures");
        return failures;
    }
}
