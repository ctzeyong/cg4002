using System.Collections;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Gameplay
{
    public class SpellCaster : MonoBehaviour
    {
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private GameStateRenderer gameStateRenderer;
        [SerializeField] private MatchState matchState;
        [SerializeField] private float lightningBurstSeconds = 0.4f;
        [SerializeField] private float lightningStrikeManaCost = 5f;
        [SerializeField] private float lightningStrikeDamage = 180f;

        [Header("Lightning area")]
        [SerializeField] private float lightningRadiusMeters = 0.2f;
        [SerializeField] private float lightningTowerDamage = 60f;
        [Range(0f, 1f)]
        [SerializeField] private float lightningEdgeDamageFraction = 0.5f;
        [SerializeField] private float lightningTelegraphSeconds = 0.5f;

        [Header("Fireball")]
        [SerializeField] private float fireballManaCost = 6f;
        [SerializeField] private float fireballUnitDamage = 120f;
        [SerializeField] private float fireballTowerDamage = 40f;
        [SerializeField] private float fireballRadiusMeters = 0.12f;
        [Range(0f, 1f)]
        [SerializeField] private float fireballEdgeDamageFraction = 0.5f;
        [Tooltip("How fast the ball flies, in metres per second at a 2 m lane (scaled with the arena).")]
        [SerializeField] private float fireballSpeedMetersPerSecond = 1.2f;
        [SerializeField] private float fireballMinFlightSeconds = 0.4f;
        [Tooltip("How long the floor keeps burning after the impact, and the damage per second to enemy units standing in it.")]
        [SerializeField] private float fireballBurnSeconds = 4f;
        [SerializeField] private float fireballBurnDps = 12f;

        [Header("Sound (optional)")]
        [Tooltip("Leave empty to use the built-in synthesised sound.")]
        [SerializeField] private AudioClip lightningChargeClip;
        [SerializeField] private AudioClip lightningCrackClip;
        [SerializeField] private AudioClip fireballThrowClip;
        [SerializeField] private AudioClip fireballBoomClip;

        public float LightningStrikeManaCost => lightningStrikeManaCost;
        public float FireballManaCost => fireballManaCost;
        public float LightningStrikeDamage => lightningStrikeDamage;
        public float FireballUnitDamage => fireballUnitDamage;
        public float LightningRadiusMeters => lightningRadiusMeters;

        private static readonly Color OwnStrikeColor = new(0.4f, 0.78f, 1f, 1f);
        private static readonly Color EnemyStrikeColor = new(0.5f, 0.6f, 1f, 1f);
        private static readonly Color OwnFireColor = new(1f, 0.3f, 0.2f, 1f);
        private static readonly Color EnemyFireColor = new(0.95f, 0.15f, 0.15f, 1f);
        private Material lineMaterial;

        private void Awake()
        {
            if (arenaFrame == null || matchState == null || gameStateRenderer == null)
            {
                Debug.LogError($"{nameof(SpellCaster)}: needs an Arena Frame, a Match State and a Game State Renderer.", this);
                enabled = false;
                return;
            }

            matchState.RegisterSpellSpec(new MatchState.SpellSpec
            {
                LightningManaCost = lightningStrikeManaCost,
                LightningUnitDamage = lightningStrikeDamage,
                LightningTowerDamage = lightningTowerDamage,
                LightningRadiusMeters = lightningRadiusMeters,
                LightningEdgeDamageFraction = lightningEdgeDamageFraction,
                LightningTelegraphSeconds = lightningTelegraphSeconds,

                FireballManaCost = fireballManaCost,
                FireballUnitDamage = fireballUnitDamage,
                FireballTowerDamage = fireballTowerDamage,
                FireballRadiusMeters = fireballRadiusMeters,
                FireballEdgeDamageFraction = fireballEdgeDamageFraction,
                FireballSpeedMetersPerSecond = fireballSpeedMetersPerSecond,
                FireballMinFlightSeconds = fireballMinFlightSeconds,
                FireballBurnSeconds = fireballBurnSeconds,
                FireballBurnDps = fireballBurnDps
            });
        }

        private void OnEnable()
        {
            if (matchState == null) return;
            matchState.LightningStrikeStarted += OnLightningStrikeStarted;
            matchState.FireballStarted += OnFireballStarted;
            matchState.BoardSpellCast += OnBoardSpellCast;
            matchState.BoardSpellRejected += OnBoardSpellRejected;
        }

        private void OnDisable()
        {
            if (matchState == null) return;
            matchState.LightningStrikeStarted -= OnLightningStrikeStarted;
            matchState.FireballStarted -= OnFireballStarted;
            matchState.BoardSpellCast -= OnBoardSpellCast;
            matchState.BoardSpellRejected -= OnBoardSpellRejected;
        }

        // The board's answer to a spell: it decided, so these raise the same events the local cast does.
        private void OnBoardSpellCast(string spell) => SpellCast?.Invoke(spell);
        private void OnBoardSpellRejected(string spell, string reason) => SpellRejected?.Invoke(spell, reason);

        // Fired when a spell is accepted: "lightning" or "fireball". Drives the card animation.
        public event System.Action<string> SpellCast;
        // A spell was refused, with a short reason for the card.
        public event System.Action<string, string> SpellRejected;

        // Both spells aim with the same crosshair.
        private bool SpellAimValid =>
            matchState.AimCursor != null && matchState.AimCursor.IsSpellAimValid;

        private float CostOf(string spell) => spell == "fireball" ? fireballManaCost : lightningStrikeManaCost;

        public string ShortCastFailure(string spell)
        {
            if (!arenaFrame.HasLane) return "NO OPPONENT";
            if (!SpellAimValid) return "BAD AIM";
            return matchState.ManaCurrent < CostOf(spell) ? "NO MANA" : "CAN'T CAST";
        }

        public string DescribeCastFailure(string spell)
        {
            if (matchState.IsBoardClient) return matchState.BoardRefusalText;
            if (!arenaFrame.HasLane) return "No opponent found yet.";
            if (!SpellAimValid) return "Aim the crosshair at the arena floor, on either side.";
            float cost = CostOf(spell);
            if (matchState.ManaCurrent < cost) return $"Not enough mana: need {cost:0.#}, you have {matchState.ManaCurrent:0.#}.";
            return "The spell couldn't be cast.";
        }

        public bool CastLightningStrike()
        {
            if (matchState.IsBoardClient) return matchState.BoardSendSpell("lightning");
            bool done = CastLightningCore();
            if (!done) SpellRejected?.Invoke("lightning", ShortCastFailure("lightning"));
            return done;
        }

        // Order matters: the aim is checked before mana is spent, so a miss costs nothing.
        private bool CastLightningCore()
        {
            if (!arenaFrame.HasLane) return false;
            if (!SpellAimValid) return false;

            Vector3 target = matchState.AimCursor.SpellAimPosition;
            if (!matchState.TrySpendMana(lightningStrikeManaCost)) return false;

            if (matchState.Role == MatchState.NetworkRole.Client)
            {
                matchState.RequestRemoteLightning(target);
            }
            else
            {
                matchState.QueueLightning(UnitSide.Own, target);
            }

            SpellCast?.Invoke("lightning");
            return true;
        }

        public bool CastFireball()
        {
            if (matchState.IsBoardClient) return matchState.BoardSendSpell("fireball");
            bool done = CastFireballCore();
            if (!done) SpellRejected?.Invoke("fireball", ShortCastFailure("fireball"));
            return done;
        }

        // Same order as lightning: lane, aim, then mana. The ball always flies to the aim point.
        private bool CastFireballCore()
        {
            if (!arenaFrame.HasLane) return false;
            if (!SpellAimValid) return false;

            Vector3 target = matchState.AimCursor.SpellAimPosition;
            if (!matchState.TrySpendMana(fireballManaCost)) return false;

            if (matchState.Role == MatchState.NetworkRole.Client)
            {
                matchState.RequestRemoteFireball(target);
            }
            else
            {
                matchState.QueueFireball(UnitSide.Own, target);
            }

            SpellCast?.Invoke("fireball");
            return true;
        }

        private void OnLightningStrikeStarted(Vector3 center, float telegraphSeconds, bool byLocalPlayer)
        {
            GameAudio.PlayLightningCharge(lightningChargeClip);
            StartCoroutine(StrikeEffect(center, telegraphSeconds, byLocalPlayer ? OwnStrikeColor : EnemyStrikeColor));
        }

        // A marked area on the floor with a ring closing in on its edge for the telegraph, then a bolt, a shockwave and a flash.
        private IEnumerator StrikeEffect(Vector3 center, float telegraphSeconds, Color color)
        {
            float radius = matchState.ScaledLightningRadius;
            float groundY = matchState.GroundY + 0.012f;

            var area = ReticleArt.CreateGroundMarker("LightningArea", ReticleArt.Area);
            var closing = ReticleArt.CreateGroundMarker("LightningClosingRing", ReticleArt.Ring);
            ReticleArt.Place(area, center, groundY, radius, color);

            float elapsed = 0f;
            while (elapsed < telegraphSeconds)
            {
                elapsed += Time.deltaTime;
                float t = telegraphSeconds > 0.0001f ? Mathf.Clamp01(elapsed / telegraphSeconds) : 1f;
                float eased = t * t;
                float pulse = 0.65f + 0.35f * Mathf.Sin(Time.time * 18f);
                ReticleArt.Place(area, center, groundY, radius, new Color(color.r, color.g, color.b, pulse));
                ReticleArt.Place(closing, center, groundY + 0.001f, Mathf.Lerp(radius * 1.6f, radius, eased), new Color(1f, 1f, 1f, 0.35f + 0.65f * t));
                yield return null;
            }

            Destroy(closing.gameObject);
            GameAudio.PlayLightningCrack(lightningCrackClip);
            StartCoroutine(ImpactEffect(area, center, groundY, radius, color));

            var bolt = BuildBolt(center, groundY, color);
            const float boltSeconds = 0.18f;
            for (float t = 0f; t < boltSeconds; t += Time.deltaTime)
            {
                float fade = 1f - t / boltSeconds;
                bolt.core.widthMultiplier = 0.022f * fade;
                bolt.glow.widthMultiplier = 0.07f * fade;
                yield return null;
            }
            Destroy(bolt.root);
        }

        // The area flashes white and fades while a shockwave ring pushes out past its edge.
        private IEnumerator ImpactEffect(SpriteRenderer area, Vector3 center, float groundY, float radius, Color color)
        {
            var shock = ReticleArt.CreateGroundMarker("LightningShockwave", ReticleArt.Ring);
            for (float t = 0f; t < lightningBurstSeconds; t += Time.deltaTime)
            {
                float u = t / lightningBurstSeconds;
                float fade = 1f - u;
                var flash = Color.Lerp(Color.white, color, u);
                ReticleArt.Place(area, center, groundY, radius, new Color(flash.r, flash.g, flash.b, fade));
                ReticleArt.Place(shock, center, groundY + 0.001f, Mathf.Lerp(radius * 0.6f, radius * 1.35f, 1f - fade * fade),
                    new Color(1f, 1f, 1f, fade));
                yield return null;
            }
            Destroy(area.gameObject);
            Destroy(shock.gameObject);
        }

        private void OnFireballStarted(Vector3 from, Vector3 to, float flightSeconds, float burnSeconds, bool byLocalPlayer)
        {
            GameAudio.PlayFireballThrow(fireballThrowClip);
            StartCoroutine(FireballEffect(from, to, flightSeconds, burnSeconds, byLocalPlayer ? OwnFireColor : EnemyFireColor));
        }

        // The target is marked on the floor while the ball flies along a low arc, then the impact flashes, a ring pushes out,
        // and the floor keeps burning (a glowing patch with rising embers) for the burn time before it fades.
        private IEnumerator FireballEffect(Vector3 from, Vector3 to, float flightSeconds, float burnSeconds, Color color)
        {
            float scale = matchState.ArenaScale;
            float radius = matchState.ScaledFireballRadius;
            float groundY = matchState.GroundY + 0.012f;

            // A red dotted ring marks the target for the whole flight and burn.
            var marker = ReticleArt.CreateGroundMarker("FireballTarget", ReticleArt.Dotted);
            ReticleArt.Place(marker, to, groundY, radius, color);

            float ballSize = Mathf.Max(0.025f, radius * 0.55f);
            var ball = FireEffects.CreateBall(ballSize, color);
            float startHeight = 0.2f * scale;
            float arcHeight = Mathf.Min(0.25f * scale, Vector3.Distance(from, to) * 0.25f);
            ball.transform.position = new Vector3(from.x, groundY + startHeight, from.z);

            float elapsed = 0f;
            while (elapsed < flightSeconds)
            {
                elapsed += Time.deltaTime;
                float t = flightSeconds > 0.0001f ? Mathf.Clamp01(elapsed / flightSeconds) : 1f;
                float eased = t * t * (1.4f - 0.4f * t);
                Vector3 flat = Vector3.Lerp(from, to, eased);
                float height = Mathf.Lerp(groundY + startHeight, groundY, eased) + Mathf.Sin(eased * Mathf.PI) * arcHeight;
                ball.transform.position = new Vector3(flat.x, height, flat.z);
                float pulse = 0.75f + 0.25f * Mathf.Sin(Time.time * 16f);
                ReticleArt.Place(marker, to, groundY, radius, new Color(color.r, color.g, color.b, pulse));
                ReticleArt.Spin(marker, 40f * Time.deltaTime);
                yield return null;
            }

            Destroy(ball, 0.35f);
            ball.GetComponent<MeshRenderer>().enabled = false;

            GameAudio.PlayFireballBoom(fireballBoomClip);
            StartCoroutine(FireballImpact(to, groundY, radius, color));

            var embers = FireEffects.CreateEmbers(to, radius, scale, color);
            for (float t = 0f; t < burnSeconds; t += Time.deltaTime)
            {
                float fadeOut = Mathf.Clamp01((burnSeconds - t) / 0.6f);
                float flicker = 0.4f + 0.2f * Mathf.Sin(Time.time * 11f) + 0.1f * Mathf.Sin(Time.time * 27f);
                ReticleArt.Place(marker, to, groundY, radius, new Color(color.r, color.g, color.b, (0.55f + flicker * 0.8f) * fadeOut));
                ReticleArt.Spin(marker, 18f * Time.deltaTime);
                yield return null;
            }
            if (embers != null)
            {
                embers.Stop(true, ParticleSystemStopBehavior.StopEmitting);
                Destroy(embers.gameObject, 2f);
            }
            Destroy(marker.gameObject);
        }

        private IEnumerator FireballImpact(Vector3 center, float groundY, float radius, Color color)
        {
            var shock = ReticleArt.CreateGroundMarker("FireballShockwave", ReticleArt.Ring);
            var flash = ReticleArt.CreateGroundMarker("FireballFlash", ReticleArt.Area);
            const float seconds = 0.35f;
            for (float t = 0f; t < seconds; t += Time.deltaTime)
            {
                float u = t / seconds;
                float fade = 1f - u;
                ReticleArt.Place(flash, center, groundY + 0.002f, radius * (0.8f + 0.5f * u), new Color(1f, 0.95f, 0.7f, fade));
                ReticleArt.Place(shock, center, groundY + 0.003f, Mathf.Lerp(radius * 0.5f, radius * 1.8f, 1f - fade * fade),
                    new Color(color.r, color.g, color.b, fade));
                yield return null;
            }
            Destroy(shock.gameObject);
            Destroy(flash.gameObject);
        }

        private Material LineMaterial
        {
            get
            {
                if (lineMaterial == null) lineMaterial = new Material(Shader.Find("Sprites/Default"));
                return lineMaterial;
            }
        }

        // A white core over a wider, softer glow in the strike colour.
        private (GameObject root, LineRenderer core, LineRenderer glow) BuildBolt(Vector3 center, float groundY, Color color)
        {
            const int points = 9;
            float top = groundY + 1.2f * matchState.ArenaScale;
            var root = new GameObject("LightningBolt");

            var positions = new Vector3[points];
            for (int i = 0; i < points; i++)
            {
                float t = i / (float)(points - 1);
                float jitter = (i == 0 || i == points - 1) ? 0f : Random.Range(-0.05f, 0.05f);
                positions[i] = new Vector3(center.x + jitter, Mathf.Lerp(top, groundY, t), center.z + Random.Range(-0.03f, 0.03f) * (i == 0 || i == points - 1 ? 0f : 1f));
            }

            var glow = AddBoltLine(root, "Glow", positions, new Color(color.r, color.g, color.b, 0.45f), 0.07f);
            var core = AddBoltLine(root, "Core", positions, Color.white, 0.022f);
            return (root, core, glow);
        }

        private LineRenderer AddBoltLine(GameObject root, string objectName, Vector3[] positions, Color color, float width)
        {
            var go = new GameObject(objectName);
            go.transform.SetParent(root.transform, false);
            var line = go.AddComponent<LineRenderer>();
            line.useWorldSpace = true;
            line.positionCount = positions.Length;
            line.SetPositions(positions);
            line.widthMultiplier = width;
            line.material = LineMaterial;
            line.numCapVertices = 4;
            line.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            line.receiveShadows = false;
            line.startColor = line.endColor = color;
            return line;
        }
    }
}
