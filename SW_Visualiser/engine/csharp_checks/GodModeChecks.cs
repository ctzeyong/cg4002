using System.Linq;
using BridgeRush.Networking;

// Checks when EngineLinkCore asks the board for God mode: once per match, only after it was asked for, never before a
// match or a player is known.
static class GodModeChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; System.Console.WriteLine("FAIL: god mode: " + what); } }

    static EngineMatchInfo Match(string id) => new EngineMatchInfo { MatchId = id, Phase = EnginePhase.Running };

    public static int Run()
    {
        var quiet = new EngineLinkCore { Player = 1 };
        quiet.OnMatch(Match("m1"), 0f);
        quiet.Tick(0f);
        Check(quiet.TakeOutgoing().Count == 0, "nothing is sent unless God mode was asked for");

        var core = new EngineLinkCore { Player = 2 };
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

        var nobody = new EngineLinkCore();
        nobody.WantGodMode(true);
        nobody.OnMatch(Match("m1"), 0f);
        nobody.Tick(0f);
        Check(nobody.TakeOutgoing().Count == 0, "nothing is sent before the player is known");

        System.Console.WriteLine($"god mode checks: {failures} failures");
        return failures;
    }
}
