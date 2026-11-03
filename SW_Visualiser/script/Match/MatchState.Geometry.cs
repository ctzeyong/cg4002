using UnityEngine;

namespace BridgeRush.Match
{
    public partial class MatchState
    {
        // This method captures the home anchors for both players if they haven't been captured yet. 
        // It calculates the positions of the own and opponent home anchors based on the lane information provided by the ArenaFrame. 
        // It also calculates the total lane length, fixed lane direction, and bridge edges for both players. 
        private void CaptureHomeAnchorsIfNeeded()
        {
            //  fires exactly once, on the first Update() after StartMatch()
            if (!matchStarted || homeAnchorsCaptured || !arenaFrame.HasLane) return;

            var lane = arenaFrame.Lane;
            Vector3 offset = new Vector3(0f, groundYOffsetMeters, 0f);
            ownHomeAnchor = lane.Origin + offset;
            opponentHomeAnchor = lane.Origin + lane.Direction * lane.Length + offset;
            totalLaneLength = Vector3.Distance(ownHomeAnchor, opponentHomeAnchor);

            float territoryFraction = (1f - bridgeFraction) * 0.5f;
            fixedLaneDirection = lane.Direction;
            ownBridgeEdge = ownHomeAnchor + fixedLaneDirection * (territoryFraction * totalLaneLength);
            opponentBridgeEdge = opponentHomeAnchor - fixedLaneDirection * (territoryFraction * totalLaneLength);
            bridgePosition = (ownBridgeEdge + opponentBridgeEdge) * 0.5f;

            if (twoBridges)
            {
                float offsetMeters = LaneWidthMeters * Mathf.Max(0f, bridgeLateralOffsetFraction);
                bridgeLateralOffsets = new[] { -offsetMeters, offsetMeters };
            }
            else
            {
                bridgeLateralOffsets = new[] { 0f };
            }
            bridgeTieToggle = false;

            frozenArenaScale = ComputeArenaScale(totalLaneLength);
            arenaScaleFrozen = true;

            homeAnchorsCaptured = true;
        }

        private void ApplyOutOfBoundsDamage(float deltaTime)
        {
            if (!homeAnchorsCaptured || !arenaFrame.HasLane) return;

            var lane = arenaFrame.Lane;
            Vector3 ownPosition = OwnTowerPosition;
            Vector3 opponentPosition = lane.Origin + lane.Direction * lane.Length;

            if (!IsInsideArenaBounds(ownPosition))
            {
                OwnTowerHP = Mathf.Max(0f, OwnTowerHP - outOfBoundsDamagePerSecond * deltaTime);
            }
            if (!IsInsideArenaBounds(opponentPosition))
            {
                OpponentTowerHP = Mathf.Max(0f, OpponentTowerHP - outOfBoundsDamagePerSecond * deltaTime);
            }
        }

        // 0 on the open island, 1 on the bridge span, smooth in the funnel between. Same curve the single-bridge arena used.
        private float FunnelBlend(float distanceAlongLane)
        {
            float ownTerritoryLength = Vector3.Distance(ownHomeAnchor, ownBridgeEdge);
            float opponentTerritoryLength = Vector3.Distance(opponentHomeAnchor, opponentBridgeEdge);
            float bridgeStart = ownTerritoryLength;
            float bridgeEnd = totalLaneLength - opponentTerritoryLength;
            float funnel = Mathf.Clamp01(funnelFraction);

            if (distanceAlongLane <= bridgeStart)
            {
                float funnelStart = bridgeStart * (1f - funnel);
                if (distanceAlongLane <= funnelStart || bridgeStart <= funnelStart) return 0f;
                return Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(funnelStart, bridgeStart, distanceAlongLane));
            }

            if (distanceAlongLane >= bridgeEnd)
            {
                float funnelEnd = bridgeEnd + opponentTerritoryLength * funnel;
                if (distanceAlongLane >= funnelEnd || funnelEnd <= bridgeEnd) return 0f;
                return 1f - Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(bridgeEnd, funnelEnd, distanceAlongLane));
            }
            return 1f;
        }

        private float IslandHalfWidth => LaneWidthMeters * 0.5f * Mathf.Max(0f, territoryWidthMultiplier);

        // The walkable lateral interval around one bridge at this blend. On the open island (blend 0) every bridge's
        // interval covers the whole island; towards the span it shrinks onto the bridge strip.
        private bool TryGetBridgeInterval(int bridgeIndex, float blend, out float low, out float high)
        {
            float island = IslandHalfWidth;
            float bridgeHalf = BridgeWidthMeters * 0.5f;
            float widest = island + Mathf.Abs(bridgeLateralOffsets[bridgeIndex]);
            float half = Mathf.Lerp(widest, bridgeHalf, blend);
            low = Mathf.Max(bridgeLateralOffsets[bridgeIndex] - half, -island);
            high = Mathf.Min(bridgeLateralOffsets[bridgeIndex] + half, island);
            return low <= high;
        }

        // Nearest allowed lateral offset at this distance along the lane. With preferredBridge >= 0 only that bridge's
        // interval counts, so a unit committed to one bridge is never snapped onto the other.
        private float ClampLateral(float distanceAlongLane, float lateral, int preferredBridge = -1)
        {
            float blend = FunnelBlend(distanceAlongLane);
            float best = lateral;
            float bestDistance = float.MaxValue;
            for (int i = 0; i < bridgeLateralOffsets.Length; i++)
            {
                if (preferredBridge >= 0 && preferredBridge < bridgeLateralOffsets.Length && i != preferredBridge) continue;
                if (!TryGetBridgeInterval(i, blend, out float low, out float high)) continue;
                float clamped = Mathf.Clamp(lateral, low, high);
                float distance = Mathf.Abs(clamped - lateral);
                if (distance < bestDistance)
                {
                    bestDistance = distance;
                    best = clamped;
                }
            }
            return best;
        }

        private float LateralOf(Vector3 position)
        {
            Vector3 flat = new Vector3(position.x, 0f, position.z);
            return Vector3.Dot(flat - new Vector3(ownHomeAnchor.x, 0f, ownHomeAnchor.z), BridgeLateralAxis);
        }

        private float LongitudinalOf(Vector3 position)
        {
            Vector3 flat = new Vector3(position.x, 0f, position.z);
            return Vector3.Dot(flat - new Vector3(ownHomeAnchor.x, 0f, ownHomeAnchor.z), fixedLaneDirection);
        }

        private int ChooseBridge(Vector3 worldPosition)
        {
            if (bridgeLateralOffsets.Length <= 1 || !homeAnchorsCaptured) return 0;

            float lateral = LateralOf(worldPosition);
            int best = 0;
            float bestDistance = Mathf.Abs(lateral - bridgeLateralOffsets[0]);
            for (int i = 1; i < bridgeLateralOffsets.Length; i++)
            {
                float distance = Mathf.Abs(lateral - bridgeLateralOffsets[i]);
                if (distance < bestDistance) { best = i; bestDistance = distance; }
            }

            // A spawn on the axis is equally close to both: alternate so a bot or tower spawn uses both bridges.
            if (bridgeLateralOffsets.Length == 2 && Mathf.Abs(Mathf.Abs(lateral - bridgeLateralOffsets[0]) - Mathf.Abs(lateral - bridgeLateralOffsets[1])) < 0.01f)
            {
                bridgeTieToggle = !bridgeTieToggle;
                return bridgeTieToggle ? 0 : 1;
            }
            return best;
        }

        private bool IsOnBridgeSpan(Vector3 position)
        {
            float spanLength = Vector3.Distance(ownBridgeEdge, opponentBridgeEdge);
            float along = Vector3.Dot(position - ownBridgeEdge, fixedLaneDirection);
            return along >= 0f && along <= spanLength;
        }

        // Same rectangle as the arena but the full island width all the way along, so a spell can be aimed at the
        // water between the two bridges.
        public bool IsInsideSpellArea(Vector3 position)
        {
            if (!homeAnchorsCaptured || totalLaneLength < 0.0001f) return false;
            float longitudinal = LongitudinalOf(position);
            float rearMargin = totalLaneLength * Mathf.Max(0f, rearMarginFraction);
            if (longitudinal < -rearMargin || longitudinal > totalLaneLength + rearMargin) return false;
            return Mathf.Abs(LateralOf(position)) <= IslandHalfWidth;
        }

        public bool IsInsideArena(Vector3 position) => IsInsideArenaBounds(position);

        // either player standing outside drains that side's tower HP at outOfBoundsDamagePerSecond. 
        private bool IsInsideArenaBounds(Vector3 position)
        {
            if (!homeAnchorsCaptured || totalLaneLength < 0.0001f) return true;

            Vector3 flat = new Vector3(position.x, 0f, position.z);
            float longitudinal = Vector3.Dot(flat - ownHomeAnchor, fixedLaneDirection);
            float rearMargin = totalLaneLength * Mathf.Max(0f, rearMarginFraction);
            if (longitudinal < -rearMargin - 0.0001f || longitudinal > totalLaneLength + rearMargin + 0.0001f) return false;

            Vector3 onSpine = ownHomeAnchor + fixedLaneDirection * longitudinal;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, fixedLaneDirection).normalized;
            float lateralOffset = Vector3.Dot(flat - onSpine, lateralAxis);

            return Mathf.Abs(ClampLateral(longitudinal, lateralOffset) - lateralOffset) <= 0.0001f;
        }

        // Checks if a given position is inside the player's own territory, based on the captured home anchors and lane information.
        public bool IsInsideOwnTerritory(Vector3 position)
        {
            if (!homeAnchorsCaptured || totalLaneLength < 0.0001f) return false;

            Vector3 flat = new Vector3(position.x, 0f, position.z);
            float longitudinal = Vector3.Dot(flat - ownHomeAnchor, fixedLaneDirection);
            float ownTerritoryLength = Vector3.Distance(ownHomeAnchor, ownBridgeEdge);
            float rearMargin = totalLaneLength * Mathf.Max(0f, rearMarginFraction);
            if (longitudinal < -rearMargin - 0.0001f || longitudinal > ownTerritoryLength + 0.0001f) return false;

            Vector3 onSpine = ownHomeAnchor + fixedLaneDirection * longitudinal;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, fixedLaneDirection).normalized;
            float lateralOffset = Vector3.Dot(flat - onSpine, lateralAxis);
            float islandHalfWidth = LaneWidthMeters * 0.5f * Mathf.Max(0f, territoryWidthMultiplier);
            return Mathf.Abs(lateralOffset) <= islandHalfWidth + 0.0001f;
        }
        // Clamps a position to the bounds of the arena, ensuring it stays within the valid playing area.
        private Vector3 ClampToArenaBounds(Vector3 position, float lookAheadLongitudinal = 0f, int preferredBridge = -1)
        {
            if (!homeAnchorsCaptured || totalLaneLength < 0.0001f) return position;

            Vector3 flat = new Vector3(position.x, 0f, position.z);
            float rearMargin = totalLaneLength * Mathf.Max(0f, rearMarginFraction);
            float longitudinal = Mathf.Clamp(Vector3.Dot(flat - ownHomeAnchor, fixedLaneDirection), -rearMargin, totalLaneLength + rearMargin);
            Vector3 onSpine = ownHomeAnchor + fixedLaneDirection * longitudinal;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, fixedLaneDirection).normalized;
            float lateralOffset = Vector3.Dot(flat - onSpine, lateralAxis);

            lateralOffset = ClampLateral(longitudinal + lookAheadLongitudinal, lateralOffset, preferredBridge);

            Vector3 clampedFlat = onSpine + lateralAxis * lateralOffset;
            return new Vector3(clampedFlat.x, position.y, clampedFlat.z);
        }

        public float GroundY => homeAnchorsCaptured
            ? ownHomeAnchor.y
            : (arenaFrame.HasLane ? arenaFrame.Lane.Origin.y : 0f) + groundYOffsetMeters;

        private Vector3 FlattenY(Vector3 position) => new Vector3(position.x, GroundY, position.z);

        // i want to compute the height of a unit based on its position along the lane, taking into account the bridge's 
        // deck height and the funneling effect near the bridge. This will help in determining if a unit is above or below 
        // the bridge deck, which can affect gameplay mechanics such as line of sight or collision detection.
        private float ComputeUnitHeight(Vector3 position)
        {
            float bridgeSpanLength = Vector3.Distance(ownBridgeEdge, opponentBridgeEdge);
            if (bridgeSpanLength <= 0.0001f) return GroundY;

            float acrossBridge01 = Vector3.Dot(position - ownBridgeEdge, fixedLaneDirection) / bridgeSpanLength;
            if (acrossBridge01 < 0f || acrossBridge01 > 1f) return GroundY;

            float lateral = LateralOf(position);
            float strayLimit = BridgeWidthMeters * 0.75f;
            bool onADeck = false;
            for (int i = 0; i < bridgeLateralOffsets.Length; i++)
            {
                if (Mathf.Abs(lateral - bridgeLateralOffsets[i]) <= strayLimit) { onADeck = true; break; }
            }
            if (!onADeck) return GroundY;

            return bridgeVisualizer.DeckHeightAt(position);
        }
    }
}
