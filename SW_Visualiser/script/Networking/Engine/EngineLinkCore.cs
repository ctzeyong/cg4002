using System;
using System.Collections.Generic;

namespace BridgeRush.Networking
{
    public enum EngineLinkState { Disconnected, Waiting, Live, Stale }

    // One request for the wrapper to publish on game/request/{player}.
    public class EngineOutgoing
    {
        public int ReqId;
        public string MatchId;
        public string Action;   // "start", "rematch", "spawn", "spell"
        public string UnitType; // for spawn
        public string Spell;    // for spell
        public bool HasAim;
        public float Along;
        public float Lat;
    }

    // The decisions of EngineLink with no Unity in them and the time passed in, so they can be checked outside the Editor
    // (engine_spec.md section 6.1). The MonoBehaviour only feeds it messages and publishes what it asks for.
    public class EngineLinkCore
    {
        public const float StartResendS = 2f;
        public const float TapTimeoutS = 2f;
        public const float LinkAlarmS = 1f;
        public const float StallRebuildS = 5f;
        public const float StallRebuildMaxS = 60f;

        public int Player; // 1 or 2, 0 until chosen

        // The last game/match, kept through any link loss and never cleared (R84).
        public EngineMatchInfo Match { get; private set; }
        public string MatchId => Match != null ? Match.MatchId : "";

        public EngineState LatestState { get; private set; }
        // Goes up with every accepted snapshot. The game applies the latest one when this is newer than the last it applied.
        public int StateCounter { get; private set; }

        // True from an `offline` game/match until the next real one.
        public bool Offline { get; private set; }

        public event Action<string> MatchChanged;     // a game/match with a new match id
        public event Action EngineOffline;
        public event Action<EngineMatchInfo> MatchUpdated;
        public event Action<EngineEvent> ActionResult; // this player's result: a tap, a glove gesture, a bubble
        public event Action<int> ActionUnknown;        // a tap whose result never came: the outcome is unknown, not "failed"

        readonly List<EngineOutgoing> outgoing = new();
        readonly Dictionary<int, float> pendingTaps = new(); // req_id -> deadline
        int nextReqId = 1;

        EngineOutgoing pendingPress;
        float pressLastSent;

        bool connected;
        float connectedSince;
        float lastStateArrival = float.NegativeInfinity;
        float stallWait = StallRebuildS;
        float stallBase;
        bool reconnectRequested;

        // --- inputs -----------------------------------------------------------------------------------------------

        public void SetConnected(bool isConnected, float now)
        {
            if (isConnected && !connected) { connectedSince = now; stallBase = now; }
            connected = isConnected;
        }

        public void OnMatch(EngineMatchInfo info, float now)
        {
            bool changed = info.Phase != EnginePhase.Offline && info.MatchId != MatchId;
            if (info.Phase == EnginePhase.Offline)
            {
                // The will names no match: it applies to whatever is shown, which stays as it is.
                Offline = true;
                EngineOffline?.Invoke();
                return;
            }
            Offline = false;
            Match = info;
            if (changed)
            {
                LatestState = null;
                lastStateArrival = float.NegativeInfinity;
            }
            if (changed) MatchChanged?.Invoke(info.MatchId);
            MatchUpdated?.Invoke(info);
        }

        // A snapshot that passed EngineMessages.TryParseState. Rejected unless it is of the current match.
        public bool OnState(EngineState state, float now)
        {
            if (Match == null || state.MatchId != MatchId) return false;
            LatestState = state;
            StateCounter++;
            lastStateArrival = now;
            stallWait = StallRebuildS;
            stallBase = now;
            return true;
        }

        public void OnEvent(EngineEvent e)
        {
            if (Match == null || e.MatchId != MatchId) return; // another match
            if (e.Input == "tap") pendingTaps.Remove(e.ReqId);
            if (e.Player == Player) ActionResult?.Invoke(e);
        }

        // --- outputs ----------------------------------------------------------------------------------------------

        // Returns -1 when no game/match has been heard yet, so there is nothing to name.
        public int SendTap(string action, string unitType, string spell, bool hasAim, float along, float lat, float now)
        {
            if (Match == null) return -1;
            var request = new EngineOutgoing
            {
                ReqId = nextReqId++, MatchId = MatchId, Action = action, UnitType = unitType ?? "", Spell = spell ?? "",
                HasAim = hasAim, Along = along, Lat = lat,
            };
            pendingTaps[request.ReqId] = now + TapTimeoutS;
            outgoing.Add(request);
            return request.ReqId;
        }

        // START in waiting, "rematch" in ended. Re-sent with the same req_id until game/match lists the press.
        public bool BeginPress(string action, float now)
        {
            if (Match == null) return false;
            pendingPress = new EngineOutgoing { ReqId = nextReqId++, MatchId = MatchId, Action = action, UnitType = "", Spell = "" };
            pressLastSent = now;
            outgoing.Add(pendingPress);
            return true;
        }

        public bool PressPending => pendingPress != null;

        public List<EngineOutgoing> TakeOutgoing()
        {
            var list = new List<EngineOutgoing>(outgoing);
            outgoing.Clear();
            return list;
        }

        // True once when the wrapper should drop the MQTT connection: connected, but no snapshot for too long. Link
        // states can't be trusted (IsConnected stays true through a stall while the phone streams, R85).
        public bool TakeReconnectRequest()
        {
            bool r = reconnectRequested;
            reconnectRequested = false;
            return r;
        }

        public EngineLinkState State(float now)
        {
            if (!connected) return EngineLinkState.Disconnected;
            if (float.IsNegativeInfinity(lastStateArrival)) return EngineLinkState.Waiting;
            return now - lastStateArrival <= LinkAlarmS ? EngineLinkState.Live : EngineLinkState.Stale;
        }

        public void Tick(float now)
        {
            ExpireTaps(now);
            ResendPress(now);
            CheckStall(now);
        }

        void ExpireTaps(float now)
        {
            List<int> expired = null;
            foreach (var kv in pendingTaps)
                if (now >= kv.Value) (expired ??= new List<int>()).Add(kv.Key);
            if (expired == null) return;
            foreach (int id in expired) { pendingTaps.Remove(id); ActionUnknown?.Invoke(id); }
        }

        void ResendPress(float now)
        {
            if (pendingPress == null || Match == null || Player < 1 || Player > 2) return;
            bool wanted = (pendingPress.Action == "start" && (Match.Phase == EnginePhase.Waiting || Match.Phase == EnginePhase.Countdown))
                       || (pendingPress.Action == "rematch" && Match.Phase == EnginePhase.Ended);
            if (!wanted || Match.Start[Player - 1])
            {
                pendingPress = null; // counted, or the match moved on
                return;
            }
            if (now - pressLastSent < StartResendS) return;
            pressLastSent = now;
            if (pendingPress.MatchId != MatchId)
            {
                // A different match since the press: send it again for this one, under a new req_id
                pendingPress = new EngineOutgoing { ReqId = nextReqId++, MatchId = MatchId, Action = pendingPress.Action, UnitType = "", Spell = "" };
            }
            outgoing.Add(pendingPress);
        }

        void CheckStall(float now)
        {
            if (!connected) return;
            float baseline = Math.Max(Math.Max(lastStateArrival, connectedSince), stallBase);
            if (now - baseline < stallWait) return;
            reconnectRequested = true;
            stallBase = now;
            stallWait = Math.Min(stallWait * 2f, StallRebuildMaxS); // doubles while no snapshot comes back (an engine that is down)
        }
    }
}
