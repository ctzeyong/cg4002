using UnityEngine;
#if ENABLE_INPUT_SYSTEM && !ENABLE_LEGACY_INPUT_MANAGER
using UnityEngine.InputSystem;
#endif

namespace BridgeRush.Input
{
    // Reads the phone's accelerometer and tells MatchHudUI when the player has shaken it to lock or unlock the tower.
    // It works with the old Input Manager and with the new Input System package (whichever Active Input Handling is on).
    // The shake rules live in ShakeDetector. This class only supplies readings. It does not mute them while the screen is
    // touched: you cannot hold a phone without a thumb on it, and that muted every shake on a real iPhone.
    //
    // Note: inside namespace BridgeRush.Input a bare "Input" means this namespace, so Unity's class is written in full.
    public sealed class TowerShakeInput
    {
        private const float MetresPerSecondSquaredPerG = 9.80665f;

        public readonly ShakeDetector Detector = new ShakeDetector();

        private float unitScale = 1f;
        private bool scaleChecked;

        // When the shake just recognised began (Time.unscaledTime clock).
        public float ShakeStartTime { get; private set; }

        // True on the frame a shake is recognised. Call once per frame, only while a shake should count.
        public bool Poll()
        {
#if UNITY_EDITOR
            if (EditorKeyPressed()) { ShakeStartTime = Time.unscaledTime; return true; }
#endif

            if (!TryReadAcceleration(out Vector3 a)) return false;

            // The new Input System can report m/s^2 on Android. Gravity alone is about 1 g, so a first reading above 5 is m/s^2.
            if (!scaleChecked)
            {
                scaleChecked = true;
                unitScale = a.magnitude > 5f ? 1f / MetresPerSecondSquaredPerG : 1f;
            }
            a *= unitScale;

            if (!Detector.Feed(Time.unscaledTime, a.x, a.y, a.z)) return false;
            ShakeStartTime = Detector.LastShakeStart;
            return true;
        }

        // Call when shakes must not count (paused, no match) so a half-finished shake does not carry over.
        public void Reset() => Detector.Reset();

#if ENABLE_LEGACY_INPUT_MANAGER
        private static bool TryReadAcceleration(out Vector3 a)
        {
            a = UnityEngine.Input.acceleration;
            return SystemInfo.supportsAccelerometer;
        }

#if UNITY_EDITOR
        private static bool EditorKeyPressed() => UnityEngine.Input.GetKeyDown(KeyCode.L);
#endif
#elif ENABLE_INPUT_SYSTEM
        private static bool TryReadAcceleration(out Vector3 a)
        {
            a = default;
            var accel = Accelerometer.current;
            if (accel == null) return false;
            if (!accel.enabled) InputSystem.EnableDevice(accel);
            a = accel.acceleration.ReadValue();
            return true;
        }

#if UNITY_EDITOR
        private static bool EditorKeyPressed() => Keyboard.current != null && Keyboard.current.lKey.wasPressedThisFrame;
#endif
#else
        private static bool TryReadAcceleration(out Vector3 a) { a = default; return false; }
#if UNITY_EDITOR
        private static bool EditorKeyPressed() => false;
#endif
#endif
    }
}
