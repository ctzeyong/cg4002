namespace BridgeRush.Input
{
    // Decides whether a stream of accelerometer readings is a deliberate "shake the phone" and not walking, running or
    // aiming. Plain C# (no Unity types) so it is checked outside Unity (engine/csharp_checks/ShakeChecks.cs).
    //
    // A shake counts only when ALL of these hold:
    //  0. Sideways: only the phone's left-right axis (its short edge, X) is listened to, so it is a shake from left to right and
    //     back, not up and down or forward and back. Walking and arm swings move the phone up and down and forward and back,
    //     and barely sideways, which is what lets the bar below be so low.
    //  1. Firm: each jolt peaks above JoltThresholdG (sideways, once gravity is removed). It is deliberately gentle, a quick
    //     wiggle of the wrist: a violent shake blurs the camera and makes AR tracking lose the arena.
    //  2. Back and forth: neighbouring jolts point roughly opposite ways. A step is a one-sided thump, so they never do.
    //  3. Fast rhythm: jolts come MinGapSeconds to MaxGapSeconds apart (about 2 to 7 per second). Steps are slower.
    //  4. Enough of them: RequiredJolts in a row (4 is two quick shakes) inside WindowSeconds.
    // After a trigger the detector ignores input for CooldownSeconds and then until the hand has gone quiet for
    // RearmQuietSeconds, so one shake, however long, cannot lock and then immediately unlock.
    public sealed class ShakeDetector
    {
        public float JoltThresholdG = 0.7f;
        public float ReleaseThresholdG = 0.3f;
        // Only the phone's left-right axis counts, and the other axes must not move more than SideDominance times as much.
        public bool SideToSideOnly = true;
        public float SideDominance = 1.0f;
        public float MinGapSeconds = 0.07f;
        public float MaxGapSeconds = 0.30f;
        public int RequiredJolts = 4;
        public float WindowSeconds = 1.2f;
        public float CooldownSeconds = 1.0f;
        public float RearmQuietSeconds = 0.35f;
        // Two neighbouring jolts must point this far apart (cosine, -1 is exactly opposite).
        public float MaxNeighbourCosine = -0.5f;

        private const float GravityTimeConstant = 0.5f;
        private const int MaxChain = 16;

        private bool haveGravity;
        private float gx, gy, gz;
        private float lastTime;

        private bool inJolt;
        private float peakMag, peakTime, px, py, pz, peakOther;

        private readonly float[] chainTime = new float[MaxChain];
        private readonly float[] chainX = new float[MaxChain];
        private readonly float[] chainY = new float[MaxChain];
        private readonly float[] chainZ = new float[MaxChain];
        private int chainCount;
        private float cooldownUntil = -1f;
        private bool armed = true;
        private float quietSince = -1f;

        // When the last recognised shake began (the time of its first jolt, same clock as Feed), so the caller can use where the
        // phone was just BEFORE it was shaken rather than where it is mid-shake.
        public float LastShakeStart { get; private set; }

        // Number of jolts in the current chain; for the debug overlay.
        public int ChainLength => chainCount;

        // Drop any partial shake (finger on the screen, paused, match not running).
        public void Reset()
        {
            inJolt = false;
            chainCount = 0;
            haveGravity = false;
            armed = true;
            quietSince = -1f;
        }

        // Feed one reading in g (gravity included) with a time in seconds. True on the sample that completes a shake.
        public bool Feed(float time, float ax, float ay, float az)
        {
            if (!haveGravity)
            {
                gx = ax; gy = ay; gz = az;
                haveGravity = true;
                lastTime = time;
                return false;
            }

            float dt = time - lastTime;
            lastTime = time;
            if (dt <= 0f) return false;

            // Slow low-pass keeps the gravity direction; what is left is the motion of the hand.
            float k = dt / (GravityTimeConstant + dt);
            gx += (ax - gx) * k; gy += (ay - gy) * k; gz += (az - gz) * k;
            float hx = ax - gx, hy = ay - gy, hz = az - gz;
            float other = 0f;
            float mag;
            if (SideToSideOnly)
            {
                mag = System.Math.Abs(hx);
                other = (float)System.Math.Sqrt(hy * hy + hz * hz);
                hy = 0f; hz = 0f;   // the jolt's direction is just left or right
            }
            else mag = (float)System.Math.Sqrt(hx * hx + hy * hy + hz * hz);

            if (!armed || time < cooldownUntil)
            {
                inJolt = false;
                chainCount = 0;
                if (time >= cooldownUntil) // cooldown over: listen again only once the hand has been still for a moment
                {
                    if (mag < ReleaseThresholdG)
                    {
                        if (quietSince < 0f) quietSince = time;
                        if (time - quietSince >= RearmQuietSeconds) armed = true;
                    }
                    else quietSince = -1f;
                }
                return false;
            }

            if (!inJolt)
            {
                if (mag >= JoltThresholdG)
                {
                    inJolt = true;
                    peakMag = mag; peakTime = time; px = hx; py = hy; pz = hz; peakOther = other;
                }
                // A chain that has gone quiet for longer than a jolt gap is over.
                else if (chainCount > 0 && time - chainTime[chainCount - 1] > MaxGapSeconds) chainCount = 0;
                return false;
            }

            // A sideways jolt ends when the hand turns round (the sign flips), not only when it goes quiet: in a brisk shake the
            // signal swings straight from one side to the other and never rests below the release level in between.
            bool turned = SideToSideOnly && hx * px < 0f;
            if (!turned)
            {
                if (mag > peakMag)
                {
                    peakMag = mag; peakTime = time; px = hx; py = hy; pz = hz;
                }
                if (other > peakOther) peakOther = other;
                if (mag > ReleaseThresholdG) return false;
            }

            // The jolt just ended: judge it against the one before.
            inJolt = false;
            // Mostly up-and-down or forward-and-back is not a side shake.
            if (SideToSideOnly && peakOther > SideDominance * peakMag)
            {
                chainCount = 0;
                return false;
            }
            return AddJolt(peakTime, px, py, pz);
        }

        private bool AddJolt(float t, float x, float y, float z)
        {
            if (chainCount > 0)
            {
                int last = chainCount - 1;
                float gap = t - chainTime[last];
                float dot = x * chainX[last] + y * chainY[last] + z * chainZ[last];
                float norm = (float)System.Math.Sqrt((x * x + y * y + z * z) * (chainX[last] * chainX[last] + chainY[last] * chainY[last] + chainZ[last] * chainZ[last]));
                float cos = norm > 1e-6f ? dot / norm : 0f;

                bool fits = gap >= MinGapSeconds && gap <= MaxGapSeconds && cos <= MaxNeighbourCosine;
                if (!fits) chainCount = 0;
            }

            if (chainCount >= MaxChain) chainCount = 0;
            chainTime[chainCount] = t; chainX[chainCount] = x; chainY[chainCount] = y; chainZ[chainCount] = z;
            chainCount++;

            if (chainCount < RequiredJolts) return false;
            if (t - chainTime[chainCount - RequiredJolts] > WindowSeconds) return false;

            LastShakeStart = chainTime[chainCount - RequiredJolts];
            chainCount = 0;
            cooldownUntil = t + CooldownSeconds;
            armed = false;
            quietSince = -1f;
            return true;
        }
    }
}
