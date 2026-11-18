using UnityEngine;
using UnityEngine.UI;
using BridgeRush.Gameplay;
using BridgeRush.Match;
using BridgeRush.UI;

namespace BridgeRush.Input
{
    public class SummonCursor : MonoBehaviour
    {
        [SerializeField] private Camera aimCamera;
        [SerializeField] private MatchState matchState;
        [SerializeField] private Canvas canvas;

        [Header("Look")]
        [SerializeField] private float maxAimDistanceMeters = 4f;

        [SerializeField] private float ringRadiusMeters = 0.05f;
        [SerializeField] private float crosshairSizePixels = 72f;
        [SerializeField] private Color validColor = new(0.3f, 1f, 0.4f, 1f);
        [SerializeField] private Color invalidColor = new(1f, 0.3f, 0.25f, 1f);
        [SerializeField] private Color scanColor = new(1f, 0.85f, 0.1f, 1f);
        [SerializeField] private Color scanFullColor = new(1f, 0.25f, 0.1f, 1f);

        private static readonly Color SpellAreaColor = new(0.35f, 0.72f, 1f, 0.9f);
        private static readonly Color FireAreaColor = new(1f, 0.22f, 0.18f, 0.95f);
        private SpriteRenderer summonMarker;
        private SpriteRenderer areaMarker;
        private SpriteRenderer fireMarker;
        private Image crosshair;
        private BubbleCaptureManager bubbles;

        public Vector3 AimedWorldPosition { get; private set; }
        public bool IsAimValid { get; private set; }
        // The crosshair ray hit the floor within reach, whether or not the spot is valid for a summon or a spell. The board engine
        // joins this with a glove gesture and checks the spot itself.
        public bool HasAimHit { get; private set; }

        // Where a spell would land: the same floor ray as the summon aim, but valid anywhere in the arena,
        // not just in your own territory. SpellCaster sets the radius so the marker shows the area it will hit.
        public Vector3 SpellAimPosition { get; private set; }
        public bool IsSpellAimValid { get; private set; }
        public float SpellRadiusMeters => matchState.ScaledLightningRadius;

        private void Awake()
        {
            if (aimCamera == null) aimCamera = Camera.main;

            summonMarker = ReticleArt.CreateGroundMarker("SummonMarker", ReticleArt.Summon, transform);
            areaMarker = ReticleArt.CreateGroundMarker("SpellAreaMarker", ReticleArt.Area, transform);
            fireMarker = ReticleArt.CreateGroundMarker("FireballAreaMarker", ReticleArt.Dotted, transform);
            BuildCrosshair();
            SetVisible(false);
        }

        private void Update()
        {
            bool playing = matchState.MatchStarted;
            if (!playing)
            {
                HasAimHit = false;
                IsAimValid = false;
                IsSpellAimValid = false;
                SetVisible(false);
                return;
            }

            // The screen crosshair stays up for the whole match. Only the ground markers need a floor hit, and the ray misses the
            // floor when the phone looks at or above the horizon, or past the aim distance.
            if (!Aim())
            {
                SetVisible(false);
                ShowCrosshair(invalidColor);
                return;
            }
            UpdateCursor();
        }

        // look to aim mechanic , raycast from the center of the screen to find a point on the ground,
        //  check if it is inside the player's territory
        private bool Aim()
        {
            HasAimHit = false;
            IsAimValid = false;
            IsSpellAimValid = false;

            float groundY = matchState.GroundY;
            var floor = new Plane(Vector3.up, new Vector3(0f, groundY, 0f));
            var ray = aimCamera.ViewportPointToRay(new Vector3(0.5f, 0.5f, 0f));
            if (!floor.Raycast(ray, out float distance)) return false;

            Vector3 hit = ray.GetPoint(distance);
            if (Vector3.Distance(aimCamera.transform.position, hit) > maxAimDistanceMeters * matchState.ArenaScale) return false;

            AimedWorldPosition = hit;
            SpellAimPosition = hit;
            HasAimHit = true;
            IsSpellAimValid = matchState.IsInsideSpellArea(hit);
            bool inTerritory = matchState.IsInsideOwnTerritory(hit);
            IsAimValid = inTerritory;
            diagnostic = $"aim {Vector3.Distance(aimCamera.transform.position, hit):0.00} m inTerritory={inTerritory}";
            return true;
        }

        // Aim readout, switched on from the debug menu (Tracking category).
        public static bool ShowDiagnostic { get; set; }

        private string diagnostic = "";
        private void OnGUI()
        {
            if (ShowDiagnostic && !string.IsNullOrEmpty(diagnostic)) GUI.Label(new Rect(10f, Screen.height * 0.35f, Screen.width - 20f, 40f), diagnostic);
        }

        // Green where you can summon, red where you can't. The yellow area shows what a Lightning would hit.
        private void UpdateCursor()
        {
            SetVisible(true);
            Color color = IsAimValid ? validColor : invalidColor;
            ShowCrosshair(color);

            float pulse = Mathf.Sin(Time.time * 4f) * 0.5f + 0.5f;
            float groundY = matchState.GroundY + 0.012f;
            ReticleArt.Place(summonMarker, AimedWorldPosition, groundY, ringRadiusMeters * matchState.ArenaScale * (1f + 0.05f * pulse),
                new Color(color.r, color.g, color.b, 0.75f + 0.25f * pulse));

            areaMarker.enabled = IsSpellAimValid;
            fireMarker.enabled = IsSpellAimValid;
            if (IsSpellAimValid)
            {
                ReticleArt.Place(fireMarker, SpellAimPosition, groundY - 0.001f, matchState.ScaledFireballRadius,
                    new Color(FireAreaColor.r, FireAreaColor.g, FireAreaColor.b, 0.8f + 0.2f * pulse));
                ReticleArt.Spin(fireMarker, 24f * Time.deltaTime);
                ReticleArt.Place(areaMarker, SpellAimPosition, groundY - 0.002f, SpellRadiusMeters,
                    new Color(SpellAreaColor.r, SpellAreaColor.g, SpellAreaColor.b, 0.6f + 0.15f * pulse));
            }
        }

        // The crosshair turns yellow while it is scanning a bubble and shifts to red as the bubble fills, whatever the floor aim says.
        private void ShowCrosshair(Color aimColor)
        {
            if (crosshair == null) return;
            crosshair.gameObject.SetActive(true);
            if (bubbles == null) bubbles = FindFirstObjectByType<BubbleCaptureManager>();
            bool scanning = bubbles != null && bubbles.IsTargeting;
            crosshair.color = scanning ? Color.Lerp(scanColor, scanFullColor, bubbles.LockProgress) : aimColor;
        }

        private void SetVisible(bool visible)
        {
            if (crosshair != null) crosshair.gameObject.SetActive(visible);
            summonMarker.enabled = visible;
            if (!visible)
            {
                areaMarker.enabled = false;
                fireMarker.enabled = false;
            }
        }

        private void BuildCrosshair()
        {
            if (canvas == null) return;

            var go = UIFactory.CreateRect("Crosshair", canvas.transform);
            crosshair = go.AddComponent<Image>();
            crosshair.sprite = ReticleArt.Screen;
            crosshair.color = validColor;
            crosshair.raycastTarget = false;
            crosshair.rectTransform.sizeDelta = new Vector2(crosshairSizePixels, crosshairSizePixels);
        }
    }
}
