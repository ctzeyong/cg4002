using System;
using Comms.Visualiser;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Tracking;

namespace BridgeRush.Networking
{
    // The phone's end of the board engine (engine_spec.md section 6.1). While active it streams this phone's tower
    // position and crosshair on viz/lane/{player}, sends card taps, START and rematch on game/request/{player}, and reads
    // game/match, game/event and game/state. All decisions live in EngineLinkCore; this only feeds it and publishes.
    //
    // Inert until Activate(): MqttConnection keeps one handler per topic, so taking over game/state while MatchNetworkSync
    // still has it would replace that handler. Activate only in the board-client role.
    public class EngineLink : MonoBehaviour
    {
        const string MatchTopic = "game/match";
        const string EventTopic = "game/event";
        const string StateTopic = "game/state";

        [SerializeField] private MqttConnection mqtt;
        [SerializeField] private MatchSetup matchSetup;
        [SerializeField] private MatchState matchState;
        [SerializeField] private PeerReadySync peerReady;
        // optional, found by type when left empty
        [SerializeField] private CommsTunnelBridge bridge;
        [SerializeField] private float laneHz = 10f;

        readonly EngineLinkCore core = new();
        float nextLane;

        public bool Active { get; private set; }
        public EngineLinkCore Core => core;
        public EngineMatchInfo Match => core.Match;
        public EngineLinkState LinkState => core.State(Now);
        public bool EngineIsOffline => core.Offline;
        // This phone has locked in, so it has a frame to send positions from. True when no PeerReadySync is wired.
        public bool IsLockedIn => peerReady == null || peerReady.HasLockedIn;

        public event Action<string> MatchChanged { add => core.MatchChanged += value; remove => core.MatchChanged -= value; }
        public event Action EngineOffline { add => core.EngineOffline += value; remove => core.EngineOffline -= value; }
        public event Action<EngineMatchInfo> MatchUpdated { add => core.MatchUpdated += value; remove => core.MatchUpdated -= value; }
        public event Action<EngineEvent> ActionResult { add => core.ActionResult += value; remove => core.ActionResult -= value; }
        public event Action<int> ActionUnknown { add => core.ActionUnknown += value; remove => core.ActionUnknown -= value; }

        static float Now => Time.realtimeSinceStartup;

        void Awake()
        {
            if (mqtt == null || matchSetup == null || matchState == null)
            {
                Debug.LogError($"{nameof(EngineLink)}: needs an Mqtt Connection, a Match Setup and a Match State.", this);
                enabled = false;
            }
        }

        public void Activate()
        {
            if (Active) return;
            Active = true;
            mqtt.Subscribe(MatchTopic, OnMatchMessage, 1);
            mqtt.Subscribe(EventTopic, OnEventMessage, 1);
            mqtt.Subscribe(StateTopic, OnStateMessage, 0);
        }

        public void Deactivate()
        {
            if (!Active) return;
            Active = false;
            mqtt.Unsubscribe(MatchTopic);
            mqtt.Unsubscribe(EventTopic);
            mqtt.Unsubscribe(StateTopic);
        }

        void OnDestroy() => Deactivate();

        // --- what the game calls ----------------------------------------------------------------------------------

        public bool PressStart() => Active && core.BeginPress("start", Now);
        public bool PressRematch() => Active && core.BeginPress("rematch", Now);

        // Returns the req_id, or -1 when nothing could be sent (inactive, or no game/match heard yet).
        public int SendSummon(UnitType type)
        {
            if (!Active) return -1;
            bool aim = matchState.TryGetAimLanePosition(out float along, out float lat);
            return core.SendTap("spawn", type.ToString(), null, aim, along, lat, Now);
        }

        public int SendSpell(string spell)
        {
            if (!Active) return -1;
            bool aim = matchState.TryGetAimLanePosition(out float along, out float lat);
            return core.SendTap("spell", null, spell, aim, along, lat, Now);
        }

        // The latest checked snapshot if it is newer than the one the caller last applied (by arrival, not by tick).
        public bool TryTakeState(ref int lastApplied, out EngineState state)
        {
            state = core.LatestState;
            if (state == null || core.StateCounter == lastApplied) return false;
            lastApplied = core.StateCounter;
            return true;
        }

        // --- messages in ------------------------------------------------------------------------------------------

        void OnMatchMessage(string topic, string json)
        {
            if (!EngineMessages.TryParseMatch(json, out var info, out string error))
            {
                Debug.LogWarning($"{nameof(EngineLink)}: bad game/match, ignored: {error}");
                return;
            }
            core.OnMatch(info, Now);
        }

        void OnEventMessage(string topic, string json)
        {
            if (!EngineMessages.TryParseEvent(json, out var evt, out string error))
            {
                Debug.LogWarning($"{nameof(EngineLink)}: bad game/event, ignored: {error}");
                return;
            }
            if (evt.Input == "glove" && evt.Player == core.Player)
                Debug.Log($"{nameof(EngineLink)}: result for glove event {evt.GestureEventId}: {(evt.Ok ? "ok" : evt.Reason)} ({evt.What})");
            core.OnEvent(evt);
        }

        void OnStateMessage(string topic, string json)
        {
            if (!EngineMessages.TryParseState(json, out var state, out string error))
            {
                Debug.LogWarning($"{nameof(EngineLink)}: bad game/state, ignored: {error}");
                return;
            }
            core.OnState(state, Now);
        }

        // --- every frame ------------------------------------------------------------------------------------------

        void Update()
        {
            if (!Active) return;
            core.Player = matchSetup.Selected == MatchSetup.SelectedPlayer.Player1 ? 1
                        : matchSetup.Selected == MatchSetup.SelectedPlayer.Player2 ? 2 : 0;
            core.SetConnected(mqtt.IsConnected, Now);
            core.Tick(Now);

            if (core.TakeReconnectRequest()) ReconnectMqtt();

            foreach (var request in core.TakeOutgoing()) PublishRequest(request);
            PublishLane();
        }

        // Drop MQTT and connect again. Leave the reconnect to the tunnel when it is running.
        void ReconnectMqtt()
        {
            mqtt.Disconnect();
            if (bridge == null) bridge = FindAnyObjectByType<CommsTunnelBridge>();
            if (bridge != null && bridge.IsRunning)
            {
                Debug.LogWarning($"{nameof(EngineLink)}: no snapshot from the engine, the tunnel will reconnect MQTT.");
                return;
            }
            Debug.LogWarning($"{nameof(EngineLink)}: no snapshot from the engine, reconnecting MQTT.");
            mqtt.Connect();
        }

        void PublishRequest(EngineOutgoing r)
        {
            if (core.Player == 0) return;
            string json = EngineMessages.BuildRequest(r.MatchId, r.ReqId, r.Action, r.UnitType, r.Spell, r.HasAim, r.Along, r.Lat);
            mqtt.Publish($"game/request/{core.Player}", json, false, 1);
        }

        // About laneHz times a second once locked in, in every phase (the rematch gate needs it after the match ends).
        // Position and aim only once this match's frame exists; before that only the lane length.
        void PublishLane()
        {
            if (core.Player == 0 || !mqtt.IsConnected || Time.unscaledTime < nextLane) return;
            if (peerReady != null && !peerReady.HasLockedIn) return;
            nextLane = Time.unscaledTime + 1f / Mathf.Max(1f, laneHz);

            float aimAlong = 0f, aimLat = 0f;
            bool hasPosition = matchState.TryGetOwnLanePosition(out float along, out float lat);
            bool hasAim = hasPosition && matchState.TryGetAimLanePosition(out aimAlong, out aimLat);
            string json = EngineMessages.BuildLane(core.MatchId, matchState.HasLane, matchState.LaneLengthMeters, hasPosition,
                along, lat, matchState.OwnTowerLocked, hasAim, aimAlong, aimLat);
            mqtt.Publish($"viz/lane/{core.Player}", json, false, 0);
        }
    }
}
