using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Threading;
using System.Threading.Tasks;
using MQTTnet;
using MQTTnet.Client;
using MQTTnet.Protocol;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// The phone half of criterion 2: subscribes to the laptop's gesture events and
    /// announces itself on viz/ready. A bare scrolling message log, which is all the
    /// rubric asks for -- see notes/unity_visualiser.md.
    ///
    /// Supervised the same way as every other link in this project: connect, run, back off,
    /// retry, forever. A broker that is not up yet must not make start order matter, which
    /// is what the order-independent bring-up deliverable needs.
    /// </summary>
    public class MqttVisualiser : MonoBehaviour
    {
        public const int MaxLogLines = 200;

        public int Player = 1;
        public string Host;
        public int Port = MqttConfig.PlaintextPort;

        /// <summary>One line for the UI. Assigned from the MQTT task, read on the main thread.</summary>
        public string Status { get; private set; } = "starting";

        public int Received { get; private set; }
        public int Duplicates { get; private set; }
        public int BadEnvelope { get; private set; }
        public int Sessions { get; private set; }
        public int Reconnects { get; private set; }

        /// <summary>Newest last. Main thread only; the UI renders it directly.</summary>
        public readonly List<string> Log = new List<string>();

        /// <summary>
        /// Bumped on every line added. The UI watches this rather than `Log.Count`, which
        /// stops changing once the log is full and would freeze the auto-scroll there.
        /// </summary>
        public int LogVersion { get; private set; }

        // MQTTnet raises its handlers on a thread-pool thread, and nothing in UnityEngine
        // may be touched from there. Received messages cross to the main thread here and
        // are parsed, de-duplicated and rendered in Update, which keeps SeenEvents and the
        // counters single-threaded for the same reason NodeStats is lock-free on the laptop.
        private readonly ConcurrentQueue<(string Topic, string Payload)> _incoming =
            new ConcurrentQueue<(string, string)>();

        /// <summary>
        /// Diagnostics raised from the MQTT task for the log. Same crossing as `_incoming`
        /// and for the same reason: AddLine touches `Log`, which is main-thread-only, so a
        /// session task must not call it directly. Drained in Update.
        /// </summary>
        private readonly ConcurrentQueue<string> _notices = new ConcurrentQueue<string>();

        private readonly SeenEvents _seen = new SeenEvents();
        private readonly string _session = Guid.NewGuid().ToString("N").Substring(0, 8);

        private IMqttClient _client;
        private CancellationTokenSource _cts;
        private int _seq;

        // mTLS state. Loaded once and reused across reconnects: re-reading the bundle on
        // every retry would turn a missing file into a slow loop of identical failures.
        private bool _useTls;
        private X509Certificate2 _ca;
        private X509Certificate2 _clientCertificate;

        private string Scheme => _useTls ? "mqtts" : "mqtt";

        /// <summary>
        /// Build the visualiser at startup so it needs no scene edits, matching the
        /// tunnel spike's controller. Stage A then runs by pressing Play.
        /// </summary>
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Bootstrap()
        {
            var go = new GameObject("MqttVisualiser");
            DontDestroyOnLoad(go);
            go.AddComponent<MqttVisualiser>();
            go.AddComponent<VisualiserUI>();
        }

        private void Start()
        {
            // Main thread, before anything reads a certificate path: Application
            // .persistentDataPath cannot be touched from the session task that loads them.
            MqttTls.CacheDataPath();

            if (string.IsNullOrEmpty(Host)) Host = MqttConfig.LoadHost();

            Connect();
        }

        /// <summary>Restart the supervisor, picking up a host the UI has just changed.
        ///
        /// The client is passed to RunAsync rather than read back out of `_client`: the
        /// session being replaced is still unwinding and would find the replacement
        /// there and disconnect it. See notes/unity_visualiser.md.
        /// </summary>
        public void Connect()
        {
            Disconnect();

            // The presence of the pushed material is the switch: `adb push` turns mTLS on,
            // deleting the files turns it off. That keeps the plaintext path usable while the
            // phone leg is being proved -- notes/mtls.md holds the plaintext listener open
            // until Stage B passes, because a broken phone leg and a broken handshake are
            // indistinguishable from the phone.
            //
            // Decided HERE rather than in Start so that pushing certificates to a running app
            // and pressing Connect is enough. Deciding it once at startup would mean the
            // material had to be in place before launch -- an ordering dependency, and
            // order-independent bring-up is a Week 7 deliverable.
            //
            // A runtime switch rather than a second copy of this file: the
            // separate-file-per-configuration rule is about A and B, which coexist
            // permanently, whereas plaintext here is scaffolding with an end date.
            _useTls = MqttTls.Available;

            // Dropped so replaced material is picked up rather than a stale cached identity.
            _ca = null;
            _clientCertificate = null;

            // Follow the scheme unless somebody deliberately pinned a port in the Inspector.
            if (_useTls && Port == MqttConfig.PlaintextPort) Port = MqttConfig.TlsPort;
            if (!_useTls && Port == MqttConfig.TlsPort) Port = MqttConfig.PlaintextPort;

            AddLine(_useTls ? "mTLS: material found" : "mTLS: off (no material pushed)");
            AddLine(MqttTls.Describe());

            _cts = new CancellationTokenSource();
            var client = new MqttFactory().CreateMqttClient();
            client.ApplicationMessageReceivedAsync += OnMessageReceived;
            _client = client;
            _ = RunAsync(client, _cts.Token);
        }

        public void Disconnect()
        {
            // Cancel but do not Dispose: the session task may still be observing this
            // token, and disposing it out from under a live `Task.Delay(..., ct)` throws
            // ObjectDisposedException from somewhere unrelated. Nothing here holds a timer,
            // so letting it be collected costs nothing.
            _cts?.Cancel();
            _cts = null;
            _client?.Dispose();
            _client = null;
        }

        private void OnDestroy() => Disconnect();

        private Task OnMessageReceived(MqttApplicationMessageReceivedEventArgs args)
        {
            var payload = args.ApplicationMessage.ConvertPayloadToString() ?? "";
            _incoming.Enqueue((args.ApplicationMessage.Topic, payload));
            return Task.CompletedTask;
        }

        private void Update()
        {
            while (_notices.TryDequeue(out var notice)) AddLine(notice);
            while (_incoming.TryDequeue(out var message)) Handle(message.Topic, message.Payload);
        }

        /// <summary>Queue a log line from any thread. Rendered on the next Update.</summary>
        private void Note(string line) => _notices.Enqueue(line);

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
            AddLine($"player={player?.ToString() ?? "?"} {eventId} {envelope["payload"].ToString(Newtonsoft.Json.Formatting.None)}");
        }

        /// <summary>Prefix on every copied log line, so it can be searched for in `adb logcat -s Unity`.</summary>
        private const string LogTag = "[demo]";

        public void AddLine(string line)
        {
            var stamped = $"{DateTime.Now:HH:mm:ss} {line}";

            Log.Add(stamped);
            if (Log.Count > MaxLogLines) Log.RemoveAt(0);
            LogVersion++;

            // Also copy to Debug.Log, as tunnelled-visualiser does: logcat's own timestamp on
            // this line is hop 7 of the latency measurement (notes/latency.md 3.2).
            Debug.Log($"{LogTag} {stamped}");
        }

        private async Task RunAsync(IMqttClient client, CancellationToken ct)
        {
            var backoff = MqttConfig.MinBackoffSeconds;

            while (!ct.IsCancellationRequested)
            {
                var started = DateTime.UtcNow;
                try
                {
                    await OneSessionAsync(client, ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception exc)
                {
                    // Guarded so a replaced session cannot paint over its successor.
                    if (!ct.IsCancellationRequested) Status = $"session failed: {exc.Message}";
                }

                if (ct.IsCancellationRequested) return;
                Reconnects++;

                var elapsed = (DateTime.UtcNow - started).TotalSeconds;
                if (elapsed >= MqttConfig.GoodRunSeconds) backoff = MqttConfig.MinBackoffSeconds;

                Status = $"reconnecting in {backoff:0}s";
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
        /// Hold one broker connection until the link drops or we are cancelled. Subscribes
        /// before announcing readiness so no event can arrive on a topic we have not asked for.
        /// </summary>
        private async Task OneSessionAsync(IMqttClient client, CancellationToken ct)
        {
            EnsureTlsMaterial();

            var builder = new MqttClientOptionsBuilder()
                .WithTcpServer(Host, Port)
                .WithClientId(MqttConfig.ClientId(Player))
                .WithKeepAlivePeriod(TimeSpan.FromSeconds(MqttConfig.KeepAliveSeconds))
                .WithCleanSession();

            if (_useTls)
            {
                // Captured locally so this session's TLS settings cannot observe fields
                // cleared by a Disconnect racing it -- the same hazard the `client`
                // parameter exists to avoid, noted in Connect(). The validation handler is
                // the one that genuinely outlives this method, so the capture is load-bearing
                // there rather than merely tidy.
                var pinnedCa = _ca;
                var identity = _clientCertificate;

                builder = builder.WithTlsOptions(tls => tls
                    // X509Certificate2, not X509Certificate: MQTTnet 4.3.7's overload is
                    // IEnumerable<X509Certificate2>, and the base type does not convert.
                    .WithClientCertificates(new List<X509Certificate2> { identity })
                    // Pinned to match `tls_version tlsv1.2` in the broker fragment. Both ends
                    // naming the same version turns a mismatch into a clear refusal rather
                    // than a negotiated surprise on one Android version and not another.
                    .WithSslProtocols(SslProtocols.Tls12)
                    .WithCertificateValidationHandler(context =>
                        MqttTls.IsOurBroker(context.Certificate, context.SslPolicyErrors, pinnedCa)));
            }

            var options = builder.Build();

            Status = $"connecting to {Scheme}://{Host}:{Port}";
            await client.ConnectAsync(options, ct);
            Sessions++;
            Status = $"connected to {Scheme}://{Host}:{Port} as {MqttConfig.ClientId(Player)}";

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
                // Best effort, and on `client` so a replaced session says goodbye to its
                // own connection. A clean DISCONNECT stops the broker holding the id.
                try { await client.DisconnectAsync(); }
                catch (Exception) { }
            }
        }

        /// <summary>
        /// Load this phone's identity and the pinned CA, once, on first use.
        ///
        /// Deliberately allowed to throw. A failure here is not a network problem and must
        /// not be retried silently into something that looks like one: the supervisor turns
        /// it into `session failed: <named cause>` on screen, which on a phone is the only
        /// place anyone can read it. Same reasoning as mqtt_config.tls_params() raising a
        /// FileNotFoundError that names the files instead of letting the handshake fail.
        /// </summary>
        private void EnsureTlsMaterial()
        {
            if (!_useTls || _clientCertificate != null) return;

            _ca = MqttTls.LoadCa();
            _clientCertificate = MqttTls.LoadClientCertificate(out var bundle, out var attempts);

            // Which bundle loaded is the answer Stage A exists to find: it decides what gets
            // pushed to the phone, and the modern one is not guaranteed to work under Mono.
            Note($"mTLS: identity {_clientCertificate.Subject} (from {bundle})");

            // Rejections that happened *before* the one that worked. Without these a fallback
            // is invisible, and "the legacy bundle loaded" does not say whether the modern one
            // was refused or simply absent.
            foreach (var attempt in attempts) Note($"mTLS: rejected {attempt}");

            Note($"mTLS: pinned CA {_ca.Subject}");
        }

        /// <summary>
        /// Announce this visualiser on viz/ready/{player}. Not required by any rubric line,
        /// but it shows the link is genuinely two-way and it is the evidence behind the
        /// order-independent bring-up claim.
        /// </summary>
        private async Task PublishReadyAsync(IMqttClient client, CancellationToken ct)
        {
            var payload = new JObject { ["status"] = "ready", ["player"] = Player };
            var json = Envelope.Build(payload, _seq++, MqttConfig.ClientId(Player), _session);

            var message = new MqttApplicationMessageBuilder()
                .WithTopic(MqttConfig.VizReadyTopic(Player))
                .WithPayload(json)
                .WithQualityOfServiceLevel(MqttQualityOfServiceLevel.AtLeastOnce)
                .Build();

            await client.PublishAsync(message, ct);
        }
    }
}
