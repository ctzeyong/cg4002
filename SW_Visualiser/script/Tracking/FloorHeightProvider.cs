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

        private static readonly List<ARRaycastHit> Hits = new();

        public float FloorY { get; private set; }
        public bool HasDetectedFloor { get; private set; }

        private void Update()
        {
            // Cast a ray downwards from the own device's position to detect the floor plane.
            var ray = new Ray(ownDeviceTransform.position, Vector3.down);
            if (raycastManager.Raycast(ray, Hits, TrackableType.PlaneWithinPolygon))
            {
                FloorY = Hits[0].pose.position.y;
                HasDetectedFloor = true;
            }
        }
    }
}
