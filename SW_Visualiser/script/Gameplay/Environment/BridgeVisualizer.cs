using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    public class BridgeVisualizer : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private GameObject bridgePrefab;
        [SerializeField] private Vector3 rotationOffsetEuler;
        [SerializeField] private float lengthMultiplier = 1.3f;
        [SerializeField] private float widthMultiplier = 1f;
        [SerializeField] private float verticalOffsetMeters = 0f;

        [Header("Deck height profile")]
        [SerializeField] private int deckSampleCount = 24;
        [SerializeField] private float raycastHeightAboveDeck = 3f;
        [SerializeField] private float fallbackDeckHeightMeters = 0.15f;

        private GameObject instance;
        private float[] deckHeightProfile;
        private bool hasDeckProfile;
        private Vector3 deckOwnEdge;
        private Vector3 deckOpponentEdge;

        private void Awake()
        {
            if (matchState == null || bridgePrefab == null)
            {
                Debug.LogError($"{nameof(BridgeVisualizer)}: needs a Match State and a Bridge Prefab.", this);
                enabled = false;
            }
        }

        private void Update()
        {
            if (!matchState.HasBridge)
            {
                if (instance != null)
                {
                    Destroy(instance);
                    instance = null;
                }
                hasDeckProfile = false;
                return;
            }

            if (instance == null)
            {
                PlaceBridge();
            }
        }

        // Returns the height of the bridge deck at a given world position. 
        // If the bridge is not present, returns the y coordinate of the world position.
            public float DeckHeightAt(Vector3 worldPosition)
        {
            if (!matchState.HasBridge) return worldPosition.y;

            float spanLength = Vector3.Distance(deckOwnEdge, deckOpponentEdge);
            if (spanLength < 0.0001f) return deckOwnEdge.y;

            Vector3 direction = (deckOpponentEdge - deckOwnEdge) / spanLength;
            float t = Mathf.Clamp01(Vector3.Dot(worldPosition - deckOwnEdge, direction) / spanLength);
            float floorY = deckOwnEdge.y;

            if (!hasDeckProfile || deckHeightProfile == null || deckHeightProfile.Length < 2)
            {
                const float rampFraction = 0.08f;
                float ramp = t < rampFraction ? t / rampFraction
                    : (t > 1f - rampFraction ? (1f - t) / rampFraction : 1f);
                return floorY + fallbackDeckHeightMeters * ramp;
            }

            float scaled = t * (deckHeightProfile.Length - 1);
            int index = Mathf.Clamp(Mathf.FloorToInt(scaled), 0, deckHeightProfile.Length - 2);
            float localT = scaled - index;
            float height = Mathf.Lerp(deckHeightProfile[index], deckHeightProfile[index + 1], localT);
            return floorY + height;
        }
        // places the bridge prefab in the scene, scaling and rotating it to fit the match state
        private void PlaceBridge()
        {
            Vector3 bridgePosition = matchState.BridgePosition;
            Vector3 laneDirection = (matchState.OpponentHomeAnchor - matchState.OwnHomeAnchor).normalized;

            instance = Instantiate(bridgePrefab, bridgePosition, Quaternion.identity);
            float alongX = ModelFit.ExtentAlong(instance, Vector3.right);
            float alongZ = ModelFit.ExtentAlong(instance, Vector3.forward);

            float autoYaw = alongX > alongZ ? -90f : 0f;
            instance.transform.rotation = Quaternion.LookRotation(laneDirection, Vector3.up) * Quaternion.Euler(0f, autoYaw, 0f) * Quaternion.Euler(rotationOffsetEuler);

            float targetLengthMeters = Vector3.Distance(matchState.OwnBridgeEdge, matchState.OpponentBridgeEdge) * lengthMultiplier;
            Vector3 lateralDirection = Vector3.Cross(Vector3.up, laneDirection).normalized;
            FitBridgeSize(instance, laneDirection, lateralDirection, targetLengthMeters, widthMultiplier);
            PlaceOnPoint(instance, bridgePosition, verticalOffsetMeters);

            EnsureColliders(instance);
            ComputeDeckProfile();
        }
        // scales the bridge prefab to fit the match state, along the lane direction and the lateral direction
        private static void FitBridgeSize(GameObject instance, Vector3 laneDirection, Vector3 lateralDirection, float targetLengthMeters, float widthMultiplier)
        {
            float length = ModelFit.ExtentAlong(instance, laneDirection);
            if (length < 0.0001f) return;
            instance.transform.localScale *= targetLengthMeters / length;

            float width = ModelFit.ExtentAlong(instance, lateralDirection);
            if (width < 0.0001f) return;

            Vector3 sideways = Quaternion.Inverse(instance.transform.rotation) * lateralDirection;
            Vector3 scale = instance.transform.localScale;
            if (Mathf.Abs(sideways.x) >= Mathf.Abs(sideways.z)) scale.x *= widthMultiplier;
            else scale.z *= widthMultiplier;
            instance.transform.localScale = scale;
        }
        // moves the bridge prefab to the target position, adjusting for its bounds and a vertical offset
        private static void PlaceOnPoint(GameObject instance, Vector3 targetPosition, float verticalOffsetMeters)
        {
            var renderers = instance.GetComponentsInChildren<Renderer>();
            if (renderers.Length == 0) return;

            var bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);

            Vector3 correction = new Vector3(
                targetPosition.x - bounds.center.x,
                targetPosition.y - bounds.min.y + verticalOffsetMeters,
                targetPosition.z - bounds.center.z);
            instance.transform.position += correction;
        }

        private static void EnsureColliders(GameObject instance)
        {
            foreach (var meshFilter in instance.GetComponentsInChildren<MeshFilter>())
            {
                if (meshFilter.GetComponent<Collider>() != null) continue;
                if (meshFilter.sharedMesh == null) continue;
                var collider = meshFilter.gameObject.AddComponent<MeshCollider>();
                collider.sharedMesh = meshFilter.sharedMesh;
            }
        }
        // was used as my bridge was not flat, but now I have a flat bridge model, so this is not needed anymore. 
        // Keeping it in case we want to use it again in the future.
        // still useful to keep the deck height profile for other purposes, like placing units on the bridge
        private void ComputeDeckProfile()
        {
            hasDeckProfile = false;
            deckOwnEdge = matchState.OwnBridgeEdge;
            deckOpponentEdge = matchState.OpponentBridgeEdge;
            float floorY = deckOwnEdge.y;

            var deckColliders = instance.GetComponentsInChildren<Collider>();
            if (deckColliders.Length == 0)
            {
                deckHeightProfile = null;
                return;
            }

            Physics.SyncTransforms();

            var samples = new float[deckSampleCount];
            var hitMask = new bool[deckSampleCount];
            int hitCount = 0;

            for (int i = 0; i < deckSampleCount; i++)
            {
                float t = i / (float)(deckSampleCount - 1);
                Vector3 samplePoint = Vector3.Lerp(deckOwnEdge, deckOpponentEdge, t);
                var ray = new Ray(new Vector3(samplePoint.x, floorY + raycastHeightAboveDeck, samplePoint.z), Vector3.down);

                float best = float.NegativeInfinity;
                foreach (var collider in deckColliders)
                {
                    if (collider.Raycast(ray, out var hit, raycastHeightAboveDeck * 2f)) best = Mathf.Max(best, hit.point.y);
                }

                if (float.IsNegativeInfinity(best)) continue;
                samples[i] = best - floorY;
                hitMask[i] = true;
                hitCount++;
            }

            if (hitCount < deckSampleCount / 2)
            {
                deckHeightProfile = null;
                return;
            }

            for (int i = 0; i < deckSampleCount; i++)
            {
                if (hitMask[i]) continue;
                for (int distance = 1; distance < deckSampleCount; distance++)
                {
                    if (i - distance >= 0 && hitMask[i - distance]) { samples[i] = samples[i - distance]; break; }
                    if (i + distance < deckSampleCount && hitMask[i + distance]) { samples[i] = samples[i + distance]; break; }
                }
            }

            float endShift = -(samples[0] + samples[deckSampleCount - 1]) * 0.5f;
            instance.transform.position += new Vector3(0f, endShift, 0f);
            for (int i = 0; i < samples.Length; i++) samples[i] += endShift;

            deckHeightProfile = samples;
            hasDeckProfile = true;
        }
    }
}
