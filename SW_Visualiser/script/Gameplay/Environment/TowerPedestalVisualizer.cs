using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    public class TowerPedestalVisualizer : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private GameObject pedestalPrefab;
        [SerializeField] private float targetHeightMeters = 0.12f;

        public float TargetHeightMeters => measuredHeightMeters >= 0f ? measuredHeightMeters : targetHeightMeters;

        private GameObject ownInstance;
        private GameObject opponentInstance;
        private float measuredHeightMeters = -1f;

        // build once while the bridge is present, and destroy when the bridge is gone
        private void Update()
        {
            if (!matchState.HasBridge)
            {
                if (ownInstance != null) { Destroy(ownInstance); ownInstance = null; }
                if (opponentInstance != null) { Destroy(opponentInstance); opponentInstance = null; }
                measuredHeightMeters = -1f;
                return;
            }

            Vector3 laneDirection = (matchState.OpponentHomeAnchor - matchState.OwnHomeAnchor).normalized;

            if (ownInstance == null)
            {
                ownInstance = PlacePedestal(matchState.OwnHomeAnchor, matchState.OwnBridgeEdge, laneDirection);
            }
            if (opponentInstance == null)
            {
                opponentInstance = PlacePedestal(matchState.OpponentHomeAnchor, matchState.OpponentBridgeEdge, -laneDirection);
            }
        }
        // used to place a pedestal exactly between the home anchor and the bridge edge, and to measure its height
        private GameObject PlacePedestal(Vector3 homeAnchor, Vector3 bridgeEdge, Vector3 facingDirection)
        {
            Vector3 rearEdge = homeAnchor - facingDirection * matchState.RearMarginMeters;
            Vector3 center = (rearEdge + bridgeEdge) * 0.5f;
            float length = Vector3.Distance(rearEdge, bridgeEdge);
            float width = matchState.IslandWidthMeters;
            Quaternion rotation = Quaternion.LookRotation(facingDirection, Vector3.up);

            var instance = Instantiate(pedestalPrefab, center, rotation);
            Vector3 lateralDirection = Vector3.Cross(Vector3.up, facingDirection).normalized;
            Normalize(instance, facingDirection, lateralDirection, targetHeightMeters, length, width, center);

            if (measuredHeightMeters < 0f)
            {
                var renderers = instance.GetComponentsInChildren<Renderer>();
                if (renderers.Length > 0)
                {
                    var bounds = renderers[0].bounds;
                    for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);
                    measuredHeightMeters = bounds.size.y;
                }
            }
            return instance;
        }

        private static void Normalize(GameObject instance, Vector3 lengthDirection, Vector3 widthDirection,
            float targetHeightMeters, float targetLengthMeters, float targetWidthMeters, Vector3 targetPosition)
        {
            if (!ModelFit.TryGetBounds(instance, out var heightBounds)) return;

            float currentHeight = heightBounds.size.y;
            float currentLength = ModelFit.ExtentAlong(instance, lengthDirection);
            float currentWidth = ModelFit.ExtentAlong(instance, widthDirection);

            var localScale = instance.transform.localScale;
            if (currentHeight > 0.0001f) localScale.y *= targetHeightMeters / currentHeight;
            if (currentLength > 0.0001f) localScale.z *= targetLengthMeters / currentLength;
            if (currentWidth > 0.0001f) localScale.x *= targetWidthMeters / currentWidth;
            instance.transform.localScale = localScale;

            if (!ModelFit.TryGetBounds(instance, out var bounds)) return;

            Vector3 correction = new Vector3(
                bounds.center.x - targetPosition.x,
                bounds.max.y - targetPosition.y,
                bounds.center.z - targetPosition.z);
            instance.transform.position -= correction;
        }
    }
}
