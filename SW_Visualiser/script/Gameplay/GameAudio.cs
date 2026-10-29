using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    // Sound effects without audio assets: each sound is synthesised once from code the first time it is needed.
    // A clip assigned in the Inspector (SpellCaster, GameStateRenderer) replaces the synthesised one.
    public static class GameAudio
    {
        private const int SampleRate = 22050;
        private const string MutedKey = "sfx_muted";

        private static readonly Dictionary<string, AudioClip> cache = new();
        private static AudioSource source;
        private static bool mutedLoaded;
        private static bool muted;

        public static float Volume { get; set; } = 0.8f;

        public static bool Muted
        {
            get
            {
                if (!mutedLoaded) { muted = PlayerPrefs.GetInt(MutedKey, 0) == 1; mutedLoaded = true; }
                return muted;
            }
            set
            {
                muted = value;
                mutedLoaded = true;
                PlayerPrefs.SetInt(MutedKey, value ? 1 : 0);
            }
        }

        public static void PlaySummon(UnitType type, AudioClip overrideClip = null) =>
            Play(overrideClip != null ? overrideClip : Get("summon_" + type, () => Synthesize(type)), 0.9f);

        public static void PlayLightningCharge(AudioClip overrideClip = null) =>
            Play(overrideClip != null ? overrideClip : Get("lightning_charge", LightningCharge), 0.7f);

        public static void PlayLightningCrack(AudioClip overrideClip = null) =>
            Play(overrideClip != null ? overrideClip : Get("lightning_crack", LightningCrack), 1f);

        public static void PlayAnnouncement() => Play(Get("announcement", Announcement), 0.9f);

        public static void PlayFireballThrow(AudioClip overrideClip = null) =>
            Play(overrideClip != null ? overrideClip : Get("fireball_throw", FireballThrow), 0.8f);

        public static void PlayFireballBoom(AudioClip overrideClip = null) =>
            Play(overrideClip != null ? overrideClip : Get("fireball_boom", FireballBoom), 1f);

        private static void Play(AudioClip clip, float volume)
        {
            if (clip == null || Muted) return;
            if (source == null)
            {
                var go = new GameObject("GameAudio");
                Object.DontDestroyOnLoad(go);
                source = go.AddComponent<AudioSource>();
                source.playOnAwake = false;
                source.spatialBlend = 0f;
            }
            source.PlayOneShot(clip, Mathf.Clamp01(volume * Volume));
        }

        private static AudioClip Get(string key, System.Func<AudioClip> build)
        {
            if (cache.TryGetValue(key, out var clip) && clip != null) return clip;
            clip = build();
            cache[key] = clip;
            return clip;
        }

        private static AudioClip Synthesize(UnitType type)
        {
            switch (type)
            {
                case UnitType.Warrior: return Warrior();
                case UnitType.Rogue: return Rogue();
                case UnitType.Mage: return Mage();
                case UnitType.Tank: return Tank();
                default: return Hero();
            }
        }

        // sample(t seconds, u 0..1 through the clip) -> -1..1. A 4 ms fade-in keeps every sound click-free.
        private static AudioClip Make(string name, float seconds, System.Func<float, float, float> sample)
        {
            int count = Mathf.CeilToInt(SampleRate * seconds);
            var data = new float[count];
            for (int i = 0; i < count; i++)
            {
                float t = i / (float)SampleRate;
                float fadeIn = Mathf.Min(1f, t / 0.004f);
                float fadeOut = Mathf.Min(1f, (count - i) / (SampleRate * 0.01f));
                data[i] = Soft(sample(t, i / (float)count)) * fadeIn * fadeOut;
            }
            var clip = AudioClip.Create(name, count, 1, SampleRate, false);
            clip.SetData(data, 0);
            return clip;
        }

        private static float Soft(float v) => (float)System.Math.Tanh(v);
        private static float Sine(float hz, float t) => Mathf.Sin(2f * Mathf.PI * hz * t);
        private static float Noise() => Random.value * 2f - 1f;
        private static float Decay(float t, float rate) => Mathf.Exp(-rate * t);

        // Phase of a sine sweeping from f0 to f1 over the clip length T.
        private static float Sweep(float f0, float f1, float t, float length) =>
            Mathf.Sin(2f * Mathf.PI * (f0 * t + (f1 - f0) * t * t / (2f * length)));

        // A decaying note that starts at `start` seconds, with two harmonics for body.
        private static float Note(float hz, float start, float t, float rate, float gain)
        {
            if (t < start) return 0f;
            float local = t - start;
            float tone = Sine(hz, local) + 0.3f * Sine(hz * 2f, local) + 0.12f * Sine(hz * 3f, local);
            return tone * Decay(local, rate) * gain * Mathf.Min(1f, local / 0.005f);
        }

        // Metal clang over a low thud.
        private static AudioClip Warrior() => Make("summon_warrior", 0.4f, (t, u) =>
            0.55f * Sine(170f, t) * Decay(t, 10f)
            + 0.32f * (Sine(523f, t) + 0.6f * Sine(784f, t) + 0.35f * Sine(1318f, t)) * Decay(t, 13f)
            + 0.3f * Noise() * Decay(t, 70f));

        // A quick rising swish.
        private static AudioClip Rogue()
        {
            float previous = 0f;
            return Make("summon_rogue", 0.28f, (t, u) =>
            {
                float n = Noise();
                float highPassed = n - previous;
                previous = n;
                float envelope = Mathf.Pow(Mathf.Sin(Mathf.PI * u), 2f);
                return 0.5f * highPassed * envelope + 0.22f * Sweep(700f, 2600f, t, 0.28f) * envelope;
            });
        }

        // A rising sparkle of three bell notes.
        private static AudioClip Mage() => Make("summon_mage", 0.6f, (t, u) =>
            Note(659f, 0f, t, 6f, 0.3f) + Note(880f, 0.09f, t, 6f, 0.3f) + Note(1175f, 0.18f, t, 5f, 0.3f));

        // A deep boom that drops in pitch.
        private static AudioClip Tank() => Make("summon_tank", 0.65f, (t, u) =>
            0.9f * Sweep(95f, 48f, t, 0.65f) * Decay(t, 4.5f)
            + 0.4f * Sine(140f, t) * Decay(t, 7f)
            + 0.25f * Noise() * Decay(t, 35f));

        // A short three-note fanfare.
        private static AudioClip Hero() => Make("summon_hero", 0.75f, (t, u) =>
            Note(523f, 0f, t, 5f, 0.3f) + Note(659f, 0.14f, t, 5f, 0.3f) + Note(784f, 0.28f, t, 3.2f, 0.34f));

        // Rises in pitch and loudness for the half-second of the telegraph.
        private static AudioClip LightningCharge() => Make("lightning_charge", 0.5f, (t, u) =>
        {
            float tone = Sweep(180f, 1500f, t, 0.5f);
            float buzz = 0.4f * Sweep(360f, 3000f, t, 0.5f);
            return (tone + buzz) * 0.3f * Mathf.Pow(u, 1.5f) + 0.08f * Noise() * u;
        });

        // A sharp crack, a falling zap and a rumble of thunder.
        private static AudioClip LightningCrack() => Make("lightning_crack", 1f, (t, u) =>
            0.95f * Noise() * Decay(t, 20f)
            + 0.55f * Sweep(3200f, 150f, t, 0.18f) * Decay(t, 14f)
            + 0.6f * Sine(55f, t) * Decay(t, 3.5f)
            + 0.3f * Sine(92f, t) * Decay(t, 5f));

        // A bright rising chime for an announcement: four bell notes up a major chord, then a shimmer.
        private static AudioClip Announcement() => Make("announcement", 1.3f, (t, u) =>
            Note(523f, 0f, t, 4f, 0.28f) + Note(659f, 0.12f, t, 4f, 0.28f) + Note(784f, 0.24f, t, 4f, 0.28f) + Note(1047f, 0.36f, t, 3f, 0.32f)
            + 0.12f * Sweep(900f, 2400f, t, 1.3f) * Mathf.Sin(Mathf.PI * u) * Decay(t, 1.5f));

        // A whoosh that rises in pitch and brightness as the ball flies.
        private static AudioClip FireballThrow()
        {
            float low = 0f;
            return Make("fireball_throw", 0.7f, (t, u) =>
            {
                float cutoff = Mathf.Lerp(0.04f, 0.5f, u);
                low += (Noise() - low) * cutoff;
                float envelope = Mathf.Pow(Mathf.Sin(Mathf.PI * Mathf.Min(1f, u * 0.9f + 0.05f)), 1.2f);
                return (2.2f * low + 0.25f * Sweep(160f, 520f, t, 0.7f)) * envelope * 0.9f;
            });
        }

        // A soft boom with a rumble and a tail of crackles for the burning floor.
        private static AudioClip FireballBoom()
        {
            float low = 0f;
            return Make("fireball_boom", 1.2f, (t, u) =>
            {
                low += (Noise() - low) * 0.12f;
                float crackle = Random.value > 0.985f ? Noise() : 0f;
                return 0.8f * Sweep(130f, 42f, t, 0.5f) * Decay(t, 5f)
                    + 1.4f * low * Decay(t, 9f)
                    + 0.55f * crackle * Decay(t, 2.2f) * Mathf.Clamp01(t * 8f);
            });
        }
    }
}
