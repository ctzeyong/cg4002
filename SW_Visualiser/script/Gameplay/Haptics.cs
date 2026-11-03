using UnityEngine;
#if UNITY_IOS && !UNITY_EDITOR
using System.Runtime.InteropServices;
#endif

namespace BridgeRush.Gameplay
{
    // Short vibration pulses of three strengths. iOS uses the Taptic Engine through Plugins/iOS/BridgeRushHaptics.mm;
    // Android uses a timed one-shot vibration. Handheld.Vibrate() is not used: it is one long fixed buzz.
    public static class Haptics
    {
        public enum Strength { Light = 0, Medium = 1, Heavy = 2 }

        private const string EnabledKey = "haptics_enabled";
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

        public static void Pulse(Strength strength)
        {
            if (!Enabled) return;
            PlatformPulse(strength);
        }

#if UNITY_IOS && !UNITY_EDITOR
        [DllImport("__Internal")]
        private static extern void BridgeRushHaptic(int style);

        private static void PlatformPulse(Strength strength) => BridgeRushHaptic((int)strength);
#elif UNITY_ANDROID && !UNITY_EDITOR
        private static AndroidJavaObject vibrator;
        private static int sdkVersion;

        private static void PlatformPulse(Strength strength)
        {
            try
            {
                if (vibrator == null)
                {
                    using var player = new AndroidJavaClass("com.unity3d.player.UnityPlayer");
                    using var activity = player.GetStatic<AndroidJavaObject>("currentActivity");
                    vibrator = activity.Call<AndroidJavaObject>("getSystemService", "vibrator");
                    using var version = new AndroidJavaClass("android.os.Build$VERSION");
                    sdkVersion = version.GetStatic<int>("SDK_INT");
                }
                if (vibrator == null) return;

                long milliseconds = strength == Strength.Light ? 20 : (strength == Strength.Medium ? 40 : 70);
                int amplitude = strength == Strength.Light ? 60 : (strength == Strength.Medium ? 140 : 255);
                if (sdkVersion >= 26)
                {
                    using var effectClass = new AndroidJavaClass("android.os.VibrationEffect");
                    using var effect = effectClass.CallStatic<AndroidJavaObject>("createOneShot", milliseconds, amplitude);
                    vibrator.Call("vibrate", effect);
                }
                else
                {
                    vibrator.Call("vibrate", milliseconds);
                }
            }
            catch (System.Exception exception)
            {
                Debug.LogWarning($"Haptics: {exception.Message}");
            }
        }
#else
        private static void PlatformPulse(Strength strength) { }
#endif
    }
}
