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

        // returns how wide the arena is at the given distance along the lane, taking into account the funneling effect near the bridge and the territory width multiplier.
        public float HalfWidthAt(float distanceAlongLane)
        {
            float ownTerritoryLength = Vector3.Distance(ownHomeAnchor, ownBridgeEdge);
            float opponentTerritoryLength = Vector3.Distance(opponentHomeAnchor, opponentBridgeEdge);
            float bridgeStart = ownTerritoryLength;
            float bridgeEnd = totalLaneLength - opponentTerritoryLength;

            float islandHalfWidth = LaneWidthMeters * 0.5f * Mathf.Max(0f, territoryWidthMultiplier);
            float bridgeHalfWidth = LaneWidthMeters * laneSpineFraction * 0.5f;
            float funnel = Mathf.Clamp01(funnelFraction);

            // it means we are at the start of the bridge, and we are in our own territory. We need to calculate the width based on the funneling effect near the bridge and the territory width multiplier.
            if (distanceAlongLane <= bridgeStart)
            {
                float funnelStart = bridgeStart * (1f - funnel);
                if (distanceAlongLane <= funnelStart || bridgeStart <= funnelStart) return islandHalfWidth;
                float t = Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(funnelStart, bridgeStart, distanceAlongLane));
                return Mathf.Lerp(islandHalfWidth, bridgeHalfWidth, t);
            }

            // it means we are at the end of the bridge, and we are in the opponent's territory. We need to calculate the width based on the funneling effect near the bridge and the territory width multiplier.
            if (distanceAlongLane >= bridgeEnd)
            {
                float funnelEnd = bridgeEnd + opponentTerritoryLength * funnel;
                if (distanceAlongLane >= funnelEnd || funnelEnd <= bridgeEnd) return islandHalfWidth;
                float t = Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(bridgeEnd, funnelEnd, distanceAlongLane));
                return Mathf.Lerp(bridgeHalfWidth, islandHalfWidth, t);
            }
            return bridgeHalfWidth;
        }

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

            return Mathf.Abs(lateralOffset) <= HalfWidthAt(longitudinal) + 0.0001f;
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
        private Vector3 ClampToArenaBounds(Vector3 position, float lookAheadLongitudinal = 0f)
        {
            if (!homeAnchorsCaptured || totalLaneLength < 0.0001f) return position;

            Vector3 flat = new Vector3(position.x, 0f, position.z);
            float rearMargin = totalLaneLength * Mathf.Max(0f, rearMarginFraction);
            float longitudinal = Mathf.Clamp(Vector3.Dot(flat - ownHomeAnchor, fixedLaneDirection), -rearMargin, totalLaneLength + rearMargin);
            Vector3 onSpine = ownHomeAnchor + fixedLaneDirection * longitudinal;
            Vector3 lateralAxis = Vector3.Cross(Vector3.up, fixedLaneDirection).normalized;
            float lateralOffset = Vector3.Dot(flat - onSpine, lateralAxis);

            float halfWidthBound = HalfWidthAt(longitudinal + lookAheadLongitudinal);
            lateralOffset = Mathf.Clamp(lateralOffset, -halfWidthBound, halfWidthBound);

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

            return bridgeVisualizer.DeckHeightAt(position);
        }
    }
}
