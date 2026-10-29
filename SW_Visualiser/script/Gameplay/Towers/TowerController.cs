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
        [Tooltip("Height of each tower before the stretch. Both towers use the same height.")]
        [SerializeField] private float towerHeight = 0.56f;
        [Tooltip("How far below the worn tag the ground is. The tag is on the body, so the tower base sits this far below it.")]
        [SerializeField] private float tagToGroundOffsetMeters = 1.1f;
        [SerializeField] private Color ownTowerColor = Color.blue;
        [SerializeField] private Color opponentTowerColor = Color.red;
        [SerializeField] private GameObject towerPrefab;
        [SerializeField] private GameObject ownTowerPrefabOverride;
        [SerializeField] private GameObject opponentTowerPrefabOverride;
        [SerializeField] private float baseRingRadiusMultiplier = 1.4f;
        [SerializeField] private Vector3 towerRotationOffsetEuler;
        [SerializeField] private float towerWidthMultiplier = 0.55f;
        [Tooltip("Makes both towers taller without changing their width. 1 = unchanged, 1.3 = 30% taller.")]
        [SerializeField] private float towerHeightStretch = 1.3f;

        [Tooltip("How far in front of the player the own tower stands, in tower footprint radii. 0 = under the player.")]
        [SerializeField] private float ownTowerForwardOffsetInRadii = 1.5f;

        // Larger of the two footprints, so units stand off from either tower without overlapping it.
        public float TowerRadius => Mathf.Max(ownFootprintRadius, opponentFootprintRadius) * ArenaScale;
        public float OpponentTowerHeightMeters => towerHeight * towerHeightStretch * ArenaScale;
        private float ArenaScale => matchState != null ? matchState.ArenaScale : 1f;
        public float TagToGroundOffsetMeters => tagToGroundOffsetMeters;
        // Follows the opponent's tag. The opponent tower and the capture bubbles are children of it. Null until Start.
        public Transform TagAnchor => tagAnchor;

        private Transform ownTower;
        private Transform opponentTower;
        private Transform tagAnchor;
        private float ownFootprintRadius;
        private float opponentFootprintRadius;

        private void Start()
        {
            ownTower = CreateTower("OwnTower", ownTowerColor, ownTowerPrefabOverride,
                towerHeight * towerHeightStretch, towerWidthMultiplier / towerHeightStretch, out ownFootprintRadius);

            // The anchor sits at the tag; the tower hangs below it by the tag-to-ground offset.
            tagAnchor = new GameObject("OpponentTagAnchor").transform;
            tagAnchor.SetParent(transform, false);
            opponentTower = CreateTower("OpponentTower", opponentTowerColor, opponentTowerPrefabOverride,
                towerHeight * towerHeightStretch, towerWidthMultiplier / towerHeightStretch, out opponentFootprintRadius);
            opponentTower.SetParent(tagAnchor, false);
            opponentTower.localPosition = new Vector3(0f, -tagToGroundOffsetMeters, 0f);
            tagAnchor.gameObject.SetActive(false);
        }

        private Transform CreateTower(string name, Color color, GameObject prefabOverride, float height, float widthMultiplier, out float footprintRadius)
        {
            var root = new GameObject(name);
            root.transform.SetParent(transform, false);

            var prefab = prefabOverride != null ? prefabOverride : towerPrefab;
            var instance = Instantiate(prefab, root.transform, false);
            NormalizeHeightAndGround(root.transform, instance, height);
            instance.transform.localScale = new Vector3(
                instance.transform.localScale.x * widthMultiplier,
                instance.transform.localScale.y,
                instance.transform.localScale.z * widthMultiplier);
            footprintRadius = MeasureFootprintRadius(instance);
            CreateBaseRing(root.transform, color, footprintRadius);

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
            float arenaScale = ArenaScale;
            ownTower.localScale = Vector3.one * arenaScale;
            opponentTower.localScale = Vector3.one * arenaScale;

            float floorY = matchState.HasBridge
                ? matchState.GroundY
                : (floorHeightProvider != null && floorHeightProvider.HasDetectedFloor ? floorHeightProvider.FloorY : 0f);

            Vector3 ownBase;
            if (matchState.MatchStarted && matchState.OwnTowerLocked)
            {
                // place the own tower at the locked position at the floor height.
                var lockedPos = matchState.OwnTowerPosition;
                ownBase = new Vector3(lockedPos.x, floorY, lockedPos.z);
            }
            else
            {
                // place the own tower at the position of the own device at the floor height.
                var pos = ownDeviceTransform.position;
                ownBase = new Vector3(pos.x, floorY, pos.z);
            }

            // Stand the own tower in front of the player, by a distance scaled from the tower's own size.
            Vector3 forward;
            if (arenaFrame.HasLane)
            {
                var lane = arenaFrame.Lane;
                var toOpponent = lane.Origin + lane.Direction * lane.Length - ownBase;
                toOpponent.y = 0f;
                forward = toOpponent.sqrMagnitude > 0.0001f ? toOpponent.normalized : lane.Direction;
            }
            else
            {
                forward = ownDeviceTransform.forward;
                forward.y = 0f;
                forward = forward.sqrMagnitude > 0.0001f ? forward.normalized : Vector3.forward;
            }
            ownTower.position = ownBase + forward * (ownFootprintRadius * arenaScale * ownTowerForwardOffsetInRadii);

            if (arenaFrame.HasLane)
            {
                var lane = arenaFrame.Lane;
                // place the opponent tower at the end of the lane, and rotate both towers to face each other.
                tagAnchor.gameObject.SetActive(true);
                var pos = lane.Origin + lane.Direction * lane.Length;
                // The tower always stands on the floor, whatever height the tag is at. The anchor sits the offset above the
                // floor so the bubbles, which are positioned relative to it, keep their layout.
                tagAnchor.position = new Vector3(pos.x, floorY + tagToGroundOffsetMeters, pos.z);
                opponentTower.localPosition = new Vector3(0f, -tagToGroundOffsetMeters, 0f);

                var rotationOffset = Quaternion.Euler(towerRotationOffsetEuler);

                Vector3 ownToOpponent = new Vector3(pos.x, 0f, pos.z) - new Vector3(ownTower.position.x, 0f, ownTower.position.z);
                Vector3 ownFacingDirection = ownToOpponent.sqrMagnitude > 0.0001f ? ownToOpponent.normalized : lane.Direction;
                // fix the rotation of the towers to face each other, with an additional rotation offset.
                ownTower.rotation = Quaternion.LookRotation(ownFacingDirection, Vector3.up) * rotationOffset;
                // The anchor faces the own player (its forward points at them) with no model offset, so bubbles can use its axes.
                tagAnchor.rotation = Quaternion.LookRotation(-ownFacingDirection, Vector3.up);
                opponentTower.localRotation = rotationOffset;
            }
            else
            {
                tagAnchor.gameObject.SetActive(false);
            }
        }
    }
}
