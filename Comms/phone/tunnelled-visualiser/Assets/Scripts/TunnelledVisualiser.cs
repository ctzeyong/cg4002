using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using MQTTnet;
using MQTTnet.Client;
using MQTTnet.Protocol;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>What runs through the tunnel. Probe is a test with no broker; Mqtt is normal use.</summary>
    public enum PayloadMode
    {
        /// <summary>Line echo against ultra96/tunnel_probe_server.py. Needs no broker on the board.</summary>
        Probe,

        /// <summary>MQTTnet to Mosquitto on the board's loopback.</summary>
        Mqtt
    }

    /// <summary>
    /// Phone visualiser for configuration B (broker on the Ultra96). Opens a two-hop SSH
    /// tunnel to the board, then runs MQTT through it: subscribes to game/gesture/+, drops
    /// repeated event_ids, and shows each event.
    ///
    /// Two supervised loops, one inside the other: the tunnel (RunAsync) and MQTT
    /// (RunMqttSessionsAsync). MQTT runs only while the tunnel is up, and is always stopped
    /// before the tunnel is closed. Why: notes/tunnelled_visualiser.md.
    /// </summary>
    public class TunnelledVisualiser : MonoBehaviour
    {
        public const int MaxLogLines = 200;
        private const int ProbeIntervalSeconds = 20;
        private const int ProbeTimeoutMs = 5000;
        private const int TunnelPollMs = 500;

        public TunnelConfig Config { get; private set; } = new TunnelConfig();
        public PayloadMode Mode = PayloadMode.Mqtt;

        /// <summary>
        /// Secrets typed at Connect. Kept in memory (never saved) because every reconnect
        /// needs the jump-host password again.
        /// </summary>
        public string JumpPassword = "";
        public string JumpPassphrase = "";
        public string BoardPassword = "";
        public string BoardPassphrase = "";
        public bool AllowUnknownHostKeysOnce;

        public string TunnelStatus { get; private set; } = "idle";
        public string MqttStatus { get; private set; } = "idle";

        public int TunnelSessions { get; private set; }
        public int TunnelRetries { get; private set; }
        public int Received { get; private set; }
        public int Duplicates { get; private set; }
        public int BadEnvelope { get; private set; }
        public int MqttSessions { get; private set; }
        public int MqttReconnects { get; private set; }
        public int ProbesOk { get; private set; }

        public bool IsRunning => _cts != null;

        /// <summary>Newest last. Main thread only; the UI renders it directly.</summary>
        public readonly List<string> Log = new List<string>();

        /// <summary>Bumped per line. The UI watches this, not Log.Count, which stops moving once full.</summary>
        public int LogVersion { get; private set; }

        // SSH.NET and MQTTnet call back on other threads, but Unity may only be used on the
        // main thread. They queue work here; Update() handles it, so no locks are needed.
        private readonly ConcurrentQueue<string> _pendingLog = new ConcurrentQueue<string>();
        private readonly ConcurrentQueue<(string Topic, string Payload)> _incoming =
            new ConcurrentQueue<(string, string)>();

        private readonly SeenEvents _seen = new SeenEvents();
        private readonly string _session = Guid.NewGuid().ToString("N").Substring(0, 8);

        private CancellationTokenSource _cts;
        private int _seq;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Bootstrap()
        {
            // Create the visualiser and its UI once, automatically, when the scene loads.
            if (FindAnyObjectByType<TunnelledVisualiser>() != null) return;

            var go = new GameObject("TunnelledVisualiser");
            DontDestroyOnLoad(go);
            go.AddComponent<TunnelledVisualiser>();
            go.AddComponent<TunnelledUI>();
        }

        private void Awake()
        {
            // Keep running with the screen on: if Android suspends the app, both links drop.
            Application.runInBackground = true;
            Screen.sleepTimeout = SleepTimeout.NeverSleep;
        }

        private void Start()
        {
            Config = TunnelConfig.Load();

            // No auto-connect: the jump-host password has to be typed first.
            AddLine($"key directory: {TunnelConfig.KeyDirectory}");
            AddLine("fill in the hops, then press Connect.");
        }

        /// <summary>Save the settings and start the tunnel supervisor (stopping any old one).</summary>
        public void Connect()
        {
            Disconnect();
            Config.Save();
            _cts = new CancellationTokenSource();
            _ = RunAsync(_cts.Token);
        }

        public void Disconnect()
        {
            // Cancel but do not Dispose the token: the loops may still be waiting on it.
            _cts?.Cancel();
            _cts = null;
            TunnelStatus = "stopped";
            MqttStatus = "idle";
        }

        private void OnDestroy() => Disconnect();

        private void OnApplicationQuit() => Disconnect();

        private void Update()
        {
            // Main thread: handle the log lines and MQTT messages queued by other threads.
            while (_pendingLog.TryDequeue(out var line)) AddLine(line);
            while (_incoming.TryDequeue(out var message)) Handle(message.Topic, message.Payload);
        }

        /// <summary>
        /// Outer loop: open the tunnel, run MQTT inside it until the tunnel drops, then retry.
        /// Wait doubles after a short run (2 s up to 60 s) and resets after a healthy one.
        /// </summary>
        private async Task RunAsync(CancellationToken ct)
        {
            var backoff = MqttConfig.MinBackoffSeconds;

            while (!ct.IsCancellationRequested)
            {
                var started = DateTime.UtcNow;
                var tunnel = new SshMqttTunnel();

                try
                {
                    TunnelStatus = "connecting";
                    var settings = Config.ToTunnelSettings(
                        JumpPassword, JumpPassphrase, BoardPassword, BoardPassphrase,
                        AllowUnknownHostKeysOnce);

                    await tunnel.StartAsync(settings, EnqueueLog, ct);
                    TunnelSessions++;
                    TunnelStatus = $"up: 127.0.0.1:{settings.PayloadLocalPort} -> board {settings.PayloadRemotePort}";

                    using (var linked = CancellationTokenSource.CreateLinkedTokenSource(ct))
                    {
                        var payload = RunPayloadAsync(tunnel, settings.PayloadLocalPort, linked.Token);
                        try
                        {
                            await WatchTunnelAsync(tunnel, linked.Token);
                        }
                        finally
                        {
                            // Stop MQTT and wait for it to finish before the tunnel is closed below.
                            linked.Cancel();
                            try { await payload; }
                            catch (OperationCanceledException) { }
                        }
                    }

                    if (!ct.IsCancellationRequested) TunnelStatus = "tunnel dropped";
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception exc)
                {
                    // Skip after Disconnect, so the "stopped" status is not overwritten.
                    if (!ct.IsCancellationRequested)
                    {
                        TunnelStatus = $"tunnel failed: {exc.Message}";
                        EnqueueLog($"TUNNEL_FAILED: {exc.GetType().Name}: {exc.Message}");
                    }
                }
                finally
                {
                    // StopAsync, not Dispose: the SSH disconnect blocks, so run it off the
                    // main thread or the screen freezes.
                    try { await tunnel.StopAsync(); }
                    catch (Exception) { /* Dispose already swallows per-object failures. */ }
                }

                if (ct.IsCancellationRequested) return;
                TunnelRetries++;

                var elapsed = (DateTime.UtcNow - started).TotalSeconds;
                if (elapsed >= MqttConfig.GoodRunSeconds) backoff = MqttConfig.MinBackoffSeconds;

                TunnelStatus += $" -- retrying in {backoff:0}s";
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(backoff), ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }

                if (elapsed < MqttConfig.GoodRunSeconds)
                    backoff = Math.Min(backoff * 2f, MqttConfig.MaxBackoffSeconds);
            }
        }

        /// <summary>
        /// Wait until the tunnel stops being ready. SSH.NET has no event for this, so
        /// IsReady is checked every 500 ms.
        /// </summary>
        private static async Task WatchTunnelAsync(SshMqttTunnel tunnel, CancellationToken ct)
        {
            while (!ct.IsCancellationRequested && tunnel.IsReady)
                await Task.Delay(TunnelPollMs, ct);
        }

        /// <summary>Run whichever payload the Mode selects: the echo test or MQTT.</summary>
        private Task RunPayloadAsync(SshMqttTunnel tunnel, int localPort, CancellationToken ct) =>
            Mode == PayloadMode.Probe
                ? RunProbeLoopAsync(tunnel, localPort, ct)
                : RunMqttSessionsAsync(tunnel, localPort, ct);

        /// <summary>
        /// Test mode: send an echo through the tunnel every 20 s, to check the tunnel stays
        /// usable with no broker on the board.
        /// </summary>
        private async Task RunProbeLoopAsync(SshMqttTunnel tunnel, int localPort, CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    await tunnel.EchoProbeAsync(localPort, ProbeTimeoutMs, ct);
                    ProbesOk++;
                    MqttStatus = $"PROBE_OK x{ProbesOk}";
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception exc)
                {
                    MqttStatus = $"probe failed: {exc.Message}";
                    EnqueueLog($"PROBE_FAILED: {exc.GetType().Name}: {exc.Message}");
                }

                await Task.Delay(TimeSpan.FromSeconds(ProbeIntervalSeconds), ct);
            }
        }

        /// <summary>
        /// Inner loop: connect MQTT through the tunnel and retry with backoff. A new client
        /// each time, because the old one's socket may belong to a closed tunnel.
        /// </summary>
        private async Task RunMqttSessionsAsync(SshMqttTunnel tunnel, int localPort, CancellationToken ct)
        {
            var backoff = MqttConfig.MinBackoffSeconds;

            while (!ct.IsCancellationRequested)
            {
                var started = DateTime.UtcNow;
                var client = new MqttFactory().CreateMqttClient();
                client.ApplicationMessageReceivedAsync += OnMessageReceived;

                try
                {
                    await OneSessionAsync(client, localPort, ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception exc)
                {
                    if (!ct.IsCancellationRequested)
                    {
                        // Tunnel still up means the board refused: most likely no broker there.
                        // (MQTTnet calls this "Error while authenticating"; see
                        // notes/tunnelled_visualiser.md.)
                        var detail = tunnel.LastForwardError ?? exc.Message;
                        MqttStatus = tunnel.IsReady
                            ? $"no broker on the board? ({detail})"
                            : $"session failed: {exc.Message}";

                        // Log it too: the status line only ever shows the latest failure.
                        EnqueueLog($"MQTT_SESSION_FAILED: {exc.GetType().Name}: {exc.Message}" +
                                   $" (nest {(tunnel.IsReady ? "up" : "down")})");
                    }
                }
                finally
                {
                    client.Dispose();
                }

                if (ct.IsCancellationRequested) return;
                MqttReconnects++;

                var elapsed = (DateTime.UtcNow - started).TotalSeconds;
                if (elapsed >= MqttConfig.GoodRunSeconds) backoff = MqttConfig.MinBackoffSeconds;

                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(backoff), ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }

                if (elapsed < MqttConfig.GoodRunSeconds)
                    backoff = Math.Min(backoff * 2f, MqttConfig.MaxBackoffSeconds);
            }
        }

        /// <summary>
        /// One MQTT connection through the tunnel: CONNECT (clean session), SUBSCRIBE to
        /// game/gesture/+ at QoS 1, announce ready, then wait until it drops.
        /// Same topics, QoS and envelope as the LAN client.
        /// </summary>
        private async Task OneSessionAsync(IMqttClient client, int localPort, CancellationToken ct)
        {
            var options = new MqttClientOptionsBuilder()
                .WithTcpServer("127.0.0.1", localPort)
                .WithClientId(MqttConfig.ClientId(Config.Player))
                .WithKeepAlivePeriod(TimeSpan.FromSeconds(MqttConfig.KeepAliveSeconds))
                .WithCleanSession()
                .Build();

            MqttStatus = $"connecting to mqtt://127.0.0.1:{localPort} (tunnelled)";
            await client.ConnectAsync(options, ct);
            MqttSessions++;
            MqttStatus = $"connected as {MqttConfig.ClientId(Config.Player)} (tunnelled)";

            try
            {
                var subscription = new MqttClientSubscribeOptionsBuilder()
                    .WithTopicFilter(f => f
                        .WithTopic(MqttConfig.GameGestureFilter)
                        .WithQualityOfServiceLevel(MqttQualityOfServiceLevel.AtLeastOnce))
                    .Build();
                await client.SubscribeAsync(subscription, ct);

                await PublishReadyAsync(client, ct);

                while (client.IsConnected && !ct.IsCancellationRequested)
                    await Task.Delay(500, ct);
            }
            finally
            {
                try { await client.DisconnectAsync(); }
                catch (Exception) { /* Best effort; a clean DISCONNECT frees the client id. */ }
            }
        }

        /// <summary>Publish a "ready" message on viz/ready/&lt;player&gt;. Diagnostic only; nothing on the board needs it.</summary>
        private async Task PublishReadyAsync(IMqttClient client, CancellationToken ct)
        {
            var payload = new JObject { ["status"] = "ready", ["player"] = Config.Player };
            var json = Envelope.Build(payload, _seq++, MqttConfig.ClientId(Config.Player), _session);

            var message = new MqttApplicationMessageBuilder()
                .WithTopic(MqttConfig.VizReadyTopic(Config.Player))
                .WithPayload(json)
                .WithQualityOfServiceLevel(MqttQualityOfServiceLevel.AtLeastOnce)
                .Build();

            await client.PublishAsync(message, ct);
        }

        /// <summary>Runs on an MQTTnet thread, so it only queues the message for Update().</summary>
        private Task OnMessageReceived(MqttApplicationMessageReceivedEventArgs args)
        {
            var payload = args.ApplicationMessage.ConvertPayloadToString() ?? "";
            _incoming.Enqueue((args.ApplicationMessage.Topic, payload));
            return Task.CompletedTask;
        }

        /// <summary>
        /// Main thread: check the envelope, skip an event_id already shown (QoS 1 can
        /// deliver twice), then show the player and payload.
        /// </summary>
        private void Handle(string topic, string payload)
        {
            JObject envelope;
            try
            {
                envelope = Envelope.Parse(payload);
            }
            catch (EnvelopeException exc)
            {
                // One bad message says nothing about the next one: log it and keep going.
                BadEnvelope++;
                AddLine($"bad envelope on {topic}: {exc.Message}");
                return;
            }

            Received++;
            var eventId = envelope["event_id"].Value<string>();
            if (!_seen.Check(eventId))
            {
                Duplicates++;
                return;
            }

            var player = MqttConfig.PlayerFromTopic(topic);
            AddLine($"player={player?.ToString() ?? "?"} {eventId} " +
                    $"{envelope["payload"].ToString(Newtonsoft.Json.Formatting.None)}");
        }

        /// <summary>Prefix on every copied log line, so it can be searched for in `adb logcat -s Unity`.</summary>
        private const string LogTag = "[tunnelled]";

        /// <summary>Add a timestamped line to the on-screen log. Main thread only.</summary>
        public void AddLine(string line)
        {
            var stamped = $"{DateTime.Now:HH:mm:ss} {line}";

            Log.Add(stamped);
            if (Log.Count > MaxLogLines) Log.RemoveAt(0);
            LogVersion++;

            // Also copy to Debug.Log so the log can be read off the phone with adb logcat.
            // Safe: no password or private key is ever passed to AddLine.
            Debug.Log($"{LogTag} {stamped}");
        }

        /// <summary>Called from SSH.NET's threads; Update() adds the line on the main thread.</summary>
        private void EnqueueLog(string line) => _pendingLog.Enqueue(line);
    }
}
