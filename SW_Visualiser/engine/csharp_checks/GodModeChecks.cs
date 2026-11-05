using System.Linq;
using BridgeRush.Networking;

// Checks when EngineLinkCore asks the board for God mode. It asks once per match and again after a reconnect, and only
// once God mode was switched on or off. It never asks before a match or a player is known, or while the link is down.
static class GodModeChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; System.Console.WriteLine("FAIL: god mode: " + what); } }

    static EngineMatchInfo Match(string id) => new EngineMatchInfo { MatchId = id, Phase = EnginePhase.Running };

    static EngineLinkCore Connected(int player)
    {
        var core = new EngineLinkCore { Player = player };
        core.SetConnected(true, 0f);
        return core;
    }

    public static int Run()
    {
        var quiet = Connected(1);
        quiet.OnMatch(Match("m1"), 0f);
        quiet.Tick(0f);
        Check(quiet.TakeOutgoing().Count == 0, "nothing is sent unless God mode was asked for");

        var core = Connected(2);
        core.WantGodMode(true);
        core.Tick(0f);
        Check(core.TakeOutgoing().Count == 0, "nothing is sent before a game/match");

        core.OnMatch(Match("m1"), 1f);
        core.Tick(1f);
        var sent = core.TakeOutgoing();
        Check(sent.Count == 1 && sent[0].Action == "god_on" && sent[0].MatchId == "m1" && sent[0].UnitType == "" && sent[0].Spell == "" && !sent[0].HasAim, "god_on for the match");
        core.Tick(2f);
        Check(core.TakeOutgoing().Count == 0, "sent once per match");

        core.OnMatch(Match("m1"), 2.5f);
        core.Tick(3f);
        Check(core.TakeOutgoing().Count == 0, "the same match again sends nothing");

        core.OnMatch(Match("m2"), 4f);
        core.Tick(4f);
        sent = core.TakeOutgoing();
        Check(sent.Count == 1 && sent[0].Action == "god_on" && sent[0].MatchId == "m2", "a new match is asked again, since it starts without God mode");

        core.WantGodMode(false);
        core.Tick(5f);
        sent = core.TakeOutgoing();
        Check(sent.Count == 1 && sent[0].Action == "god_off" && sent[0].MatchId == "m2", "switching off sends god_off");
        core.Tick(6f);
        Check(core.TakeOutgoing().Count == 0, "and only once");

        var ids = new[] { core.TakeOutgoing().Count };
        core.WantGodMode(true);
        core.Tick(7f);
        core.WantGodMode(false);
        core.Tick(8f);
        var two = core.TakeOutgoing();
        Check(two.Count == 2 && two[0].ReqId != two[1].ReqId, "each request has its own req_id");

        var nobody = Connected(0);
        nobody.WantGodMode(true);
        nobody.OnMatch(Match("m1"), 0f);
        nobody.Tick(0f);
        Check(nobody.TakeOutgoing().Count == 0, "nothing is sent before the player is known");

        var link = Connected(1);
        link.OnMatch(Match("m1"), 0f);
        link.SetConnected(false, 1f);
        link.WantGodMode(true);
        link.Tick(1f);
        Check(link.TakeOutgoing().Count == 0, "nothing is sent while the link is down, since it would be dropped");
        link.SetConnected(true, 2f);
        link.Tick(2f);
        sent = link.TakeOutgoing();
        Check(sent.Count == 1 && sent[0].Action == "god_on" && sent[0].MatchId == "m1", "sent once the link is back");

        link.SetConnected(false, 3f);
        link.Tick(3f);
        link.SetConnected(true, 4f);
        link.Tick(4f);
        var again = link.TakeOutgoing();
        Check(again.Count == 1 && again[0].Action == "god_on" && again[0].ReqId != sent[0].ReqId,
              "sent again after a reconnect, since the last one may have been lost");
        link.Tick(5f);
        Check(link.TakeOutgoing().Count == 0, "and only once per reconnect");

        System.Console.WriteLine($"god mode checks: {failures} failures");
        return failures;
    }
}
