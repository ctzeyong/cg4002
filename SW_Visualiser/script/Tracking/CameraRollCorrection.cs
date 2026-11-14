using UnityEngine;

namespace BridgeRush.Tracking
{
    // Turns the AR camera by a quarter turn, 0 / 90 / 180 / 270 degrees about its own forward axis, after the tracking has set its
    // rotation. Needed on devices that report the camera pose for a different screen orientation than the app is showing: on an
    // iPad Air (5th gen, iPadOS 27) the 3D world came out rolled a quarter turn against the real room, so towers lay on their side
    // while the UI and the camera video looked right (measured 2026-11-14, camera euler z about 270).
    //
    // Chosen from the debug menu (Tracking, "Camera roll"), remembered per device, and 0 by default, so a device that works is
    // untouched. Only the rotation changes, never the position.
    //
    // Ordering: the Tracked Pose Driver (Input System) writes the camera's pose in Update and again just before rendering. This
    // runs just after it in Update (execution order) and last before rendering (BeforeRenderOrder), and skips a frame where the
    // rotation is still the one it wrote, so the turn is never applied twice.
    [DefaultExecutionOrder(-29000)]
    public class CameraRollCorrection : MonoBehaviour
    {
        private const string Pref = "camera_roll_quarter_turns";
        private static int quarterTurns = -1;

        // 0 to 3 quarter turns, remembered on this device.
        public static int QuarterTurns
        {
            get
            {
                if (quarterTurns < 0) quarterTurns = ((PlayerPrefs.GetInt(Pref, 0) % 4) + 4) % 4;
                return quarterTurns;
            }
            private set
            {
                quarterTurns = ((value % 4) + 4) % 4;
                PlayerPrefs.SetInt(Pref, quarterTurns);
            }
        }

        public static int Degrees => QuarterTurns * 90;

        // The next quarter turn, adding the correction to the main camera the first time it is needed.
        public static void Cycle()
        {
            QuarterTurns++;
            Ensure();
        }

        private static CameraRollCorrection Ensure()
        {
            var cam = Camera.main;
            if (cam == null) return null;
            return cam.TryGetComponent(out CameraRollCorrection existing) ? existing : cam.gameObject.AddComponent<CameraRollCorrection>();
        }

        // A device with a saved turn gets the correction from the first frame, before the debug menu is ever opened.
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void InstallIfSaved()
        {
            if (QuarterTurns != 0) Ensure();
        }

        private Quaternion lastTurned = Quaternion.identity;
        private bool hasTurned;

        private void OnEnable() => Application.onBeforeRender += OnBeforeRender;

        private void OnDisable() => Application.onBeforeRender -= OnBeforeRender;

        private void Update() => Apply();

        [BeforeRenderOrder(32000)]
        private void OnBeforeRender() => Apply();

        private void Apply()
        {
            int turns = QuarterTurns;
            if (turns == 0)
            {
                hasTurned = false; // the tracking overwrites the rotation next frame, which undoes the last turn
                return;
            }

            var current = transform.localRotation;
            if (hasTurned && Quaternion.Angle(current, lastTurned) < 0.01f) return; // still the rotation written here: do not turn it twice

            lastTurned = current * Quaternion.AngleAxis(turns * 90f, Vector3.forward);
            transform.localRotation = lastTurned;
            hasTurned = true;
        }
    }
}
