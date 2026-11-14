using System;
using UnityEngine;
using BridgeRush.Networking;

namespace BridgeRush.Match
{
    // The board-client role (engine_spec.md section 6.3): the Ultra96 engine runs the match, this phone sends its inputs
    // through EngineLink and draws the engine's snapshots for the player it chose. Nothing is simulated or spent locally.
    // Also holds what EngineLink reads: tower and crosshair in lane units, lane length, and the Paused setter.
    public partial class MatchState
    {
        [Header("Board engine")]
        [Tooltip("Wire EngineLink here to let this phone be a board client. Left empty, the board role never starts.")]
        [SerializeField] private EngineLink engineLink;

        private int boardTickHz;
        private int lastBoardState;
        private int lastBoardTick = -1;
        private bool boardResultSet;

        public string BoardRefusalText { get; private set; } = "";

        // A summon or spell the board refused or accepted, for the cards and the banner. The reason is a short card text.
        public event Action<string> BoardSpellCast;
        public event Action<string, string> BoardSpellRejected;

        public bool IsBoardClient => Role == NetworkRole.BoardClient;
        private bool DoesNotSimulate => Role == NetworkRole.Client || Role == NetworkRole.BoardClient;

        // Which owner string in a snapshot is this phone's player.
        private string LocalOwnerName => Role == NetworkRole.BoardClient ? (LocalIsPlayer1 ? "player1" : "player2") : "player2";

        public bool BoardEngineAvailable => engineLink != null;

        // The match the board is running, "" outside the board role. BubbleSync sends it with a capture.
        public string BoardMatchId => IsBoardClient && engineLink != null ? engineLink.Core.MatchId : "";

        // --- seam for EngineLink ----------------------------------------------------------------------------------

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

        // The other player's tower where that player reports it to the board (their device, or their lock point), in this
        // phone's world, so it stands where the engine fights over it. False outside the board role, without a fresh report
        // or before this match's frame exists; the caller then falls back to the tag.
        public bool TryGetOpponentTowerReported(out Vector3 worldPosition, out bool locked)
        {
            worldPosition = Vector3.zero;
            locked = false;
            if (!IsBoardClient || engineLink == null || !homeAnchorsCaptured) return false;
            if (!engineLink.TryGetPeerTower(out float along, out float lat, out locked)) return false;
            worldPosition = FromCanonical(along, lat);
            return true;
        }

        // God mode for the debug menu. Where the phone simulates (VS CPU, the host) it pins your mana and tower; on the board
        // it is this player's own request to the engine, so it works for Player 1 and Player 2 alike. A host-phone client has
        // no way to ask the host.
        public bool GodModeAvailable => Role == NetworkRole.Local || Role == NetworkRole.Host || Role == NetworkRole.BoardClient;

        public void SetGodMode(bool on)
        {
            GodMode = on;
            if (IsBoardClient && engineLink != null) engineLink.SetGodMode(on);
        }

        public void SetPaused(bool paused) => Paused = paused;

        // --- what the screens ask ---------------------------------------------------------------------------------

        // Time left in the board's countdown, or -1 when it isn't counting down.
        public int BoardCountdownMs
        {
            get
            {
                var info = engineLink != null ? engineLink.Match : null;
                return IsBoardClient && info != null && info.Phase == EnginePhase.Countdown && !matchStarted ? info.CountdownMs : -1;
            }
        }

        // One line for the player while something is wrong or being waited for, "" when there is nothing to say.
        public string BoardStatusMessage
        {
            get
            {
                if (!IsBoardClient || engineLink == null) return "";
                var info = engineLink.Match;
                var link = engineLink.LinkState;
                if (engineLink.EngineIsOffline) return "The game engine is offline";
                if (link == EngineLinkState.Disconnected || link == EngineLinkState.Stale) return "Reconnecting to the game...";
                if (info == null) return "Waiting for the game engine...";
                if (matchStarted && info.Phase == EnginePhase.Paused) return "Paused: " + info.Reason;
                if (!matchStarted && (info.Phase == EnginePhase.Waiting || info.Phase == EnginePhase.Countdown) && info.Reason.Length > 0)
                    return info.Reason.Substring(0, 1).ToUpperInvariant() + info.Reason.Substring(1);
                return "";
            }
        }

        public bool BoardPressStart() => IsBoardClient && engineLink != null && engineLink.PressStart();
        public bool BoardPressRematch() => IsBoardClient && engineLink != null && engineLink.PressRematch();

        // --- actions: a tap only asks the board ---------------------------------------------------------------------

        private bool BoardSendSummon(UnitType type)
        {
            if (engineLink != null && engineLink.Active && engineLink.SendSummon(type) >= 0) return true;
            BoardRefusalText = ReasonText("not_running");
            OwnUnitRejected?.Invoke(type, ReasonShort("not_running"));
            return false;
        }

        public bool BoardSendSpell(string spell)
        {
            if (engineLink != null && engineLink.Active && engineLink.SendSpell(spell) >= 0) return true;
            BoardRefusalText = ReasonText("not_running");
            BoardSpellRejected?.Invoke(spell, ReasonShort("not_running"));
            return false;
        }

        private void OnEnable()
        {
            if (engineLink == null) return;
            engineLink.ActionResult += OnBoardActionResult;
            engineLink.MatchChanged += OnBoardMatchChanged;
        }

        private void OnDisable()
        {
            if (engineLink == null) return;
            engineLink.ActionResult -= OnBoardActionResult;
            engineLink.MatchChanged -= OnBoardMatchChanged;
        }

        // How long a summon takes to show: the board's accepted event to the first snapshot with the unit (debug menu).
        public readonly SummonLatencyTracker SummonLatency = new SummonLatencyTracker();

        // The board's answer to one of this player's actions, from a card tap or a glove gesture alike.
        private void OnBoardActionResult(EngineEvent e)
        {
            if (!IsBoardClient || e.Action == "bubble") return;
            if (!e.Ok && (e.Reason == "old_match" || e.Reason == "cooldown")) return;

            if (e.Action == "spell")
            {
                if (e.Ok) BoardSpellCast?.Invoke(e.What);
                else { BoardRefusalText = ReasonText(e.Reason); BoardSpellRejected?.Invoke(e.What, ReasonShort(e.Reason)); }
                return;
            }

            bool known = Enum.TryParse(e.What, out UnitType type);
            if (e.Ok)
            {
                if (known)
                {
                    OwnUnitSummoned?.Invoke(type);
                    SummonLatency.NoteAccepted(e.UnitId, Time.realtimeSinceStartup);
                }
                return;
            }
            BoardRefusalText = ReasonText(e.Reason);
            // A gesture the game doesn't know names no unit: show it against the first card so the banner still appears.
            OwnUnitRejected?.Invoke(known ? type : UnitType.Warrior, ReasonShort(e.Reason));
        }

        private static string ReasonShort(string reason)
        {
            switch (reason)
            {
                case "no_mana": return "NO MANA";
                case "bad_aim": return "BAD AIM";
                case "tower_out_of_arena": return "OUT OF ARENA";
                case "not_running": return "PAUSED";
                case "phone_not_connected": return "NO PHONE";
                case "unknown_gesture": return "UNKNOWN";
                case "wrong_source": return "ANOTHER PHONE";
                default: return "ERROR";
            }
        }

        private static string ReasonText(string reason)
        {
            switch (reason)
            {
                case "no_mana": return "Not enough mana.";
                case "bad_aim": return "Aim the crosshair at the ground on your own side of the arena (a spell: anywhere on the island).";
                case "tower_out_of_arena": return "You are outside the arena. Walk back to your side.";
                case "not_running": return "The match is not running (not started, paused or over).";
                case "phone_not_connected": return "The game has no recent aim from this phone.";
                case "unknown_gesture": return "That gesture is not bound to anything.";
                case "wrong_source": return "Another phone is playing as this player.";
                default: return "The game engine could not do that.";
            }
        }

        // --- the board role's frame loop ----------------------------------------------------------------------------

        private void OnBoardMatchChanged(string matchId)
        {
            if (!IsBoardClient) return;
            ResetMatch();
            Paused = false;
            lastBoardTick = -1;
            boardResultSet = false;
            BoardRefusalText = "";
            SummonLatency.NewMatch();
        }

        private void UpdateBoardClient()
        {
            if (engineLink == null || !engineLink.Active) return;
            var info = engineLink.Match;
            if (info == null) return;
            boardTickHz = info.TickHz;

            StartFromBoardPhase(info);
            if (!matchStarted) return;

            // Capture the frame even while paused: a phone that rejoins a paused match must still send its position.
            CaptureHomeAnchorsIfNeeded();
            CheckDoubleMana();

            Paused = info.Phase == EnginePhase.Paused;
            if (!Paused) timeSinceLastTick += Time.deltaTime;

            if (engineLink.TryTakeState(ref lastBoardState, out var state)) ApplyBoardState(state);

            if (info.Phase == EnginePhase.Ended && !boardResultSet) SetBoardResult(info.Winner);
        }

        // The board's `running` is the only signal that starts a match here. A phone that was not part of the countdown (a
        // relaunched app, or one that joined late) joins the same way, once it is locked in and has a snapshot of that match.
        private void StartFromBoardPhase(EngineMatchInfo info)
        {
            if (matchStarted) return;
            bool live = info.Phase == EnginePhase.Running || info.Phase == EnginePhase.Paused;
            if (!live || engineLink.Core.LatestState == null || !engineLink.IsLockedIn) return;
            StartMatch();
        }

        private void ApplyBoardState(EngineState s)
        {
            int me = LocalIsPlayer1 ? 0 : 1;
            int other = 1 - me;
            OwnTowerHP = s.TowerHP[me];
            OpponentTowerHP = s.TowerHP[other];
            ManaCurrent = s.Mana[me];
            enemyManaCurrent = s.Mana[other];
            matchTimeRemaining = s.MatchTimeRemaining;

            // Units, strikes and their smoothing need this match's frame. Until it exists only the numbers apply.
            if (!homeAnchorsCaptured) return;

            bool newTick = s.Tick != lastBoardTick;
            if (newTick) timeSinceLastTick = 0f;
            lastBoardTick = s.Tick;
            // A repeated tick (paused, or an extra snapshot between ticks) adds units and strikes and nothing else, so the
            // units already on screen keep gliding instead of jumping.
            float seenAt = Time.realtimeSinceStartup;
            foreach (var unit in s.Units) SummonLatency.NoteSeen(unit.id, seenAt);
            ApplyUnitSnapshots(s.Units, LocalOwnerName, newTick, newTick);
            ApplyStrikeSnapshots(s.Strikes);
        }

        private void SetBoardResult(string winner)
        {
            boardResultSet = true;
            string mine = LocalIsPlayer1 ? "player1" : "player2";
            string theirs = LocalIsPlayer1 ? "player2" : "player1";
            Result = winner == mine ? MatchResult.Win : winner == theirs ? MatchResult.Loss : MatchResult.Draw;
        }
    }
}
