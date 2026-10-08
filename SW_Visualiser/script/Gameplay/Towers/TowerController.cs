using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Gameplay
{
    public class TowerController : MonoBehaviour
    {
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private Transform ownDeviceTransform;
        [SerializeField] private MatchState matchState;
        [SerializeField] private FloorHeightProvider floorHeightProvider;
        [SerializeField] private float towerHeight = 0.56f;
        [SerializeField] private Color ownTowerColor = Color.blue;
        [SerializeField] private Color opponentTowerColor = Color.red;
        [SerializeField] private GameObject towerPrefab;
        [SerializeField] private GameObject ownTowerPrefabOverride;
        [SerializeField] private GameObject opponentTowerPrefabOverride;
        [SerializeField] private float baseRingRadiusMultiplier = 1.4f;
        [SerializeField] private Vector3 towerRotationOffsetEuler;
        [SerializeField] private float towerWidthMultiplier = 0.55f;

        public float TowerRadius => measuredTowerFootprintRadius;

        private Transform ownTower;
        private Transform opponentTower;
        private float measuredTowerFootprintRadius = -1f;

        private void Start()
        {
            ownTower = CreateTower("OwnTower", ownTowerColor, ownTowerPrefabOverride);
            opponentTower = CreateTower("OpponentTower", opponentTowerColor, opponentTowerPrefabOverride);
            opponentTower.gameObject.SetActive(false);
        }

        private Transform CreateTower(string name, Color color, GameObject prefabOverride)
        {
            var root = new GameObject(name);
            root.transform.SetParent(transform, false);

            var prefab = prefabOverride != null ? prefabOverride : towerPrefab;
            var instance = Instantiate(prefab, root.transform, false);
            NormalizeHeightAndGround(root.transform, instance, towerHeight);
            instance.transform.localScale = new Vector3(
                instance.transform.localScale.x * towerWidthMultiplier,
                instance.transform.localScale.y,
                instance.transform.localScale.z * towerWidthMultiplier);
            if (measuredTowerFootprintRadius < 0f)
            {
                measuredTowerFootprintRadius = MeasureFootprintRadius(instance);
            }
            CreateBaseRing(root.transform, color, measuredTowerFootprintRadius);

            return root.transform;
        }
        // set the height and also ground the tower.
        private static void NormalizeHeightAndGround(Transform root, GameObject instance, float targetHeightMeters)
        {
            var renderers = instance.GetComponentsInChildren<Renderer>();
            if (renderers.Length == 0) return;

            var bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);

            float currentHeight = bounds.size.y;
            if (currentHeight > 0.0001f)
            {
                instance.transform.localScale *= targetHeightMeters / currentHeight;
            }

            bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);

            Vector3 correction = new Vector3(
                bounds.center.x - root.position.x,
                bounds.min.y - root.position.y,
                bounds.center.z - root.position.z);
            instance.transform.localPosition -= correction;
        }
        // get the radius of the tower's footprint by measuring the bounds of all renderers in the tower.
        private static float MeasureFootprintRadius(GameObject instance)
        {
            var renderers = instance.GetComponentsInChildren<Renderer>();
            if (renderers.Length == 0) return 0f;

            var bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);

            return Mathf.Max(bounds.size.x, bounds.size.z) * 0.5f;
        }
        // create a base ring around the toweer to allow easier visual detection of the tower's footprint,

        private void CreateBaseRing(Transform root, Color color, float baseRadius)
        {
            var ringObject = new GameObject("BaseRing");
            ringObject.transform.SetParent(root, false);

            var lineRenderer = ringObject.AddComponent<LineRenderer>();
            const int segments = 32;
            lineRenderer.positionCount = segments;
            lineRenderer.loop = true;
            lineRenderer.useWorldSpace = false;
            lineRenderer.widthCurve = AnimationCurve.Constant(0f, 1f, 0.03f);
            lineRenderer.material = new Material(Shader.Find("Sprites/Default"));
            lineRenderer.startColor = color;
            lineRenderer.endColor = color;

            float radius = baseRadius * baseRingRadiusMultiplier;
            for (int i = 0; i < segments; i++)
            {
                float angle = i / (float)segments * Mathf.PI * 2f;
                lineRenderer.SetPosition(i, new Vector3(Mathf.Cos(angle) * radius, 0.01f, Mathf.Sin(angle) * radius));
            }
        }
        // constantly update positon, rotation, and visibility of the towers based on the current state of the match and the arena frame.
        private void Update()
        {
            float floorY = matchState.HasBridge
                ? matchState.GroundY
                : (floorHeightProvider != null && floorHeightProvider.HasDetectedFloor ? floorHeightProvider.FloorY : 0f);

            if (matchState.MatchStarted && matchState.OwnTowerLocked)
            {   
                // place the own tower at the locked position at the floor height.
                var lockedPos = matchState.OwnTowerPosition;
                ownTower.position = new Vector3(lockedPos.x, floorY, lockedPos.z);
            }
            else
            {
                // place the own tower at the position of the own device at the floor height.
                var pos = ownDeviceTransform.position;
                ownTower.position = new Vector3(pos.x, floorY, pos.z);
            }

            if (arenaFrame.HasLane)
            {
                var lane = arenaFrame.Lane;
                // place the opponent tower at the end of the lane, and rotate both towers to face each other.
                opponentTower.gameObject.SetActive(true);
                var pos = lane.Origin + lane.Direction * lane.Length;
                opponentTower.position = new Vector3(pos.x, floorY, pos.z);

                var rotationOffset = Quaternion.Euler(towerRotationOffsetEuler);

                Vector3 ownToOpponent = opponentTower.position - ownTower.position;
                Vector3 ownFacingDirection = ownToOpponent.sqrMagnitude > 0.0001f ? ownToOpponent.normalized : lane.Direction;
                // fix the rotation of the towers to face each other, with an additional rotation offset.
                ownTower.rotation = Quaternion.LookRotation(ownFacingDirection, Vector3.up) * rotationOffset;
                opponentTower.rotation = Quaternion.LookRotation(-ownFacingDirection, Vector3.up) * rotationOffset;
            }
            else
            {
                opponentTower.gameObject.SetActive(false);
            }
        }
    }
}
