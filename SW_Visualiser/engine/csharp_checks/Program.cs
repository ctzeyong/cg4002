using System;
using System.Collections.Generic;
using System.IO;
using BridgeRush.Networking;
using Newtonsoft.Json.Linq;

// Checks EngineMessages outside Unity. Usage: dotnet run -- CASES.json BUILT.json
// CASES.json comes from engine/dump_wire_cases.py (what the board really sends). BUILT.json gets what this side
// builds, for check_built.py to read back with the board's own parsers.
static class Checks
{
    static int failures;

    static void Check(bool ok, string what)
    {
        if (!ok) { failures++; Console.WriteLine("FAIL: " + what); }
    }

    static void Rejected(string json, string what)
    {
        Check(!EngineMessages.TryParseState(json, out _, out _), "state accepted: " + what);
    }

    static string With(JObject o, Action<JObject> edit)
    {
        var copy = (JObject)o.DeepClone();
        edit(copy);
        return copy.ToString();
    }

    static int Main(string[] args)
    {
        var cases = JObject.Parse(File.ReadAllText(args[0]));

        int accepted = 0, units = 0, strikes = 0;
        foreach (var s in cases["states"])
        {
            Check(EngineMessages.TryParseState(s.ToString(), out var st, out var err), "real state rejected: " + err);
            if (st == null) continue;
            accepted++; units += st.Units.Count; strikes += st.Strikes.Count;
        }
        Check(units > 100 && strikes > 5, $"the real match should have units ({units}) and strikes ({strikes})");

        var phases = new List<EnginePhase>();
        foreach (var m in cases["matches"])
        {
            Check(EngineMessages.TryParseMatch(m.ToString(), out var info, out var err), "real match rejected: " + err);
            if (info != null) phases.Add(info.Phase);
        }
        Check(phases.Count == 6 && phases[0] == EnginePhase.Waiting && phases[5] == EnginePhase.Offline, "phases " + string.Join(",", phases));
        EngineMessages.TryParseMatch(cases["matches"][1].ToString(), out var countdown, out _);
        Check(countdown.CountdownMs == 2500 && countdown.TickHz == 10 && countdown.Phone[0] && !countdown.Phone[1]
              && countdown.Start[1] && countdown.RulesVersion == "v1" && countdown.Reason == "player 2 glove down", "countdown fields");

        foreach (var e in cases["events"])
            Check(EngineMessages.TryParseEvent(e.ToString(), out _, out var err), "real event rejected: " + err);
        EngineMessages.TryParseEvent(cases["events"][0].ToString(), out var ev, out _);
        Check(ev.Ok && ev.What == "Warrior" && ev.UnitId > 0 && ev.GestureEventId == "ev-1" && ev.Input == "glove", "event fields");
        EngineMessages.TryParseEvent(cases["events"][1].ToString(), out var refused, out _);
        Check(!refused.Ok && refused.Reason == "unknown_gesture" && refused.UnitId == -1, "refused event fields");

        // a state with units and a strike, to take apart
        JObject rich = null;
        foreach (JObject s in cases["states"])
            if (((JArray)s["units"]).Count >= 2 && ((JArray)s["strikes"]).Count >= 1) { rich = s; break; }
        Check(rich != null, "a state with units and strikes");

        Rejected("", "empty");
        Rejected("[]", "an array");
        Rejected("{\"a\":1} x", "trailing text");
        Rejected(With(rich, o => o.Remove("towerHP1")), "missing tower HP");
        Rejected(With(rich, o => o.Remove("units")), "missing units");
        Rejected(With(rich, o => o["units"] = "none"), "units not a list");
        Rejected(With(rich, o => o.Remove("strikes")), "missing strikes");
        Rejected(With(rich, o => ((JObject)o["units"][0]).Remove("hp")), "a unit without hp");
        Rejected(With(rich, o => o["units"][0]["hp"] = 1e100), "hp 1e100");
        Rejected(With(rich, o => o["units"][0]["hp"] = 2_000_000), "hp over a million");
        Rejected(With(rich, o => ((JArray)o["strikes"]).Add(o["strikes"][0].DeepClone())), "duplicate strike id");
        Rejected(With(rich, o => o["units"][0]["id"] = 1000001), "id 1000001");
        Rejected(With(rich, o => o["units"][0]["id"] = 3000000000L), "id 3 billion");
        Rejected(With(rich, o => o["units"][0]["id"] = 3.0), "id as a float token");
        Rejected(With(rich, o => o["units"][1]["id"] = o["units"][0]["id"]), "duplicate unit id");
        Rejected(With(rich, o => o["units"][0]["target"] = 999999), "target not in the snapshot");
        Rejected(With(rich, o => o["units"][0]["type"] = "Dragon"), "unknown unit type");
        Rejected(With(rich, o => o["units"][0]["owner"] = "player3"), "owner");
        Rejected(With(rich, o => o["units"][0]["state"] = "Sleeping"), "state");
        Rejected(With(rich, o => o["strikes"][0]["kind"] = "meteor"), "strike kind");
        Rejected(With(rich, o => o["strikes"][0]["delay"] = 61), "delay 61");
        Rejected(With(rich, o => o["mana1"] = 1001), "mana 1001");
        Rejected(With(rich, o => o["tick"] = -1), "negative tick");
        Rejected(With(rich, o => o["tick"] = 3000000000L), "tick over int");
        Rejected(With(rich, o => o["matchTimeRemaining"] = "180"), "time as a string");
        Rejected(With(rich, o => o["match_id"] = ""), "empty match id");
        Rejected(With(rich, o => o["units"][0]["along"] = 11), "along 11");
        Rejected(rich.ToString().Replace("\"mana2\":", "\"mana2\":NaN,\"x\":").Replace("mana2", "mana2"), "NaN literal");
        Rejected("{\"match_id\":\"a\",\"tick\":1,\"matchTimeRemaining\":Infinity}", "Infinity literal");
        Rejected(With(rich, o => o["towerHP1"] = true), "bool as a number");

        var dated = With(rich, o => o["match_id"] = "2026-11-01T00:00:00Z");
        Check(EngineMessages.TryParseState(dated, out var datedState, out _) && datedState.MatchId == "2026-11-01T00:00:00Z", "a match id that looks like a date stays text");

        var match = (JObject)cases["matches"][2];
        Check(!EngineMessages.TryParseMatch(With(match, o => o["phase"] = "dancing"), out _, out _), "unknown phase");
        Check(!EngineMessages.TryParseMatch(With(match, o => o.Remove("glove2")), out _, out _), "missing glove2");
        Check(!EngineMessages.TryParseMatch(With(match, o => o["phone1"] = 1), out _, out _), "phone1 as a number");
        Check(!EngineMessages.TryParseMatch(With(match, o => o["match_id"] = ""), out _, out _), "empty match id outside offline");
        Check(!EngineMessages.TryParseMatch(With(match, o => o["countdown_ms"] = -1), out _, out _), "negative countdown");
        Check(!EngineMessages.TryParseMatch(With(match, o => o["winner"] = "everyone"), out _, out _), "winner");
        Check(EngineMessages.TryParseMatch(With(match, o => o["reason"] = new string('x', 200)), out var clipped, out _) && clipped.Reason.Length == 80, "long reason is clipped");

        var good = (JObject)cases["events"][0];
        Check(!EngineMessages.TryParseEvent(With(good, o => o["ok"] = false), out _, out _), "refused event without a reason");
        Check(!EngineMessages.TryParseEvent(With(good, o => o["reason"] = "no_mana"), out _, out _), "accepted event with a reason");
        Check(!EngineMessages.TryParseEvent(With(good, o => o["reason"] = "tired"), out _, out _), "unknown reason");
        Check(!EngineMessages.TryParseEvent(With(good, o => o["player"] = 3), out _, out _), "player 3");
        Check(!EngineMessages.TryParseEvent(With(good, o => o["what"] = "Dragon"), out _, out _), "unknown what");
        Check(!EngineMessages.TryParseEvent(With(good, o => o.Remove("unit_id")), out _, out _), "missing unit_id");

        // what this side builds, for check_built.py
        var built = new JObject
        {
            ["lanes"] = new JArray(
                EngineMessages.BuildLane("e1-1", true, 2.1f, true, 0.02f, -0.1f, false, true, 0.3f, 0.05f),
                EngineMessages.BuildLane("", false, 9f, false, 9f, 9f, false, true, 9f, 9f),
                EngineMessages.BuildLane("e1-1", true, 2.1f, true, 0.02f, -0.1f, true, false, 9f, 9f)),
            ["requests"] = new JArray(
                EngineMessages.BuildRequest("e1-1", 7, "start", "", "", false, 0f, 0f),
                EngineMessages.BuildRequest("e1-1", 8, "spawn", "Warrior", "", true, 0.1f, 0.2f),
                EngineMessages.BuildRequest("e1-1", 9, "spell", "", "fireball", true, 0.5f, 0f),
                EngineMessages.BuildRequest("e1-1", 10, "rematch", "", "", false, 9f, 9f)),
        };
        // a flag that is false must send zeros, not the stale numbers the caller still holds
        var off = JObject.Parse((string)built["lanes"][2]);
        Check((float)off["aimAlong"] == 0f && (float)off["aimLat"] == 0f, "aim sent although hasAim is false");
        var none = JObject.Parse((string)built["lanes"][1]);
        Check((float)none["along"] == 0f && (float)none["lane_length_m"] == 0f, "position or lane sent although the flags are false");
        var noAim = JObject.Parse((string)built["requests"][3]);
        Check((float)noAim["along"] == 0f && (float)noAim["lat"] == 0f, "request aim sent although hasAim is false");
        File.WriteAllText(args[1], built.ToString());

        Console.WriteLine($"{accepted} real states accepted ({units} units, {strikes} strikes); {failures} failures");
        failures += LinkChecks.Run();
        failures += TutorialChecks.Run();
        failures += PeerLaneChecks.Run();
        failures += GodModeChecks.Run();
        failures += ShakeChecks.Run();
        failures += FrameRateChecks.Run();
        failures += SummonLatencyChecks.Run();
        return failures == 0 ? 0 : 1;
    }
}
