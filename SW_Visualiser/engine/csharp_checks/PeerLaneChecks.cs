using BridgeRush.Networking;

// Checks the other player's viz/lane as the phone reads it: the parser, and when a position is fresh enough to draw from.
static class PeerLaneChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; System.Console.WriteLine("FAIL: peer lane: " + what); } }

    static EngineMatchInfo Match(string id) => new EngineMatchInfo { MatchId = id, Phase = EnginePhase.Running };

    static EnginePeerLane Lane(string id, float along = 0.97f, float lat = -0.2f, bool locked = false, bool has = true)
    {
        EngineMessages.TryParsePeerLane(EngineMessages.BuildLane(id, true, 2.1f, has, along, lat, locked, false, 0f, 0f), out var lane, out _);
        return lane;
    }

    public static int Run()
    {
        // what this side sends reads back
        var lane = Lane("m1");
        Check(lane != null && lane.MatchId == "m1" && lane.HasPosition && lane.Along == 0.97f && lane.Lat == -0.2f && !lane.Locked, "a built lane reads back");
        Check(Lane("m1", locked: true).Locked, "locked is read");
        Check(!Lane("m1", along: 9f, lat: 9f, has: false).HasPosition, "no position is read as no position");

        // bad ones are rejected whole
        Check(!EngineMessages.TryParsePeerLane("", out _, out _), "empty");
        Check(!EngineMessages.TryParsePeerLane("[]", out _, out _), "an array");
        Check(!EngineMessages.TryParsePeerLane("{\"match_id\":\"m1\",\"hasPosition\":true,\"lat\":0,\"locked\":false}", out _, out _), "missing along");
        Check(!EngineMessages.TryParsePeerLane("{\"match_id\":\"m1\",\"hasPosition\":true,\"along\":11,\"lat\":0,\"locked\":false}", out _, out _), "along 11");
        Check(!EngineMessages.TryParsePeerLane("{\"match_id\":\"m1\",\"hasPosition\":true,\"along\":\"0.5\",\"lat\":0,\"locked\":false}", out _, out _), "along as text");
        Check(!EngineMessages.TryParsePeerLane("{\"match_id\":\"m1\",\"hasPosition\":1,\"along\":0.5,\"lat\":0,\"locked\":false}", out _, out _), "hasPosition as a number");

        var core = new EngineLinkCore();
        float x, y; bool locked;
        Check(!core.OnPeerLane(Lane("m1"), 0f), "a lane before any game/match is refused");
        core.OnMatch(Match("m1"), 0f);
        Check(!core.TryGetPeerTower(0f, out _, out _, out _), "nothing heard yet");
        Check(!core.OnPeerLane(Lane("other"), 1f), "another match's lane is refused");
        Check(core.OnPeerLane(Lane("m1", 0.97f, -0.2f, true), 1f), "this match's lane is taken");
        Check(core.TryGetPeerTower(1.5f, out x, out y, out locked) && x == 0.97f && y == -0.2f && locked, "a fresh position is returned");
        Check(core.TryGetPeerTower(1f + EngineLinkCore.PeerFreshS, out _, out _, out _), "still fresh at the limit");
        Check(!core.TryGetPeerTower(1f + EngineLinkCore.PeerFreshS + 0.01f, out _, out _, out _), "stale after the limit, so the tag is used");

        core.OnPeerLane(Lane("m1", has: false), 5f);
        Check(!core.TryGetPeerTower(5f, out _, out _, out _), "a lane with no position is not used");

        core.OnPeerLane(Lane("m1", 0.5f, 0.1f), 6f);
        Check(core.TryGetPeerTower(6f, out _, out _, out _), "a position again");
        core.OnMatch(Match("m2"), 6.1f);
        Check(!core.TryGetPeerTower(6.1f, out _, out _, out _), "a new match forgets the old position");
        core.OnPeerLane(Lane("m2", 0.4f, 0f), 6.2f);
        Check(core.TryGetPeerTower(6.3f, out x, out _, out _) && x == 0.4f, "the new match's position is used");

        System.Console.WriteLine($"peer lane checks: {failures} failures");
        return failures;
    }
}
