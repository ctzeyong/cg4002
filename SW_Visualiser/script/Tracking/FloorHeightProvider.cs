using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace BridgeRush.Tracking
{
    // i had issue in simulation that the floor height was not detected correctly, so i added this class to provide a fallback floor height of 0 if no floor is detected.
    public class FloorHeightProvider : MonoBehaviour
    {
        [SerializeField] private ARRaycastManager raycastManager;
        [SerializeField] private Transform ownDeviceTransform;
        [Tooltip("Optional. Fallback when no floor plane is directly below the phone: use the lowest detected horizontal plane.")]
        [SerializeField] private ARPlaneManager planeManager;
        [Tooltip("A fallback plane must be at least this far below the phone, so a table is not mistaken for the floor.")]
        [SerializeField] private float minFallbackDropMeters = 0.6f;

        private static readonly List<ARRaycastHit> Hits = new();

        private const string OffsetPref = "floor_offset_meters";

        private float detectedFloorY;
        private float floorOffsetMeters;

        // Phones without LiDAR read the floor a few centimetres off; this is set per phone from the debug menu and remembered.
        public float FloorOffsetMeters
        {
            get => floorOffsetMeters;
            set
            {
                floorOffsetMeters = Mathf.Clamp(value, -1f, 1f);
                PlayerPrefs.SetFloat(OffsetPref, floorOffsetMeters);
            }
        }

        public float FloorY => detectedFloorY + floorOffsetMeters;
        public bool HasDetectedFloor { get; private set; }
        public bool FromFallback { get; private set; }

        private void Awake()
        {
            floorOffsetMeters = PlayerPrefs.GetFloat(OffsetPref, 0f);
        }

        private void Update()
        {
            // Cast a ray downwards from the own device's position to detect the floor plane.
            var ray = new Ray(ownDeviceTransform.position, Vector3.down);
            if (raycastManager.Raycast(ray, Hits, TrackableType.PlaneWithinPolygon))
            {
                detectedFloorY = Hits[0].pose.position.y;
                HasDetectedFloor = true;
                FromFallback = false;
            }
            else if (planeManager != null && TryLowestHorizontalPlane(out float lowestY))
            {
                detectedFloorY = lowestY;
                HasDetectedFloor = true;
                FromFallback = true;
            }
        }

        // Phones without LiDAR often detect the floor ahead of the user, not under the phone, so the downward ray misses it.
        private bool TryLowestHorizontalPlane(out float lowestY)
        {
            lowestY = float.MaxValue;
            float limit = ownDeviceTransform.position.y - minFallbackDropMeters;
            foreach (var plane in planeManager.trackables)
            {
                if (plane.alignment != PlaneAlignment.HorizontalUp) continue;
                float y = plane.transform.position.y;
                if (y < limit && y < lowestY) lowestY = y;
            }
            return lowestY != float.MaxValue;
        }
    }
}
