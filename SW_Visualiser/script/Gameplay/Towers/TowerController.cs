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

        // How far in front of the player the own tower stands, in tower footprint radii. 0 = on the tag itself (under the phone).
        // Deliberately NOT a serialized field: the saved scene held 1.5 and kept overriding the code, which put the drawn tower
        // 1.5 radii ahead of where the engine fights over it, so enemy units stopped inside it. Keep engine/rules.json
        // TowerController.ownTowerForwardOffsetInRadii equal. Change it here, in code.
        private float ownTowerForwardOffsetInRadii = 0f;

        [Tooltip("Draw the towers over real-world objects instead of letting AR occlusion hide them behind chairs, walls or people. Units, bubbles and the arena are still occluded.")]
        [SerializeField] private bool towerIgnoresArOcclusion = true;

        [Tooltip("Your own tower is hidden from your own screen (it blocks your view): you only see its ring on the floor until you lock it with a shake, then it appears in a puff of smoke. The other player always sees it. Untick to always show it.")]
        [SerializeField] private bool hideOwnTowerUntilLocked = true;

        [Tooltip("The tallest a tower may stand in the real world, in metres, after the arena scaling. The scaling shrinks nothing here: a small room keeps its small towers, a big one stops growing at this height.")]
        [SerializeField] private float maxTowerHeightMeters = 1.4f;

        // Larger of the two footprints, so units stand off from either tower without overlapping it.
        public float TowerRadius => Mathf.Max(ownFootprintRadius, opponentFootprintRadius) * TowerScale;
        // How far in front of the player the own tower is drawn, in metres. Being out of the arena is judged where the tower
        // stands (MatchState.OwnTowerBodyPosition), so a tower is only "out" once its centre is past the edge: more than half
        // of it outside.
        public float OwnTowerForwardOffsetMeters => ownFootprintRadius * TowerScale * ownTowerForwardOffsetInRadii;
        public float OpponentTowerHeightMeters => towerHeight * towerHeightStretch * TowerScale;
        private float ArenaScale => matchState != null ? matchState.ArenaScale : 1f;
        // The scale the towers are drawn at: the arena scale, but never so large that a tower is taller than maxTowerHeightMeters.
        // The footprint, the bubbles and the spawn offsets use it too, so everything stays in proportion to the drawn tower.
        private float TowerScale
        {
            get
            {
                float baseHeight = Mathf.Max(0.0001f, towerHeight * towerHeightStretch);
                float cap = maxTowerHeightMeters > 0f ? maxTowerHeightMeters / baseHeight : float.MaxValue;
                return Mathf.Min(ArenaScale, cap);
            }
        }
        public float TagToGroundOffsetMeters => tagToGroundOffsetMeters;
        // Follows the opponent's tag. The opponent tower and the capture bubbles are children of it. Null until Start.
        public Transform TagAnchor => tagAnchor;

        private Transform ownTower;
        // The own tower's model (everything but its ring), so it can be hidden while the ring stays; scaled for the grow-in.
        private Transform ownTowerModel;
        private ParticleSystem ownSmoke;
        private bool ownShown = true;
        private float ownShownAt = -10f;
        private const float AppearSeconds = 0.55f;
        private Transform opponentTower;
        private Transform tagAnchor;
        private float ownFootprintRadius;
        private float opponentFootprintRadius;
        private Vector3 reportedOpponentBase;
        private bool hasReportedOpponent;

        // How fast the opponent tower follows its reported position (about ten reports a second), per second.
        private const float ReportedFollowRate = 12f;

        private void Start()
        {
            ownTower = CreateTower("OwnTower", ownTowerColor, ownTowerPrefabOverride,
                towerHeight * towerHeightStretch, towerWidthMultiplier / towerHeightStretch, out ownFootprintRadius);

            // Put the model under a pivot at the tower's base, so it can grow in from the floor without moving.
            ownTowerModel = new GameObject("OwnTowerModel").transform;
            ownTowerModel.SetParent(ownTower, false);
            ownTower.GetChild(0).SetParent(ownTowerModel, true);
            ownSmoke = BuildSmoke(ownTower, ownFootprintRadius);
            ownShown = !hideOwnTowerUntilLocked;
            ownTowerModel.gameObject.SetActive(ownShown);

            // The anchor sits at the tag; the tower hangs below it by the tag-to-ground offset.
            tagAnchor = new GameObject("OpponentTagAnchor").transform;
            tagAnchor.SetParent(transform, false);
            opponentTower = CreateTower("OpponentTower", opponentTowerColor, opponentTowerPrefabOverride,
                towerHeight * towerHeightStretch, towerWidthMultiplier / towerHeightStretch, out opponentFootprintRadius);
            opponentTower.SetParent(tagAnchor, false);
            opponentTower.localPosition = new Vector3(0f, -tagToGroundOffsetMeters, 0f);
            tagAnchor.gameObject.SetActive(false);
            Debug.Log($"[TowerRadius] own {ownFootprintRadius:F4} m, opponent {opponentFootprintRadius:F4} m: put {Mathf.Max(ownFootprintRadius, opponentFootprintRadius):F4} in engine/rules.json TowerController.TowerRadius");
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
            if (towerIgnoresArOcclusion) ArDepthClear.AddCopies(instance);
            CreateBaseRing(root.transform, color, footprintRadius);

            return root.transform;
        }

        // Your own tower blocks your own view, so it is hidden (its floor ring stays) until you lock it. Locking shows it in a puff of
        // smoke, with a short grow-in; unlocking puffs it away again. Visual only: the engine and the other player are unaffected.
        private void UpdateOwnTowerVisibility()
        {
            bool show = !hideOwnTowerUntilLocked || matchState.OwnTowerLocked;
            if (show != ownShown)
            {
                ownShown = show;
                ownShownAt = Time.time;
                ownTowerModel.gameObject.SetActive(show);
                if (ownSmoke != null)
                {
                    ownSmoke.Clear();
                    ownSmoke.Play();
                }
            }

            if (!ownShown) return;
            float t = Mathf.Clamp01((Time.time - ownShownAt) / AppearSeconds);
            // Ease out with a little overshoot, rising from the floor and widening slightly.
            float c = 1.70158f;
            float back = 1f + (c + 1f) * Mathf.Pow(t - 1f, 3f) + c * Mathf.Pow(t - 1f, 2f);
            float rise = Mathf.Max(0.0001f, back);
            float widen = Mathf.Lerp(0.6f, 1f, Mathf.Clamp01(back));
            ownTowerModel.localScale = new Vector3(widen, rise, widen);
        }

        private static Texture2D softPuffTexture;

        // A one-shot puff of grey smoke around the base: soft round sprites that rise, swell and fade. Built in code so no asset
        // is needed. Local space, so it scales and moves with the tower.
        private static ParticleSystem BuildSmoke(Transform parent, float baseRadius)
        {
            if (softPuffTexture == null)
            {
                const int size = 64;
                softPuffTexture = new Texture2D(size, size, TextureFormat.RGBA32, false) { wrapMode = TextureWrapMode.Clamp, name = "SoftPuff" };
                for (int y = 0; y < size; y++)
                {
                    for (int x = 0; x < size; x++)
                    {
                        float dx = (x + 0.5f) / size * 2f - 1f, dy = (y + 0.5f) / size * 2f - 1f;
                        float d = Mathf.Sqrt(dx * dx + dy * dy);
                        float a = Mathf.Clamp01(1f - d);
                        a = a * a * (3f - 2f * a); // smoothstep: soft edge, denser middle
                        softPuffTexture.SetPixel(x, y, new Color(1f, 1f, 1f, a));
                    }
                }
                softPuffTexture.Apply();
            }

            var go = new GameObject("TowerSmoke");
            go.transform.SetParent(parent, false);
            go.transform.localRotation = Quaternion.Euler(-90f, 0f, 0f); // the shape's disc lies on the floor, cone axis up
            var ps = go.AddComponent<ParticleSystem>();
            ps.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);

            var main = ps.main;
            main.playOnAwake = false;
            main.loop = false;
            main.duration = 1f;
            main.simulationSpace = ParticleSystemSimulationSpace.Local;
            main.scalingMode = ParticleSystemScalingMode.Hierarchy;
            main.startLifetime = new ParticleSystem.MinMaxCurve(1.0f, 1.6f);
            main.startSpeed = new ParticleSystem.MinMaxCurve(baseRadius * 0.4f, baseRadius * 1.2f);
            main.startSize = new ParticleSystem.MinMaxCurve(baseRadius * 1.2f, baseRadius * 2.2f);
            main.startRotation = new ParticleSystem.MinMaxCurve(0f, Mathf.PI * 2f);
            main.startColor = new ParticleSystem.MinMaxGradient(new Color(0.92f, 0.92f, 0.94f, 0.85f), new Color(0.62f, 0.64f, 0.68f, 0.85f));
            main.gravityModifier = -0.04f; // drifts up a little
            main.maxParticles = 60;

            var emission = ps.emission;
            emission.rateOverTime = 0f;
            emission.SetBursts(new[] { new ParticleSystem.Burst(0f, 36) });

            var shape = ps.shape;
            shape.enabled = true;
            shape.shapeType = ParticleSystemShapeType.Cone;
            shape.angle = 28f;
            shape.radius = baseRadius * 1.1f;
            shape.radiusThickness = 1f;

            var growth = ps.sizeOverLifetime;
            growth.enabled = true;
            growth.size = new ParticleSystem.MinMaxCurve(1f, new AnimationCurve(new Keyframe(0f, 0.45f), new Keyframe(1f, 1.5f)));

            var rotation = ps.rotationOverLifetime;
            rotation.enabled = true;
            rotation.z = new ParticleSystem.MinMaxCurve(-0.7f, 0.7f);

            var colour = ps.colorOverLifetime;
            colour.enabled = true;
            var fade = new Gradient();
            fade.SetKeys(
                new[] { new GradientColorKey(Color.white, 0f), new GradientColorKey(Color.white, 1f) },
                new[] { new GradientAlphaKey(0f, 0f), new GradientAlphaKey(1f, 0.12f), new GradientAlphaKey(0.55f, 0.6f), new GradientAlphaKey(0f, 1f) });
            colour.color = fade;

            var renderer = go.GetComponent<ParticleSystemRenderer>();
            renderer.renderMode = ParticleSystemRenderMode.Billboard;
            renderer.sortingOrder = 5;
            var material = new Material(Shader.Find("Sprites/Default")) { name = "TowerSmoke", mainTexture = softPuffTexture };
            renderer.sharedMaterial = material;
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;
            return ps;
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
        // In the board role the opponent tower stands where that player reports it to the board, which is the spot the engine
        // fights over, not where this camera last saw their tag (so it moves and locks even when the tag is out of view).
        // It is drawn the way that player's own phone draws their tower: their device, then a step towards the other player.
        // Before the match, or before the first report of a match, it stays on the tag. Once a match is running and the tower
        // has been placed from a report, a gap in the reports (the other phone's link dropping, or the 2 s freshness window
        // running out) holds the tower where it was instead of snapping to the live tag, which jumps whenever the tag is
        // lost and found again. When reports resume it glides to the new spot rather than jumping.
        private Vector3 OpponentBase(Vector3 tagPosition, Vector3 ownBase, float arenaScale)
        {
            if (!matchState.MatchStarted) hasReportedOpponent = false;

            if (!matchState.TryGetOpponentTowerReported(out var reported, out _))
            {
                if (matchState.MatchStarted && hasReportedOpponent) return reportedOpponentBase;
                hasReportedOpponent = false;
                return tagPosition;
            }

            var toOwn = new Vector3(ownBase.x - reported.x, 0f, ownBase.z - reported.z);
            toOwn = toOwn.sqrMagnitude > 0.0001f ? toOwn.normalized : -arenaFrame.Lane.Direction;
            var target = new Vector3(reported.x, 0f, reported.z)
                + toOwn * (opponentFootprintRadius * arenaScale * ownTowerForwardOffsetInRadii);

            if (!hasReportedOpponent) reportedOpponentBase = target;
            else reportedOpponentBase = Vector3.Lerp(reportedOpponentBase, target, 1f - Mathf.Exp(-ReportedFollowRate * Time.deltaTime));
            hasReportedOpponent = true;
            return reportedOpponentBase;
        }

        // constantly update positon, rotation, and visibility of the towers based on the current state of the match and the arena frame.
        private void Update()
        {
            float arenaScale = TowerScale; // named for what it feeds below: the footprint offsets of the towers
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
            ownTower.position = ownBase + forward * OwnTowerForwardOffsetMeters;

            UpdateOwnTowerVisibility();

            if (arenaFrame.HasLane)
            {
                var lane = arenaFrame.Lane;
                // place the opponent tower at the end of the lane, and rotate both towers to face each other.
                tagAnchor.gameObject.SetActive(true);
                var pos = OpponentBase(lane.Origin + lane.Direction * lane.Length, ownBase, arenaScale);
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
