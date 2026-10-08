using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    // class to represent the water in the game
    public class WaterVisualizer : MonoBehaviour
    {
        [SerializeField] private MatchState matchState;
        [SerializeField] private TowerPedestalVisualizer towerPedestalVisualizer;
        [SerializeField] private GameObject waterPrefab;
        [SerializeField] private float lengthMultiplier = 1.1f;
        [SerializeField] private float widthMultiplier = 1.3f;
        [SerializeField] private float waterDepthFraction = 0.5f;
        [SerializeField] private float fallbackPedestalThicknessMeters = 0.12f;

        private GameObject instance;

        private void Awake()
        {
            if (matchState == null || waterPrefab == null)
            {
                Debug.LogError($"{nameof(WaterVisualizer)}: needs a Match State and a Water Prefab.", this);
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
                return;
            }

            if (instance == null)
            {
                PlaceWater();
            }
        }

        private void PlaceWater()
        {
            Vector3 ownAnchor = matchState.OwnHomeAnchor;
            Vector3 opponentAnchor = matchState.OpponentHomeAnchor;
            Vector3 center = (ownAnchor + opponentAnchor) * 0.5f;

            float pedestalThickness = towerPedestalVisualizer != null
                ? towerPedestalVisualizer.TargetHeightMeters
                : fallbackPedestalThicknessMeters;
            center.y -= pedestalThickness * Mathf.Clamp01(waterDepthFraction);

            Vector3 laneDirection = (opponentAnchor - ownAnchor).normalized;
            Quaternion laneFacingRotation = Quaternion.LookRotation(laneDirection, Vector3.up);

            instance = Instantiate(waterPrefab, center, laneFacingRotation);

            float lengthMeters = Vector3.Distance(ownAnchor, opponentAnchor) * lengthMultiplier;
            float widthMeters = matchState.IslandWidthMeters * widthMultiplier;
            Vector3 lateralDirection = Vector3.Cross(Vector3.up, laneDirection).normalized;
            NormalizeWater(instance, laneDirection, lateralDirection, lengthMeters, widthMeters, center);
        }

        private static void NormalizeWater(GameObject instance, Vector3 lengthDirection, Vector3 widthDirection, float targetLengthMeters, float targetWidthMeters, Vector3 targetPosition)
        {
            float currentLength = ModelFit.ExtentAlong(instance, lengthDirection);
            float currentWidth = ModelFit.ExtentAlong(instance, widthDirection);

            var localScale = instance.transform.localScale;
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
