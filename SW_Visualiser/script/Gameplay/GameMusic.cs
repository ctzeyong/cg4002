using System;
using System.Threading.Tasks;
using UnityEngine;

namespace BridgeRush.Gameplay
{
    // Which piece plays: the calm menu loop (before the match and on the result screen), the match loop, or the faster
    // last-minute loop (double mana).
    public enum MusicTrack { Menu, Match, LastMinute }

    // Background music without audio files. Three short loops (eight bars each) are rendered once in the background the first
    // time they are needed: a calm, warm menu loop (72 BPM, soft pad and a slow harp-like arpeggio, no drums), the match loop
    // (92 BPM, pad chords, bass, a plucked arpeggio, a simple melody and soft drums) and a faster, more driving one (128 BPM,
    // sixteenth-note arpeggio, a pulsing bass, a kick on every beat) for the last minute. Clips assigned on GameStateRenderer
    // replace them. The music switches with a short dip and fades out when it is turned off.
    public static class GameMusic
    {
        private const int SampleRate = 22050;
        private const float MenuBpm = 72f;
        private const float NormalBpm = 92f;
        private const float FastBpm = 128f;
        private const string EnabledKey = "music_enabled";

        private static AudioSource source;
        private static readonly AudioClip[] clips = new AudioClip[3];
        private static readonly Task<float[]>[] rendering = new Task<float[]>[3];
        private static float currentVolume;
        private static bool enabledLoaded;
        private static bool enabled = true;

        public static bool Enabled
        {
            get
            {
                if (!enabledLoaded) { enabled = PlayerPrefs.GetInt(EnabledKey, 1) == 1; enabledLoaded = true; }
                return enabled;
            }
            set
            {
                enabled = value;
                enabledLoaded = true;
                PlayerPrefs.SetInt(EnabledKey, value ? 1 : 0);
            }
        }

        // Call every frame. track says what should be playing now; play is false to fade the music out (the Debug switch,
        // or the music turned off on GameStateRenderer). custom* are optional replacements for the built-in loops (with only
        // customMatch set, it plays in the last minute too; with no customMenu, the menu uses the built-in menu loop).
        public static void Tick(bool play, MusicTrack track, AudioClip customMenu, AudioClip customMatch, AudioClip customLastMinute, float targetVolume)
        {
            bool want = play && Enabled;
            AudioClip playing = Pick(track, want, customMenu, customMatch, customLastMinute);

            if (source == null)
            {
                if (!want) return;
                var go = new GameObject("GameMusic");
                UnityEngine.Object.DontDestroyOnLoad(go);
                source = go.AddComponent<AudioSource>();
                source.playOnAwake = false;
                source.loop = true;
                source.spatialBlend = 0f;
                source.volume = 0f;
            }

            bool switching = source.isPlaying && source.clip != null && playing != null && source.clip != playing;
            float goal = want && playing != null && !switching ? Mathf.Clamp01(targetVolume) : 0f;
            float seconds = switching ? 0.4f : (want ? 1.2f : 1f);
            currentVolume = Mathf.MoveTowards(currentVolume, goal, Time.unscaledDeltaTime / seconds);

            if (want && playing != null)
            {
                if (source.clip != playing && (!source.isPlaying || currentVolume <= 0f))
                {
                    source.Stop();
                    source.clip = playing;
                }
                if (source.clip == playing && !source.isPlaying) source.Play();
            }
            else if (currentVolume <= 0f && source.isPlaying)
            {
                source.Stop();
            }
            source.volume = currentVolume;
        }

        private static AudioClip Pick(MusicTrack track, bool want, AudioClip customMenu, AudioClip customMatch, AudioClip customLastMinute)
        {
            switch (track)
            {
                case MusicTrack.Menu:
                    return customMenu != null ? customMenu : BuiltInClip(MusicTrack.Menu, want);
                case MusicTrack.LastMinute:
                    if (customLastMinute != null) return customLastMinute;
                    return customMatch != null ? customMatch : BuiltInClip(MusicTrack.LastMinute, want);
                default:
                    return customMatch != null ? customMatch : BuiltInClip(MusicTrack.Match, want);
            }
        }

        // The built-in loops are rendered on background threads; this returns null until the one asked for is ready. Once any
        // music is wanted, all three are started so every switch is instant. The menu loop goes first.
        private static AudioClip BuiltInClip(MusicTrack track, bool needed)
        {
            int index = (int)track;
            if (clips[index] != null) return clips[index];
            if (!needed) return null;

            for (int i = 0; i < 3; i++)
            {
                if (rendering[i] == null && clips[i] == null)
                {
                    var which = (MusicTrack)i;
                    rendering[i] = Task.Run(() => Render(which));
                }
            }

            var task = rendering[index];
            if (task == null || !task.IsCompleted) return null;

            if (task.IsFaulted)
            {
                Debug.LogWarning("GameMusic: could not render the music loop.");
                rendering[index] = null;
                return null;
            }

            float[] data = task.Result;
            clips[index] = AudioClip.Create("game_music_" + track, data.Length, 1, SampleRate, false);
            clips[index].SetData(data, 0);
            rendering[index] = null;
            return clips[index];
        }

        private static double Hz(int midi) => 440.0 * Math.Pow(2.0, (midi - 69) / 12.0);

        // The match loop is eight bars in A minor: Am, F, C, G, Am, F, G, Am. Everything wraps around the end so the loop has no
        // seam. The last-minute version keeps the same chords and melody but plays them harder (a pulsing bass, sixteenth-note
        // arpeggio, a kick on every beat). The menu loop is in C major (C, Am, F, G, twice), slow and soft, with no drums.
        private static float[] Render(MusicTrack track)
        {
            bool menu = track == MusicTrack.Menu;
            bool fast = track == MusicTrack.LastMinute;
            int beat = (int)(SampleRate * 60f / (menu ? MenuBpm : (fast ? FastBpm : NormalBpm)));
            int bars = 8;
            int total = bars * 4 * beat;
            var mix = new double[total];
            var random = new System.Random(20261027);

            int[][] chords = menu
                ? new[]
                {
                    new[] { 60, 64, 67 }, new[] { 57, 60, 64 }, new[] { 53, 57, 60 }, new[] { 55, 59, 62 },
                    new[] { 60, 64, 67 }, new[] { 57, 60, 64 }, new[] { 53, 57, 60 }, new[] { 55, 59, 62 }
                }
                : new[]
                {
                    new[] { 57, 60, 64 }, new[] { 53, 57, 60 }, new[] { 48, 52, 55 }, new[] { 55, 59, 62 },
                    new[] { 57, 60, 64 }, new[] { 53, 57, 60 }, new[] { 55, 59, 62 }, new[] { 57, 60, 64 }
                };
            int[][] melody = menu
                ? new[]
                {
                    new[] { 76, 79 }, new[] { 72, 76 }, new[] { 77, 74 }, new[] { 79, 74 },
                    new[] { 76, 72 }, new[] { 72, 69 }, new[] { 77, 76 }, new[] { 74, 71 }
                }
                : new[]
                {
                    new[] { 76, 72 }, new[] { 77, 72 }, new[] { 76, 79 }, new[] { 74, 71 },
                    new[] { 76, 72 }, new[] { 77, 81 }, new[] { 79, 74 }, new[] { 76, 69 }
                };

            for (int bar = 0; bar < bars; bar++)
            {
                int barStart = bar * 4 * beat;
                var chord = chords[bar];

                // Pad: the three chord notes, an octave up, swelling in and out over the bar.
                foreach (int note in chord) Tone(mix, barStart, 4 * beat + beat, Hz(note + 12), menu ? 0.06 : (fast ? 0.03 : 0.045), menu ? 1.2 : 0.6, 1.0, 0.0, 0.3);

                if (menu)
                {
                    // A slow, warm loop: a soft bass note on beat 1, a harp-like arpeggio on every beat, a gentle melody. No drums.
                    Tone(mix, barStart, (int)(beat * 3.2f), Hz(chord[0] - 24), 0.1, 0.05, 1.2, 0.0, 0.4);
                    int[] pattern = { 0, 1, 2, 1 };
                    for (int step = 0; step < 4; step++)
                    {
                        int note = chord[pattern[step]] + 24;
                        Pluck(mix, barStart + step * beat, (int)(beat * 1.8f), Hz(note), 0.05, 3.2);
                    }
                    Pluck(mix, barStart, (int)(beat * 2.6f), Hz(melody[bar][0]), 0.08, 1.6);
                    Pluck(mix, barStart + 2 * beat, (int)(beat * 2.6f), Hz(melody[bar][1]), 0.07, 1.6);
                }
                else if (!fast)
                {
                    // Bass: the root on beats 1 and 3.
                    Tone(mix, barStart, (int)(beat * 1.6f), Hz(chord[0] - 24), 0.2, 0.02, 0.5, 0.0, 0.5);
                    Tone(mix, barStart + 2 * beat, (int)(beat * 1.2f), Hz(chord[0] - 24), 0.16, 0.02, 0.4, 0.0, 0.5);

                    // Arpeggio: eighth notes up and down the chord, plucked.
                    int[] pattern = { 0, 1, 2, 1, 0, 2, 1, 2 };
                    for (int step = 0; step < 8; step++)
                    {
                        int note = chord[pattern[step]] + 24;
                        Pluck(mix, barStart + step * beat / 2, (int)(beat * 0.9f), Hz(note), 0.06);
                    }

                    // Melody: two long notes a bar.
                    Pluck(mix, barStart, 2 * beat, Hz(melody[bar][0]), 0.09, 2.2);
                    Pluck(mix, barStart + 2 * beat, 2 * beat, Hz(melody[bar][1]), 0.08, 2.2);

                    // Drums, kept soft: kick on 1 and 3, a light snare on 2 and 4, hats on the off-beats.
                    Kick(mix, barStart, 0.3);
                    Kick(mix, barStart + 2 * beat, 0.26);
                    Snare(mix, barStart + beat, random, 0.07);
                    Snare(mix, barStart + 3 * beat, random, 0.07);
                    for (int e = 0; e < 8; e++) Hat(mix, barStart + e * beat / 2, random, e % 2 == 1 ? 0.035 : 0.018);
                }
                else
                {
                    // Bass: a pulse on every eighth note, jumping an octave on the off-beats.
                    for (int e = 0; e < 8; e++)
                    {
                        int root = chord[0] - 24 + (e % 2 == 1 ? 12 : 0);
                        Tone(mix, barStart + e * beat / 2, (int)(beat * 0.45f), Hz(root), 0.15, 0.01, 0.12, 0.0, 0.5);
                    }

                    // Arpeggio: sixteenth notes, short and bright, one octave higher than the calm version.
                    int[] pattern = { 0, 1, 2, 1, 2, 1, 0, 1, 2, 1, 2, 1, 0, 2, 1, 2 };
                    for (int step = 0; step < 16; step++)
                    {
                        int note = chord[pattern[step]] + 24;
                        Pluck(mix, barStart + step * beat / 4, (int)(beat * 0.5f), Hz(note), 0.045, 9.0);
                    }

                    // Melody: the same two notes, an octave up, with a quick answer on beats 2 and 4.
                    Pluck(mix, barStart, (int)(beat * 1.8f), Hz(melody[bar][0] + 12), 0.075, 2.6);
                    Pluck(mix, barStart + 2 * beat, (int)(beat * 1.8f), Hz(melody[bar][1] + 12), 0.065, 2.6);
                    Pluck(mix, barStart + beat, beat, Hz(chord[1] + 36), 0.04, 6.0);
                    Pluck(mix, barStart + 3 * beat, beat, Hz(chord[2] + 36), 0.04, 6.0);

                    // Drums: a kick on every beat, a firm snare on 2 and 4, hats on every sixteenth.
                    for (int b = 0; b < 4; b++) Kick(mix, barStart + b * beat, b % 2 == 0 ? 0.32 : 0.24);
                    Snare(mix, barStart + beat, random, 0.11);
                    Snare(mix, barStart + 3 * beat, random, 0.11);
                    for (int e = 0; e < 16; e++) Hat(mix, barStart + e * beat / 4, random, e % 2 == 1 ? 0.03 : 0.016);
                }
            }

            var data = new float[total];
            for (int i = 0; i < total; i++) data[i] = (float)Math.Tanh(mix[i] * (fast ? 1.25 : (menu ? 1.0 : 1.1))) * 0.85f;
            return data;
        }

        // A sustained note with a slow swell (attack and release as fractions of its length), wrapping past the end of the loop.
        private static void Tone(double[] mix, int start, int length, double hz, double gain, double attackSeconds, double releaseSeconds, double detune, double harmonic)
        {
            double attack = attackSeconds * SampleRate;
            double release = releaseSeconds * SampleRate;
            for (int i = 0; i < length; i++)
            {
                double envelope = Math.Min(1.0, i / Math.Max(1.0, attack)) * Math.Min(1.0, (length - i) / Math.Max(1.0, release));
                double phase = 2.0 * Math.PI * hz * i / SampleRate;
                double value = Math.Sin(phase) + harmonic * Math.Sin(2.0 * phase + detune) + 0.15 * Math.Sin(3.0 * phase);
                mix[(start + i) % mix.Length] += value * envelope * gain;
            }
        }

        private static void Pluck(double[] mix, int start, int length, double hz, double gain, double decay = 5.5)
        {
            for (int i = 0; i < length; i++)
            {
                double t = (double)i / SampleRate;
                double envelope = Math.Exp(-decay * t) * Math.Min(1.0, i / 120.0);
                double phase = 2.0 * Math.PI * hz * t;
                double value = Math.Sin(phase) + 0.35 * Math.Sin(2.0 * phase) + 0.12 * Math.Sin(3.0 * phase);
                mix[(start + i) % mix.Length] += value * envelope * gain;
            }
        }

        private static void Kick(double[] mix, int start, double gain)
        {
            int length = (int)(SampleRate * 0.25);
            double phase = 0.0;
            for (int i = 0; i < length; i++)
            {
                double t = (double)i / SampleRate;
                double hz = 45.0 + 85.0 * Math.Exp(-t * 28.0);
                phase += 2.0 * Math.PI * hz / SampleRate;
                mix[(start + i) % mix.Length] += Math.Sin(phase) * Math.Exp(-t * 16.0) * gain;
            }
        }

        private static void Snare(double[] mix, int start, System.Random random, double gain)
        {
            int length = (int)(SampleRate * 0.14);
            for (int i = 0; i < length; i++)
            {
                double t = (double)i / SampleRate;
                double noise = random.NextDouble() * 2.0 - 1.0;
                double tone = Math.Sin(2.0 * Math.PI * 190.0 * t);
                mix[(start + i) % mix.Length] += (noise * 0.8 + tone * 0.4) * Math.Exp(-t * 26.0) * gain;
            }
        }

        private static void Hat(double[] mix, int start, System.Random random, double gain)
        {
            int length = (int)(SampleRate * 0.04);
            double previous = 0.0;
            for (int i = 0; i < length; i++)
            {
                double t = (double)i / SampleRate;
                double noise = random.NextDouble() * 2.0 - 1.0;
                double bright = noise - previous;
                previous = noise;
                mix[(start + i) % mix.Length] += bright * Math.Exp(-t * 90.0) * gain;
            }
        }
    }
}
