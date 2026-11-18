namespace BridgeRush.UI
{
    // Measures the frame rate: the last second, the average since the last reset, and the 1% low (the rate the slowest 1% of frames
    // fell to, which is what a player feels as stutter). Plain C# so it can be checked outside Unity. Frame times go into a
    // 1 ms histogram, so memory is fixed however long a match runs.
    public class FrameRateMeter
    {
        private const int Buckets = 251;   // 0 to 249 ms, and one bucket for anything slower
        private readonly int[] histogram = new int[Buckets];
        private int frames;
        private double seconds;
        private int windowFrames;
        private double windowSeconds;

        public float Fps { get; private set; }
        public int Frames => frames;
        public double Seconds => seconds;
        public float AverageFps => seconds > 0.0 ? (float)(frames / seconds) : 0f;

        public void Reset()
        {
            System.Array.Clear(histogram, 0, histogram.Length);
            frames = 0;
            seconds = 0.0;
            windowFrames = 0;
            windowSeconds = 0.0;
            Fps = 0f;
        }

        public void Tick(float frameSeconds)
        {
            if (frameSeconds <= 0f || float.IsNaN(frameSeconds) || float.IsInfinity(frameSeconds)) return;
            frames++;
            seconds += frameSeconds;
            int bucket = (int)(frameSeconds * 1000f);
            histogram[bucket >= Buckets ? Buckets - 1 : bucket]++;

            windowFrames++;
            windowSeconds += frameSeconds;
            if (windowSeconds >= 1.0)
            {
                Fps = (float)(windowFrames / windowSeconds);
                windowFrames = 0;
                windowSeconds = 0.0;
            }
        }

        // The frame rate of the slowest 1% of frames (the 99th percentile frame time). 0 until there are 100 frames.
        public float OnePercentLowFps
        {
            get
            {
                if (frames < 100) return 0f;
                int slowest = (int)System.Math.Ceiling(frames * 0.01);
                int seen = 0;
                for (int b = Buckets - 1; b >= 0; b--)
                {
                    seen += histogram[b];
                    if (seen >= slowest) return 1000f / (b + 1f);   // the bucket's slow edge, so the figure is never flattering
                }
                return 0f;
            }
        }
    }
}
