using System.Collections;
using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Gameplay
{
    public class UnitView : MonoBehaviour
    {
        [SerializeField] private string walkStateName = "02-SRB_BOT_BONE_001_SRB_WALK_ 0";
        [SerializeField] private string attackStateName = "07-SRB_BOT_BONE_001_SRB_ATTACK_001";
        [SerializeField] private string dieStateName = "12-SRB_BOT_BONE_001_SRB_DIE";
        [SerializeField] private float dieAnimationSeconds = 1f;
        [SerializeField] private float spawnInSeconds = 0.4f;

        private Animator animator;
        private UnitCombatState? currentCombatState;

        private void Awake()
        {
            animator = GetComponentInChildren<Animator>();

            if (animator != null)
            {
                for (int i = 1; i < animator.layerCount; i++)
                {
                    animator.SetLayerWeight(i, 0f);
                }
            }
        }
        //esnure the unit is in the correct animation state based on its combat state (attacking or walking).
        public void SetCombatState(UnitCombatState combatState)
        {
            if (currentCombatState == combatState) return;
            currentCombatState = combatState;
            if (animator == null) return;
            string stateName = combatState == UnitCombatState.Attacking ? attackStateName : walkStateName;
            if (!string.IsNullOrEmpty(stateName))
            {
                animator.Play(stateName, 0, 0f);
                animator.Update(0f);
            }
        }
        // Grows the unit from nothing to its current scale with a small overshoot, so it appears out of the dust.
        public void PlaySpawnIn()
        {
            StartCoroutine(SpawnInCoroutine());
        }

        private IEnumerator SpawnInCoroutine()
        {
            var target = transform.localScale;
            transform.localScale = Vector3.zero;
            for (float t = 0f; t < spawnInSeconds; t += Time.deltaTime)
            {
                float u = t / spawnInSeconds - 1f;
                float eased = 1f + 2.70158f * u * u * u + 1.70158f * u * u;
                transform.localScale = target * eased;
                yield return null;
            }
            transform.localScale = target;
        }

        // ensure that it plays the die animation and then destroys the unit after the die animation duration.
        public void PlayDieAndDestroy()
        {
            if (animator != null && !string.IsNullOrEmpty(dieStateName))
            {
                animator.Play(dieStateName, 0, 0f);
                animator.Update(0f);
            }
            Destroy(gameObject, dieAnimationSeconds);
        }
        // setting the color for enemy unit to be red
        public void ApplyTint(Color tint)
        {
            foreach (var renderer in GetComponentsInChildren<Renderer>())
            {
                renderer.material.color = tint;
            }
        }
    }
}
