using System;
using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Gameplay
{
    // Capture bubbles on the opponent's tower. The crosshair (camera centre) fills the bubble it points at; looking away
    // drains it slowly after a short grace window. A full bubble fires OnBubbleCaptured, goes on cooldown, then respawns
    // at a different spot. Awarding the reward is NOT done here: BubbleSync reports the capture to the Ultra96.
    public class BubbleCaptureManager : MonoBehaviour
    {
        [SerializeField] private Camera aimCamera;
        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchState matchState;
        [SerializeField] private TowerController towerController;

        [Header("Capture")]
        [Tooltip("Seconds of uninterrupted aim to fill a bubble.")]
        [SerializeField] private float holdTimeSeconds = 2.5f;
        [Tooltip("Progress lost per second once the grace window has passed (1 = full bubble). Smaller = slower drain.")]
        [SerializeField] private float drainPerSecond = 0.2f;
        [Tooltip("Aim can leave the bubble this long without the lock starting to drain, so head jitter does not break it.")]
        [SerializeField] private float graceSeconds = 0.3f;
        [Tooltip("Farthest camera-to-bubble distance at which progress counts.")]
        [SerializeField] private float maxLockDistanceMeters = 6f;
        [Tooltip("Widest angle between the tower's facing direction and the direction to you. Stops locking from behind or the side.")]
        [SerializeField] private float maxViewingAngleDegrees = 60f;
        [Tooltip("Seconds a captured bubble stays gone before it reappears somewhere else.")]
        [SerializeField] private float respawnCooldownSeconds = 1f;

        [Header("Reward")]
        [Tooltip("What a capture is worth. Reported/awarded by the Ultra96; kept here as data so a buff type can be added later.")]
        [SerializeField] private BubbleReward reward = new() { Type = BubbleRewardType.Mana, Amount = 1f };

        [Header("Bubbles")]
        private const int bubbleCount = 1;
        [Tooltip("Possible heights as a fraction of tower height (1 = tag-to-ground top of the tower).")]
        [SerializeField] private float[] slotHeightFractions = { 0.2f, 0.4f, 0.6f, 0.8f, 1.0f };
        [Tooltip("Possible positions around the tower's front, in degrees from straight at the player.")]
        [SerializeField] private float[] slotAzimuthDegrees = { -45f, 0f, 45f };
        [Tooltip("Physics layer for the bubbles (create a 'Bubble' layer and put its index here). -1 = leave on Default.")]
        [SerializeField] private int bubbleLayer = -1;

        [Header("Look")]
        [SerializeField] private Color idleColor = new(0.3f, 0.8f, 1f, 0.35f);
        [SerializeField] private Color lockingColor = new(1f, 0.85f, 0.1f, 0.75f);
        [SerializeField] private Color fullColor = new(1f, 0.25f, 0.1f, 0.9f);

        // Fires once per capture with the id of the bubble that was captured.
        public event Action<int> OnBubbleCaptured;

        // True while the crosshair is on a bubble and every gate (tracked tag, distance, angle) passes.
        public bool IsTargeting => target != null;
        // Progress of the bubble that is filling, or the fullest one while draining (0..1).
        public float LockProgress { get; private set; }
        public BubbleReward Reward => reward;

        private readonly List<CaptureBubble> bubbles = new();
        private readonly List<int> slotOfBubble = new();
        private readonly List<float> respawnAt = new();
        private readonly List<float> secondsSinceTargeted = new();
        private Transform container;
        private CaptureBubble target;

        // Sized from the tower so the bubble stays proportionate to it.
        private float bubbleRadiusMeters => towerController.OpponentTowerHeightMeters * 0.11f;
        private float slotRadiusMeters => towerController.OpponentTowerHeightMeters * 0.27f;

        private int SlotCount => Mathf.Max(1, slotHeightFractions.Length) * Mathf.Max(1, slotAzimuthDegrees.Length);

        private void Awake()
        {
            if (aimCamera == null) aimCamera = Camera.main;
            if (matchState == null || arenaFrame == null || towerController == null)
            {
                Debug.LogError($"{nameof(BubbleCaptureManager)}: needs a Match State, an Arena Frame and a Tower Controller.", this);
                enabled = false;
            }
        }

        // Bubbles hang off the tower controller's tag anchor, which exists only after its Start.
        private bool EnsureBuilt()
        {
            if (container != null) return true;
            if (towerController.TagAnchor == null) return false;
            if (aimCamera == null) aimCamera = Camera.main;
            if (aimCamera == null) return false;

            container = new GameObject("CaptureBubbles").transform;
            container.SetParent(towerController.TagAnchor, false);

            for (int i = 0; i < bubbleCount; i++)
            {
                var go = new GameObject($"Bubble{i}");
                go.transform.SetParent(container, false);
                var bubble = go.AddComponent<CaptureBubble>();
                bubble.Build(i, bubbleRadiusMeters, aimCamera, idleColor, lockingColor, fullColor, bubbleLayer);
                bubbles.Add(bubble);
                slotOfBubble.Add(-1);
                respawnAt.Add(0f);
                secondsSinceTargeted.Add(0f);
                PlaceBubble(i);
            }
            return true;
        }

        private void Update()
        {
            if (!EnsureBuilt()) return;

            bool live = matchState.MatchStarted && !matchState.HasEnded && !matchState.Paused;
            container.gameObject.SetActive(live);
            if (!live)
            {
                target = null;
                LockProgress = 0f;
                return;
            }

            float dt = Time.deltaTime;
            for (int i = 0; i < bubbles.Count; i++)
            {
                if (!bubbles[i].IsActive && Time.time >= respawnAt[i]) PlaceBubble(i);
                if (bubbles[i].IsActive) bubbles[i].transform.localPosition = SlotLocalPosition(slotOfBubble[i]);
            }

            target = FindTarget();

            float highest = 0f;
            for (int i = 0; i < bubbles.Count; i++)
            {
                var bubble = bubbles[i];
                if (!bubble.IsActive) continue;

                if (bubble == target)
                {
                    secondsSinceTargeted[i] = 0f;
                    bubble.Progress = Mathf.Min(1f, bubble.Progress + dt / Mathf.Max(0.01f, holdTimeSeconds));
                }
                else
                {
                    secondsSinceTargeted[i] += dt;
                    // Drain, don't reset, once the grace window has passed.
                    if (secondsSinceTargeted[i] > graceSeconds)
                        bubble.Progress = Mathf.Max(0f, bubble.Progress - drainPerSecond * dt);
                }
                bubble.IsLocking = bubble.Progress > 0f && secondsSinceTargeted[i] <= graceSeconds;
                highest = Mathf.Max(highest, bubble.Progress);

                if (bubble.Progress >= 1f) Capture(i);
            }
            LockProgress = highest;
        }

        // The bubble under the crosshair, if all the gates pass; otherwise null.
        private CaptureBubble FindTarget()
        {
            // Predicted/held poses do not count: the tag must actually be seen this moment.
            if (!arenaFrame.IsOpponentTracked || !arenaFrame.HasLane) return null;

            var ray = aimCamera.ViewportPointToRay(new Vector3(0.5f, 0.5f, 0f));
            int mask = bubbleLayer >= 0 ? 1 << bubbleLayer : Physics.DefaultRaycastLayers;
            var hits = Physics.RaycastAll(ray, maxLockDistanceMeters + 1f, mask, QueryTriggerInteraction.Collide);

            // Nearest bubble wins; tower or unit colliders in the way are ignored.
            CaptureBubble nearest = null;
            float nearestDistance = float.MaxValue;
            foreach (var hit in hits)
            {
                var bubble = hit.collider.GetComponent<CaptureBubble>();
                if (bubble == null || !bubble.IsActive || hit.distance >= nearestDistance) continue;
                nearest = bubble;
                nearestDistance = hit.distance;
            }
            if (nearest == null) return null;

            Vector3 toCamera = aimCamera.transform.position - nearest.transform.position;
            if (toCamera.magnitude > maxLockDistanceMeters) return null;

            Transform anchor = towerController.TagAnchor;
            Vector3 facing = Vector3.ProjectOnPlane(anchor.forward, Vector3.up);
            Vector3 flatToCamera = Vector3.ProjectOnPlane(toCamera, Vector3.up);
            if (flatToCamera.sqrMagnitude > 0.0001f && Vector3.Angle(facing, flatToCamera) > maxViewingAngleDegrees) return null;

            return nearest;
        }

        private void Capture(int index)
        {
            var bubble = bubbles[index];
            bubble.SetActive(false);
            respawnAt[index] = Time.time + respawnCooldownSeconds;
            Debug.Log($"BubbleCaptureManager: bubble {bubble.Id} captured (reward {reward}).");
            OnBubbleCaptured?.Invoke(bubble.Id);
        }

        // Puts a bubble on a free slot that is not the one it just left, so players cannot camp one position.
        private void PlaceBubble(int index)
        {
            int previous = slotOfBubble[index];
            var free = new List<int>();
            for (int slot = 0; slot < SlotCount; slot++)
            {
                if (slot == previous || SlotInUse(slot, index)) continue;
                free.Add(slot);
            }
            if (free.Count == 0)
            {
                for (int slot = 0; slot < SlotCount; slot++)
                    if (slot != previous) free.Add(slot);
            }
            if (free.Count == 0) free.Add(0);

            int chosen = free[UnityEngine.Random.Range(0, free.Count)];
            slotOfBubble[index] = chosen;
            var bubble = bubbles[index];
            bubble.transform.localPosition = SlotLocalPosition(chosen);
            bubble.Progress = 0f;
            secondsSinceTargeted[index] = 0f;
            bubble.SetActive(true);
        }

        private bool SlotInUse(int slot, int exceptIndex)
        {
            for (int i = 0; i < bubbles.Count; i++)
                if (i != exceptIndex && bubbles[i].IsActive && slotOfBubble[i] == slot) return true;
            return false;
        }

        // Anchor-local: x is sideways, z points at the player, y is relative to the tag (the ground is below it).
        private Vector3 SlotLocalPosition(int slot)
        {
            int azimuthCount = Mathf.Max(1, slotAzimuthDegrees.Length);
            int heightIndex = Mathf.Clamp(slot / azimuthCount, 0, slotHeightFractions.Length - 1);
            int azimuthIndex = Mathf.Clamp(slot % azimuthCount, 0, slotAzimuthDegrees.Length - 1);

            float y = slotHeightFractions[heightIndex] * towerController.OpponentTowerHeightMeters - towerController.TagToGroundOffsetMeters;
            float azimuth = slotAzimuthDegrees[azimuthIndex] * Mathf.Deg2Rad;
            return new Vector3(Mathf.Sin(azimuth) * slotRadiusMeters, y, Mathf.Cos(azimuth) * slotRadiusMeters);
        }
    }
}
