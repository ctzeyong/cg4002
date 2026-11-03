using System;
using System.Collections.Generic;
using Unity.Collections;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

namespace BridgeRush.Tracking
{
    [RequireComponent(typeof(ARCameraManager))]
    public class AprilTagTracker : MonoBehaviour, IMarkerTracker
    {
        [Tooltip("True = rotate the CPU image 90 deg clockwise to match a Portrait-locked device.")]
        [SerializeField] private bool rotateClockwise = true;

        [Tooltip("Flip if a tracked tag's position reads mirrored.")]
        [SerializeField] private bool mirrorY = true;

        [Tooltip("Size the detector needs for correct distances. Calibrated on a device: a printed tag measuring 0.185 m reads true distances at 0.119.")]
        [SerializeField] private float tagSizeMeters = 0.119f;

        [Tooltip("Detection downsampling factor - higher is faster but loses range/accuracy.")]
        [SerializeField] private int decimation = 2;

        [Tooltip("tagStandard41h12 tag IDs to track, indexed by marker ID.")]
        [SerializeField] private int[] tagIdToMarkerId = { 0, 1 };

        private const string TagSizePref = "apriltag_size_meters";

        // Tuned live from the debug menu; the last value used is remembered on the phone and overrides the Inspector value.
        public float TagSizeMeters
        {
            get => tagSizeMeters;
            set
            {
                tagSizeMeters = Mathf.Clamp(value, 0.02f, 0.5f);
                PlayerPrefs.SetFloat(TagSizePref, tagSizeMeters);
            }
        }

        public bool RotateClockwise { get => rotateClockwise; set => rotateClockwise = value; }
        public bool MirrorY { get => mirrorY; set => mirrorY = value; }

        // What the detector is fed, for checking the camera intrinsics against the CPU image.
        public string DebugInfo => $"img {lastImageWidth}x{lastImageHeight}  intr {lastIntrinsicsWidth}x{lastIntrinsicsHeight}  f(scaled) {lastFocalX:0}";

        private int lastImageWidth;
        private int lastImageHeight;
        private int lastIntrinsicsWidth;
        private int lastIntrinsicsHeight;
        private float lastFocalX;

        private ARCameraManager cameraManager;
        private AprilTag.TagDetector detector;
        private int detectorWidth;
        private int detectorHeight;
        private readonly Dictionary<int, int> tagIdToMarkerIdLookup = new();
        private readonly HashSet<int> trackedMarkerIds = new();
        private readonly HashSet<int> markerIdsSeenThisFrame = new();
        private readonly List<int> markerIdsToDrop = new();

        public event Action<int, Pose> MarkerUpdated;
        public event Action<int> MarkerLost;

        private void Awake()
        {
            cameraManager = GetComponent<ARCameraManager>();
            if (PlayerPrefs.HasKey(TagSizePref)) tagSizeMeters = PlayerPrefs.GetFloat(TagSizePref);

            tagIdToMarkerIdLookup.Clear();
            for (int i = 0; i < tagIdToMarkerId.Length; i++)
            {
                tagIdToMarkerIdLookup[tagIdToMarkerId[i]] = i;
            }
        }

        private void OnEnable()
        {
            cameraManager.frameReceived += OnCameraFrameReceived;
        }

        private void OnDisable()
        {
            cameraManager.frameReceived -= OnCameraFrameReceived;
        }

        private void OnDestroy()
        {
            detector?.Dispose();
        }

        private void OnCameraFrameReceived(ARCameraFrameEventArgs args)
        {
            if (!cameraManager.TryAcquireLatestCpuImage(out XRCpuImage image)) return;

            XRCameraIntrinsics intrinsics;
            using (image)
            {
                if (!cameraManager.TryGetIntrinsics(out intrinsics)) return;
                lastImageWidth = image.width;
                lastImageHeight = image.height;
                lastIntrinsicsWidth = intrinsics.resolution.x;
                lastIntrinsicsHeight = intrinsics.resolution.y;

                int rotatedWidth = image.height;
                int rotatedHeight = image.width;

                //images get disposed when the using block ends, so we need to create a new detector if the image size changes
                if (detector == null || detectorWidth != rotatedWidth || detectorHeight != rotatedHeight)
                {
                    detector?.Dispose();
                    detectorWidth = rotatedWidth;
                    detectorHeight = rotatedHeight;
                    detector = new AprilTag.TagDetector(detectorWidth, detectorHeight, decimation);
                }

                // Convert the image to RGBA32 format and rotate it if necessary.
                var conversionParams = new XRCpuImage.ConversionParams
                {
                    inputRect = new RectInt(0, 0, image.width, image.height),
                    outputDimensions = new Vector2Int(image.width, image.height),
                    outputFormat = TextureFormat.RGBA32,
                    transformation = mirrorY ? XRCpuImage.Transformation.MirrorY : XRCpuImage.Transformation.None
                };

                int dataSize = image.GetConvertedDataSize(conversionParams);
                using var rawBuffer = new NativeArray<byte>(dataSize, Allocator.Temp);
                image.Convert(conversionParams, rawBuffer);

                //rotate and auto free the buffer
                using var rotatedBuffer = new NativeArray<Color32>(rotatedWidth * rotatedHeight, Allocator.Temp);
                RotateBuffer90(rawBuffer.Reinterpret<Color32>(1), image.width, image.height, rotatedBuffer, rotateClockwise);

                // The intrinsics can be for a different image size than the CPU image; scale the focal length to match it.
                float intrinsicsScale = intrinsics.resolution.x > 0 ? (float)image.width / intrinsics.resolution.x : 1f;
                float focalX = intrinsics.focalLength.x * intrinsicsScale;
                lastFocalX = focalX;
                float verticalFovRadians = 2f * Mathf.Atan2(detectorHeight * 0.5f, focalX);

                detector.ProcessImage(rotatedBuffer.AsReadOnlySpan(), verticalFovRadians, tagSizeMeters);
            }

            var cameraTransform = cameraManager.transform;
            markerIdsSeenThisFrame.Clear();

            // For each detected tag, check if it corresponds to a marker ID and invoke the MarkerUpdated event with the tag's pose.
            foreach (var tag in detector.DetectedTags)
            {
                if (!tagIdToMarkerIdLookup.TryGetValue(tag.ID, out int markerId)) continue;

                var pose = new Pose(
                    cameraTransform.TransformPoint(tag.Position),
                    cameraTransform.rotation * tag.Rotation);

                trackedMarkerIds.Add(markerId);
                markerIdsSeenThisFrame.Add(markerId);
                MarkerUpdated?.Invoke(markerId, pose);
            }

            // Identify markers that were tracked in previous frames but not seen in the current frame, and invoke the MarkerLost event for those markers.
            markerIdsToDrop.Clear();
            foreach (int trackedMarkerId in trackedMarkerIds)
            {
                if (!markerIdsSeenThisFrame.Contains(trackedMarkerId))
                {
                    markerIdsToDrop.Add(trackedMarkerId);
                }
            }

            foreach (int markerId in markerIdsToDrop)
            {
                trackedMarkerIds.Remove(markerId);
                MarkerLost?.Invoke(markerId);
            }
        }

        private static void RotateBuffer90(
            NativeArray<Color32> src, int srcWidth, int srcHeight, NativeArray<Color32> dst, bool clockwise)
        {
            int dstWidth = srcHeight;
            for (int y = 0; y < srcHeight; y++)
            {
                int rowOffset = y * srcWidth;
                for (int x = 0; x < srcWidth; x++)
                {
                    int dstX, dstY;
                    if (clockwise)
                    {
                        dstX = srcHeight - 1 - y;
                        dstY = x;
                    }
                    else
                    {
                        dstX = y;
                        dstY = srcWidth - 1 - x;
                    }
                    dst[dstY * dstWidth + dstX] = src[rowOffset + x];
                }
            }
        }
    }
}
