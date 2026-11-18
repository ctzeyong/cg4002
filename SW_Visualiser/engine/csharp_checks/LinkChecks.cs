using System;
using System.Collections.Generic;
using BridgeRush.Networking;

// Checks EngineLinkCore: START re-sends, tap timeouts, link state, the stall watchdog, match changes (engine_spec.md 6.1).
static class LinkChecks
{
    static int failures;
    static void Check(bool ok, string what) { if (!ok) { failures++; Console.WriteLine("FAIL: link: " + what); } }

    static EngineMatchInfo Info(string id, EnginePhase phase, bool start1 = false, bool start2 = false) => new EngineMatchInfo
    {
        MatchId = id, Phase = phase, Reason = "", Winner = "none", TickHz = 10, RulesVersion = "v1",
        Start = new[] { start1, start2 },
    };

    static EngineState State(string id) => new EngineState { MatchId = id };

    static EngineEvent Event(string id, int player, string input, int reqId, bool ok = true) => new EngineEvent
    {
        MatchId = id, Player = player, Input = input, ReqId = reqId, Ok = ok, Reason = ok ? "" : "no_mana", Action = "spawn", What = "Warrior",
        GestureEventId = "", Gesture = "", UnitId = -1,
    };

    static EngineLinkCore Fresh(float now = 0f)
    {
        var c = new EngineLinkCore { Player = 1 };
        c.SetConnected(true, now);
        return c;
    }

    public static int Run()
    {
        // nothing can be named before the first game/match
        var c = Fresh();
        Check(c.SendTap("spawn", "Warrior", null, false, 0, 0, 0) == -1, "a tap before any game/match");
        Check(!c.BeginPress("start", 0), "a press before any game/match");

        // START: sent at once, re-sent with the same req_id every 2 s until game/match lists it
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Waiting), 0);
        Check(c.BeginPress("start", 0), "begin a press");
        var sent = c.TakeOutgoing();
        Check(sent.Count == 1 && sent[0].Action == "start" && sent[0].MatchId == "e-1", "the first START");
        int reqId = sent[0].ReqId;
        c.Tick(1.9f);
        Check(c.TakeOutgoing().Count == 0, "no re-send before 2 s");
        c.Tick(2.0f);
        var resent = c.TakeOutgoing();
        Check(resent.Count == 1 && resent[0].ReqId == reqId, "the re-send keeps the req_id");
        c.Tick(3.9f);
        Check(c.TakeOutgoing().Count == 0, "the next re-send is 2 s after the last");
        c.Tick(4.0f);
        Check(c.TakeOutgoing().Count == 1, "re-sent again");
        c.OnMatch(Info("e-1", EnginePhase.Waiting, start1: true), 4.1f);
        c.Tick(6.5f);
        Check(c.TakeOutgoing().Count == 0 && !c.PressPending, "stops once the engine lists the press");

        // another player's flag does not count as mine
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Waiting, start2: true), 0);
        c.BeginPress("start", 0); c.TakeOutgoing();
        c.Tick(2.1f);
        Check(c.TakeOutgoing().Count == 1, "player 2's START does not stop player 1's re-send");

        // a press that no longer applies stops (the match began)
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Waiting), 0);
        c.BeginPress("start", 0); c.TakeOutgoing();
        c.OnMatch(Info("e-1", EnginePhase.Running), 1);
        c.Tick(3);
        Check(c.TakeOutgoing().Count == 0 && !c.PressPending, "a START stops when the match is running");

        // a new match id while a press is pending: sent again for the new match under a new req_id
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Waiting), 0);
        c.BeginPress("start", 0); int first = c.TakeOutgoing()[0].ReqId;
        c.OnMatch(Info("e-2", EnginePhase.Waiting), 1);
        c.Tick(2.1f);
        var retarget = c.TakeOutgoing();
        Check(retarget.Count == 1 && retarget[0].MatchId == "e-2" && retarget[0].ReqId != first, "re-target on a new match id");

        // rematch only applies in ended
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Ended), 0);
        c.BeginPress("rematch", 0); c.TakeOutgoing();
        c.Tick(2.1f);
        Check(c.TakeOutgoing().Count == 1, "a rematch is re-sent in ended");
        c.OnMatch(Info("e-2", EnginePhase.Waiting, start1: true), 2.2f);
        c.Tick(5);
        Check(!c.PressPending, "a rematch is done when the next match is waiting");

        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        c.BeginPress("rematch", 0); c.TakeOutgoing();
        c.Tick(2.1f);
        Check(c.TakeOutgoing().Count == 0 && !c.PressPending, "a rematch means nothing while the match is running");

        // taps: a result clears it, silence for 2 s is "unknown", never "failed"
        c = Fresh();
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        var unknown = new List<int>(); var results = new List<EngineEvent>();
        c.ActionUnknown += unknown.Add; c.ActionResult += results.Add;
        int a = c.SendTap("spawn", "Warrior", null, true, 0.1f, 0f, 0);
        int b = c.SendTap("spell", null, "lightning", true, 0.5f, 0f, 0);
        var taps = c.TakeOutgoing();
        Check(taps.Count == 2 && taps[0].HasAim && taps[1].Spell == "lightning" && taps[0].ReqId == a, "tap requests");
        c.OnEvent(Event("e-1", 1, "tap", a));
        c.Tick(2.5f);
        Check(results.Count == 1 && results[0].ReqId == a, "the answered tap gives a result");
        Check(unknown.Count == 1 && unknown[0] == b, "the unanswered tap is unknown after 2 s");
        c.Tick(5);
        Check(unknown.Count == 1, "unknown is raised once");
        c.OnEvent(Event("e-1", 1, "tap", b));
        Check(results.Count == 2 && results[1].ReqId == b, "a late result is still shown: it is true information");
        c.OnEvent(Event("e-9", 1, "glove", -1));
        Check(results.Count == 2, "an event of another match is ignored");
        c.OnEvent(Event("e-1", 2, "glove", -1));
        Check(results.Count == 2, "the other player's event is not mine");
        c.OnEvent(Event("e-1", 1, "glove", -1, ok: false));
        Check(results.Count == 3 && !results[2].Ok && results[2].Reason == "no_mana", "my glove result is raised");

        // match changes and the offline will
        c = Fresh();
        var changed = new List<string>(); int offline = 0;
        c.MatchChanged += changed.Add; c.EngineOffline += () => offline++;
        c.OnMatch(Info("e-1", EnginePhase.Waiting), 0);
        c.OnMatch(Info("e-1", EnginePhase.Countdown), 1);
        c.OnMatch(Info("e-2", EnginePhase.Waiting), 2);
        Check(changed.Count == 2 && changed[0] == "e-1" && changed[1] == "e-2", "MatchChanged only on a new id");
        c.OnMatch(new EngineMatchInfo { MatchId = "", Phase = EnginePhase.Offline, Start = new bool[2] }, 3);
        Check(offline == 1 && c.MatchId == "e-2" && c.Match.Phase == EnginePhase.Waiting, "offline keeps the shown match");

        // snapshots: only of the current match, latest kept, counted by arrival
        c = Fresh();
        Check(!c.OnState(State("e-1"), 0), "a snapshot before any game/match");
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        Check(c.OnState(State("e-1"), 0.1f) && c.StateCounter == 1, "a snapshot of the current match");
        Check(!c.OnState(State("e-0"), 0.2f) && c.StateCounter == 1, "a snapshot of another match");
        c.OnState(State("e-1"), 0.3f);
        Check(c.StateCounter == 2, "the counter rises on every snapshot");
        c.OnMatch(Info("e-2", EnginePhase.Waiting), 0.4f);
        Check(c.LatestState == null, "a new match drops the old snapshot");

        // link state
        c = new EngineLinkCore { Player = 1 };
        Check(c.State(0) == EngineLinkState.Disconnected, "not connected");
        c.SetConnected(true, 0);
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        Check(c.State(0.1f) == EngineLinkState.Waiting, "connected, no snapshot");
        c.OnState(State("e-1"), 1.0f);
        Check(c.State(1.9f) == EngineLinkState.Live, "a snapshot within 1 s");
        Check(c.State(2.2f) == EngineLinkState.Stale, "no snapshot for over 1 s");
        c.SetConnected(false, 3);
        Check(c.State(3) == EngineLinkState.Disconnected, "disconnected again");
        Check(c.Match != null && c.MatchId == "e-1", "the last game/match survives a link loss");

        // the stall watchdog: 5 s, then 10, 20, 40, 60; a snapshot resets it; a reconnect restarts the clock
        c = Fresh(0);
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        c.Tick(4.9f);
        Check(!c.TakeReconnectRequest(), "no reconnect before 5 s");
        c.Tick(5.0f);
        Check(c.TakeReconnectRequest(), "reconnect at 5 s of silence");
        Check(!c.TakeReconnectRequest(), "asked for once");
        c.Tick(14.9f);
        Check(!c.TakeReconnectRequest(), "the next wait is 10 s, not 5");
        c.Tick(15.0f);
        Check(c.TakeReconnectRequest(), "the second at +10 s");
        c.Tick(34.9f); Check(!c.TakeReconnectRequest(), "then +20 s");
        c.Tick(35.0f); Check(c.TakeReconnectRequest(), "third request");
        c.Tick(75.0f); Check(c.TakeReconnectRequest(), "+40 s");
        c.Tick(134.9f); Check(!c.TakeReconnectRequest(), "capped at 60, not before");
        c.Tick(135.0f); Check(c.TakeReconnectRequest(), "+60 s");
        c.Tick(195.0f); Check(c.TakeReconnectRequest(), "stays at 60 s");
        c.OnState(State("e-1"), 196);
        c.Tick(200.9f); Check(!c.TakeReconnectRequest(), "a snapshot resets the wait to 5 s");
        c.Tick(201.0f); Check(c.TakeReconnectRequest(), "5 s after the snapshot");

        c = Fresh(0);
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        c.SetConnected(false, 3);
        c.Tick(100);
        Check(!c.TakeReconnectRequest(), "no watchdog while disconnected");
        c.SetConnected(true, 100);
        c.Tick(104.9f);
        Check(!c.TakeReconnectRequest(), "the wait counts from the reconnect");
        c.Tick(105.1f);
        Check(c.TakeReconnectRequest(), "then fires");

        // it runs after `offline` too (R102): a phone that heard the will and then stalls still reconnects
        c = Fresh(0);
        c.OnMatch(Info("e-1", EnginePhase.Running), 0);
        c.OnMatch(new EngineMatchInfo { MatchId = "", Phase = EnginePhase.Offline, Start = new bool[2] }, 1);
        c.Tick(5.1f);
        Check(c.TakeReconnectRequest(), "the watchdog still runs after offline");

        Console.WriteLine($"link checks: {failures} failures");
        return failures;
    }
}
