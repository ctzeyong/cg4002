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
        [SerializeField] private Color overchargeTintColor = Color.yellow;

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
        // plays an overcharge aura effect on the unit for a specified duration, changing its color to indicate the effect.
        public void PlayOverchargeAura(float durationSeconds)
        {
            StartCoroutine(OverchargeCoroutine(durationSeconds));
        }
        // Coroutine that handles the overcharge effect, changing the unit's color and reverting it back after the duration.
        private IEnumerator OverchargeCoroutine(float durationSeconds)
        {
            var renderers = GetComponentsInChildren<Renderer>();
            var originalColors = new Color[renderers.Length];
            for (int i = 0; i < renderers.Length; i++)
            {
                originalColors[i] = renderers[i].material.color;
                renderers[i].material.color = Color.Lerp(originalColors[i], overchargeTintColor, 0.6f);
            }
            yield return new WaitForSeconds(durationSeconds);

            for (int i = 0; i < renderers.Length; i++)
            {
                if (renderers[i] != null) renderers[i].material.color = originalColors[i];
            }
        }
    }
}
