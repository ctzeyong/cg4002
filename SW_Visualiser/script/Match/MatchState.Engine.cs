using UnityEngine;

namespace BridgeRush.Match
{
    // What EngineLink reads to tell the board engine where this phone's tower and crosshair are, in lane units (the same
    // canonical frame as the snapshots), and the one setter it needs to pause the match from the engine's phase.
    public partial class MatchState
    {
        // This match's anchors are captured, so positions in lane units mean something. Before that the numbers are
        // from the last match's frame or from no frame at all.
        public bool FrameCaptured => homeAnchorsCaptured;

        public bool HasLane => arenaFrame != null && arenaFrame.HasLane;

        public float LaneLengthMeters => HasLane ? arenaFrame.Lane.Length : 0f;

        public bool TryGetOwnLanePosition(out float along, out float lat)
        {
            along = lat = 0f;
            if (!homeAnchorsCaptured || !arenaFrame.HasLane) return false;
            (along, lat) = ToCanonical(OwnTowerPosition);
            return true;
        }

        // The crosshair's floor hit, valid for a summon or not: the engine checks the spot per action.
        public bool TryGetAimLanePosition(out float along, out float lat)
        {
            along = lat = 0f;
            if (!homeAnchorsCaptured || summonCursor == null || !summonCursor.HasAimHit) return false;
            (along, lat) = ToCanonical(summonCursor.AimedWorldPosition);
            return true;
        }

        public void SetPaused(bool paused) => Paused = paused;
    }
}
