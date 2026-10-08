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
        [SerializeField] private float lightningBurstScale = 1.05f;
        [SerializeField] private float overchargeSeconds = 8f;
        [SerializeField] private float lightningStrikeManaCost = 4f;
        [SerializeField] private float lightningStrikeDamage = 180f;
        [SerializeField] private float overchargeManaCost = 5f;

        public float LightningStrikeManaCost => lightningStrikeManaCost;
        public float OverchargeManaCost => overchargeManaCost;

        private void Awake()
        {
            if (arenaFrame == null || matchState == null || gameStateRenderer == null)
            {
                Debug.LogError($"{nameof(SpellCaster)}: needs an Arena Frame, a Match State and a Game State Renderer.", this);
                enabled = false;
            }
        }

        public bool CastLightningStrike()
        {
            if (!arenaFrame.HasLane) return false;
            if (!matchState.TrySpendMana(lightningStrikeManaCost)) return false;

            if (matchState.Role == MatchState.NetworkRole.Client)
            {
                matchState.RequestRemoteSpell("lightning");
            }
            else
            {
                matchState.DamageOpponentTower(lightningStrikeDamage);
            }

            var position = arenaFrame.WorldPositionFromNormalized(0.5f);
            StartCoroutine(BurstEffect(position));
            return true;
        }

        public bool CastOvercharge()
        {
            if (!matchState.TrySpendMana(overchargeManaCost)) return false;

            if (matchState.Role == MatchState.NetworkRole.Client)
            {
                matchState.RequestRemoteSpell("overcharge");
            }

            gameStateRenderer.ApplyOverchargeAura(overchargeSeconds);
            return true;
        }

        private IEnumerator BurstEffect(Vector3 position)
        {
            var burst = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            burst.name = "LightningStrikeBurst";
            Destroy(burst.GetComponent<Collider>());
            burst.transform.position = position;
            burst.transform.localScale = Vector3.zero;
            burst.GetComponent<Renderer>().material.color = Color.yellow;

            float elapsed = 0f;
            while (elapsed < lightningBurstSeconds)
            {
                elapsed += Time.deltaTime;
                float t = elapsed / lightningBurstSeconds;
                burst.transform.localScale = Vector3.one * Mathf.Lerp(0f, lightningBurstScale, t);
                yield return null;
            }

            Destroy(burst);
        }
    }
}
