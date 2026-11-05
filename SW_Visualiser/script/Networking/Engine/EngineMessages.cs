using System;
using System.Collections.Generic;
using System.Globalization;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace BridgeRush.Networking
{
    public enum EnginePhase { Waiting, Countdown, Running, Paused, Ended, Offline }

    public class EngineMatchInfo
    {
        public string MatchId;
        public EnginePhase Phase;
        public string Reason;
        public int CountdownMs;
        public bool[] Phone = new bool[2];
        public bool[] Glove = new bool[2];
        public bool[] Start = new bool[2];
        public string Winner;
        public int TickHz;
        public string RulesVersion;
    }

    public class EngineEvent
    {
        public string MatchId;
        public int Player;
        public string Input;
        public int ReqId;
        public string GestureEventId;
        public string Gesture;
        public string Action;
        public string What;
        public bool Ok;
        public string Reason;
        public int UnitId;
    }

    // One game/state snapshot, per player (engine_spec.md section 1.3). Units and strikes reuse the phone's own classes,
    // whose field names are the wire names.
    public class EngineState
    {
        public string MatchId;
        public int Tick;
        public float MatchTimeRemaining;
        public float[] TowerHP = new float[2];
        public float[] TowerMaxHP = new float[2];
        public float[] Mana = new float[2];
        public List<UnitSnapshot> Units = new();
        public List<StrikeSnapshot> Strikes = new();
    }

    // The other phone's viz/lane, read for one thing: where that player says their tower is (their device, or the lock point
    // once they have locked), in lane units from Player 1's end. The engine fights over this same spot.
    public class EnginePeerLane
    {
        public string MatchId;
        public bool HasPosition;
        public float Along;
        public float Lat;
        public bool Locked;
    }

    // Builds the viz/lane and game/request payloads and parses game/match, game/event and game/state. Plain C#, no Unity,
    // so it can be checked outside the Editor. A message that fails any check is rejected whole and touches nothing
    // (engine_spec.md section 6.2): JsonUtility would fill a missing field with a default and nobody would know.
    public static class EngineMessages
    {
        public const float CoordMax = 10f;
        public const int IdMax = 1_000_000;
        public const int ReasonMax = 80;

        static readonly string[] UnitTypes = { "Warrior", "Rogue", "Mage", "Tank", "Hero" };
        static readonly string[] Spells = { "lightning", "fireball" };
        static readonly string[] Phases = { "waiting", "countdown", "running", "paused", "ended", "offline" };
        static readonly string[] Winners = { "none", "player1", "player2", "draw" };
        static readonly string[] Inputs = { "glove", "tap", "bubble" };
        static readonly string[] EventActions = { "spawn", "spell", "bubble", "none" };
        static readonly string[] Reasons =
        {
            "", "not_running", "old_match", "wrong_source", "phone_not_connected", "bad_request", "engine_error",
            "no_mana", "bad_aim", "tower_out_of_arena", "unknown_gesture", "cooldown"
        };
        static readonly string[] UnitStates = { "Walking", "Attacking" };

        // --- building ---------------------------------------------------------------------------------------------

        public static string BuildLane(string matchId, bool hasLane, float laneLengthM, bool hasPosition, float along,
            float lat, bool locked, bool hasAim, float aimAlong, float aimLat)
        {
            return new JObject
            {
                ["match_id"] = matchId ?? "",
                ["hasLane"] = hasLane,
                ["lane_length_m"] = hasLane ? laneLengthM : 0f,
                ["hasPosition"] = hasPosition,
                ["along"] = hasPosition ? along : 0f,
                ["lat"] = hasPosition ? lat : 0f,
                ["locked"] = locked,
                ["hasAim"] = hasAim,
                ["aimAlong"] = hasAim ? aimAlong : 0f,
                ["aimLat"] = hasAim ? aimLat : 0f,
            }.ToString(Formatting.None);
        }

        // action: "start", "rematch", "spawn" or "spell". unitType for spawn, spell for spell, else "".
        public static string BuildRequest(string matchId, int reqId, string action, string unitType, string spell,
            bool hasAim, float along, float lat)
        {
            return new JObject
            {
                ["match_id"] = matchId ?? "",
                ["req_id"] = reqId,
                ["action"] = action,
                ["unitType"] = unitType ?? "",
                ["spell"] = spell ?? "",
                ["hasAim"] = hasAim,
                ["along"] = hasAim ? along : 0f,
                ["lat"] = hasAim ? lat : 0f,
            }.ToString(Formatting.None);
        }

        // --- parsing ----------------------------------------------------------------------------------------------

        public static bool TryParseMatch(string json, out EngineMatchInfo info, out string error)
        {
            info = null;
            if (!TryObject(json, out var o, out error)) return false;
            try
            {
                string phase = Str(o, "phase", Phases);
                string matchId = Str(o, "match_id");
                if (phase != "offline" && matchId.Length == 0) throw new FormatException("match_id is empty");
                var parsed = new EngineMatchInfo
                {
                    MatchId = matchId,
                    Phase = (EnginePhase)Array.IndexOf(Phases, phase),
                    Reason = Clip(Str(o, "reason")),
                    CountdownMs = Int(o, "countdown_ms", 0, 3_600_000),
                    Winner = Str(o, "winner", Winners),
                    TickHz = Int(o, "tick_hz", 1, 100),
                    RulesVersion = Str(o, "rules_version"),
                };
                for (int i = 0; i < 2; i++)
                {
                    parsed.Phone[i] = Bool(o, "phone" + (i + 1));
                    parsed.Glove[i] = Bool(o, "glove" + (i + 1));
                    parsed.Start[i] = Bool(o, "start" + (i + 1));
                }
                info = parsed;
                return true;
            }
            catch (FormatException e) { error = e.Message; return false; }
        }

        public static bool TryParseEvent(string json, out EngineEvent evt, out string error)
        {
            evt = null;
            if (!TryObject(json, out var o, out error)) return false;
            try
            {
                string action = Str(o, "action", EventActions);
                string what = Str(o, "what");
                if (what.Length > 0 && Array.IndexOf(UnitTypes, what) < 0 && Array.IndexOf(Spells, what) < 0)
                    throw new FormatException($"what '{what}' is not a unit or spell");
                int player = Int(o, "player", 1, 2);
                var parsed = new EngineEvent
                {
                    MatchId = Str(o, "match_id"),
                    Player = player,
                    Input = Str(o, "input", Inputs),
                    ReqId = Int(o, "req_id", -1, int.MaxValue),
                    GestureEventId = Str(o, "gesture_event_id"),
                    Gesture = Str(o, "gesture"),
                    Action = action,
                    What = what,
                    Ok = Bool(o, "ok"),
                    Reason = Str(o, "reason", Reasons),
                    UnitId = Int(o, "unit_id", -1, IdMax),
                };
                info_check(parsed);
                evt = parsed;
                return true;
            }
            catch (FormatException e) { error = e.Message; return false; }
        }

        // The tower position in the other phone's viz/lane. Rejected whole if a field this side reads is missing or out of range.
        public static bool TryParsePeerLane(string json, out EnginePeerLane lane, out string error)
        {
            lane = null;
            if (!TryObject(json, out var o, out error)) return false;
            try
            {
                lane = new EnginePeerLane
                {
                    MatchId = Str(o, "match_id"),
                    HasPosition = Bool(o, "hasPosition"),
                    Along = Num(o, "along", -CoordMax, CoordMax),
                    Lat = Num(o, "lat", -CoordMax, CoordMax),
                    Locked = Bool(o, "locked"),
                };
                return true;
            }
            catch (FormatException e) { error = e.Message; return false; }
        }

        static void info_check(EngineEvent e)
        {
            if (e.Ok && e.Reason.Length > 0) throw new FormatException("an accepted event has a reason");
            if (!e.Ok && e.Reason.Length == 0) throw new FormatException("a refused event has no reason");
        }

        // Checks the whole snapshot, units and strikes included, with every number inside its section 1.3 limit so it
        // fits the C# float or int it goes into.
        public static bool TryParseState(string json, out EngineState state, out string error)
        {
            state = null;
            if (!TryObject(json, out var o, out error)) return false;
            try
            {
                var parsed = new EngineState
                {
                    MatchId = Str(o, "match_id"),
                    Tick = Int(o, "tick", 0, int.MaxValue),
                    MatchTimeRemaining = Num(o, "matchTimeRemaining", 0, 3600),
                };
                if (parsed.MatchId.Length == 0) throw new FormatException("match_id is empty");
                for (int i = 0; i < 2; i++)
                {
                    parsed.TowerHP[i] = Num(o, "towerHP" + (i + 1), 0, 1_000_000);
                    parsed.TowerMaxHP[i] = Num(o, "towerMaxHP" + (i + 1), 0, 1_000_000);
                    parsed.Mana[i] = Num(o, "mana" + (i + 1), 0, 1000);
                }

                var units = Arr(o, "units");
                var unitIds = new HashSet<int>();
                var unitObjects = new List<JObject>();
                foreach (var item in units)
                {
                    if (item is not JObject u) throw new FormatException("a unit is not an object");
                    unitObjects.Add(u);
                    if (!unitIds.Add(Int(u, "id", 0, IdMax))) throw new FormatException("two units share an id");
                }
                foreach (var u in unitObjects)
                {
                    int target = Int(u, "target", -1, IdMax);
                    if (target != -1 && !unitIds.Contains(target)) throw new FormatException($"unit targets {target}, not in the snapshot");
                    parsed.Units.Add(new UnitSnapshot
                    {
                        id = Int(u, "id", 0, IdMax),
                        type = Str(u, "type", UnitTypes),
                        owner = Str(u, "owner", new[] { "player1", "player2" }),
                        along = Num(u, "along", -CoordMax, CoordMax),
                        lat = Num(u, "lat", -CoordMax, CoordMax),
                        hp = Num(u, "hp", 0, 1_000_000),
                        maxHp = Num(u, "maxHp", 0, 1_000_000),
                        state = Str(u, "state", UnitStates),
                        target = target,
                        range = Num(u, "range", 0, 100),
                        faceAlong = Num(u, "faceAlong", -CoordMax, CoordMax),
                        faceLat = Num(u, "faceLat", -CoordMax, CoordMax),
                    });
                }

                var strikeIds = new HashSet<int>();
                foreach (var item in Arr(o, "strikes"))
                {
                    if (item is not JObject s) throw new FormatException("a strike is not an object");
                    if (!strikeIds.Add(Int(s, "id", 0, IdMax))) throw new FormatException("two strikes share an id");
                    parsed.Strikes.Add(new StrikeSnapshot
                    {
                        id = Int(s, "id", 0, IdMax),
                        owner = Str(s, "owner", new[] { "player1", "player2" }),
                        kind = Str(s, "kind", Spells),
                        along = Num(s, "along", -CoordMax, CoordMax),
                        lat = Num(s, "lat", -CoordMax, CoordMax),
                        delay = Num(s, "delay", 0, 60),
                        fromAlong = Num(s, "fromAlong", -CoordMax, CoordMax),
                        fromLat = Num(s, "fromLat", -CoordMax, CoordMax),
                        burn = Num(s, "burn", 0, 60),
                    });
                }
                state = parsed;
                return true;
            }
            catch (FormatException e) { error = e.Message; return false; }
        }

        // --- checks -----------------------------------------------------------------------------------------------

        static bool TryObject(string json, out JObject o, out string error)
        {
            o = null;
            error = null;
            if (string.IsNullOrEmpty(json)) { error = "empty message"; return false; }
            try
            {
                using var reader = new JsonTextReader(new System.IO.StringReader(json)) { DateParseHandling = DateParseHandling.None };
                var token = JToken.Load(reader);
                if (reader.Read() && reader.TokenType != JsonToken.Comment) { error = "text after the JSON"; return false; }
                o = token as JObject;
                if (o == null) error = "not a JSON object";
                return o != null;
            }
            catch (JsonException e) { error = "bad JSON: " + e.Message; return false; }
        }

        static JToken Need(JObject o, string key, params JTokenType[] types)
        {
            if (!o.TryGetValue(key, out var token)) throw new FormatException($"missing '{key}'");
            if (Array.IndexOf(types, token.Type) < 0) throw new FormatException($"'{key}' is {token.Type}, not {string.Join("/", types)}");
            return token;
        }

        static string Str(JObject o, string key, string[] allowed = null)
        {
            string value = (string)((JValue)Need(o, key, JTokenType.String)).Value;
            if (allowed != null && Array.IndexOf(allowed, value) < 0) throw new FormatException($"'{key}' value '{value}' is not allowed");
            return value;
        }

        static bool Bool(JObject o, string key) => (bool)((JValue)Need(o, key, JTokenType.Boolean)).Value;

        static int Int(JObject o, string key, long low, long high)
        {
            var token = Need(o, key, JTokenType.Integer);
            long value;
            try { value = token.Value<long>(); }
            catch (OverflowException) { throw new FormatException($"'{key}' does not fit an int"); }
            if (value < low || value > high) throw new FormatException($"'{key}' {value} is outside {low} to {high}");
            return (int)value;
        }

        static float Num(JObject o, string key, double low, double high)
        {
            var token = Need(o, key, JTokenType.Integer, JTokenType.Float);
            double value = Convert.ToDouble(((JValue)token).Value, CultureInfo.InvariantCulture);
            if (!double.IsFinite(value) || value < low || value > high) throw new FormatException($"'{key}' {value} is outside {low} to {high}");
            return (float)value;
        }

        static JArray Arr(JObject o, string key) => (JArray)Need(o, key, JTokenType.Array);

        static string Clip(string s) => s.Length <= ReasonMax ? s : s.Substring(0, ReasonMax);
    }
}
