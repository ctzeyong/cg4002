using System;
using UnityEngine;

namespace BridgeRush.Tracking
{
    // Interface for a marker tracker that provides events for marker updates and losses.
    public interface IMarkerTracker
    {
        event Action<int, Pose> MarkerUpdated;
        event Action<int> MarkerLost;
    }

    // Provides the axis of the lane between the own device and the opponent's marker, including origin, direction, and length.
    public readonly struct LaneAxis
    {
        public readonly Vector3 Origin;
        public readonly Vector3 Direction;
        public readonly float Length;

        // Constructs a LaneAxis with the specified origin, direction, and length.
        public LaneAxis(Vector3 origin, Vector3 direction, float length)
        {
            Origin = origin;
            Direction = direction;
            Length = length;
        }

        // Represents an invalid lane axis with zero length and a default direction.
        public static readonly LaneAxis Invalid = new(Vector3.zero, Vector3.forward, 0f);

        // Indicates whether the lane axis is valid / length is more than 0 (i.e., has a non-negligible length).
        public bool IsValid => Length > 0.0001f;
    }
    
    // Represents a frame of the arena, including the own device's position and the opponent's marker position, and provides methods to compute the lane axis and world positions along the lane.
    public class ArenaFrame : MonoBehaviour
    {
        [SerializeField] private MonoBehaviour markerTrackerBehaviour;
        [SerializeField] private Transform ownDeviceTransform;
        [Tooltip("Optional. Falls back to floor height 0 when unassigned or before a floor is found.")]
        [SerializeField] private FloorHeightProvider floorHeightProvider;
        [SerializeField] private int opponentMarkerId;
        [Tooltip("How long the opponent marker can stay out of view before the held pose is flagged long-stale.")]
        // 8 seconnds timer to consider the opponent marker as long-stale if it is not tracked for this duration.
        [SerializeField] private float longStaleTimeoutSeconds = 8f;
        [Tooltip("Smooths detection jitter. 0 disables smoothing.")]
        // 0.12 seconds smoothing time for the opponent marker position to reduce jitter in tracking.
        [SerializeField] private float opponentSmoothTimeSeconds = 0.12f;

        private IMarkerTracker markerTracker;
        private Pose lastOpponentPose;
        private bool hasOpponentPose;
        private float timeSinceLastOpponentUpdate;
        private Vector3 smoothedOpponentPosition;
        private Vector3 opponentSmoothVelocity;
        private bool opponentLocked;

        public bool IsOpponentTracked { get; private set; }
        // IsLongStale is true only once you've had a real opponent lock, then lost visual tracking, and it's been lost for a while 
        public bool IsLongStale => hasOpponentPose && !IsOpponentTracked && timeSinceLastOpponentUpdate >= longStaleTimeoutSeconds;
        public bool HasLane => Lane.IsValid;
        public LaneAxis Lane { get; private set; } = LaneAxis.Invalid;

        public void SetOpponentMarkerId(int markerId) => opponentMarkerId = markerId;

        // this is only for testing, to simulate an opponent marker at a given distance ahead of the own device, in the forward direction of the own device.
        public void SetFakeOpponent(float distanceMetersAhead)
        {
            Vector3 flatForward = new Vector3(ownDeviceTransform.forward.x, 0f, ownDeviceTransform.forward.z);
            if (flatForward.sqrMagnitude < 0.0001f) flatForward = Vector3.forward;
            flatForward.Normalize();

            var fakePosition = ownDeviceTransform.position + flatForward * distanceMetersAhead;
            OnMarkerUpdated(opponentMarkerId, new Pose(fakePosition, Quaternion.LookRotation(-flatForward, Vector3.up)));
        }
        
        // Locks or unlocks the opponent marker tracking. When locked, the opponent marker will not update its position even if the marker is detected.
        public void SetOpponentLocked(bool locked)
        {
            opponentLocked = locked;
            if (!locked) hasOpponentPose = false;
        }

        // help to reset opponent tracking when the "Restart Game" button is pressed in the debug panel,
        public void DebugForgetOpponent()
        {
            hasOpponentPose = false;
            IsOpponentTracked = false;
            Lane = LaneAxis.Invalid;
        }

        private void Awake()
        {
            markerTracker = markerTrackerBehaviour as IMarkerTracker;
        }

        private void OnEnable()
        {
            markerTracker.MarkerUpdated += OnMarkerUpdated;
            markerTracker.MarkerLost += OnMarkerLost;
        }

        private void OnDisable()
        {
            markerTracker.MarkerUpdated -= OnMarkerUpdated;
            markerTracker.MarkerLost -= OnMarkerLost;
        }

        // 
        private void OnMarkerUpdated(int markerId, Pose pose)
        {   
            // Ignore updates for the opponent if the marker is locked or if the update is for a different marker.
            if (markerId != opponentMarkerId || opponentLocked) return;

            // If this is the first update for the opponent, initialize the smoothed position and velocity.
            if (!hasOpponentPose)   
            {
                smoothedOpponentPosition = pose.position;
                opponentSmoothVelocity = Vector3.zero;
            }
            
            // Update the last known pose and reset the timer for how long it's been since the last update.
            lastOpponentPose = pose;
            hasOpponentPose = true;
            timeSinceLastOpponentUpdate = 0f;
            IsOpponentTracked = true;
        }

        // Called when a marker is lost.
        private void OnMarkerLost(int markerId)
        {
            if (markerId != opponentMarkerId || !IsOpponentTracked || opponentLocked) return;

            IsOpponentTracked = false;
        }

        // Called every frame to update the smoothed position of the opponent and calculate the lane axis.
        private void Update()
        {
            timeSinceLastOpponentUpdate += Time.deltaTime;

            if (!hasOpponentPose) return;
            // Smooth the opponent's position to reduce jitter, using SmoothDamp if smoothing is enabled.
            smoothedOpponentPosition = opponentSmoothTimeSeconds > 0f
                ? Vector3.SmoothDamp(smoothedOpponentPosition, lastOpponentPose.position, ref opponentSmoothVelocity, opponentSmoothTimeSeconds)
                : lastOpponentPose.position;

            // get the flattened positions of the own device and the smoothed opponent position, then calculate the lane axis based on these positions.
            var origin = Flatten(ownDeviceTransform.position);
            var offset = Flatten(smoothedOpponentPosition) - origin;
            float length = offset.magnitude;

            if (length < 0.0001f) return;

            Lane = new LaneAxis(origin, offset / length, length);
        }
        // Flattens a position to the floor height, using the floor height provider if available else 0
        private Vector3 Flatten(Vector3 position)
        {
            float floorY = floorHeightProvider != null && floorHeightProvider.HasDetectedFloor
                ? floorHeightProvider.FloorY
                : 0f;
            return new Vector3(position.x, floorY, position.z);
        }

        public Vector3 WorldPositionFromNormalized(float t)
        {
            if (!HasLane) return Vector3.zero;
            return Lane.Origin + Lane.Direction * (Mathf.Clamp01(t) * Lane.Length);
        }
    }
}