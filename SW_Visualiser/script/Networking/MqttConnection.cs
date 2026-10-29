using System;
using System.Collections.Concurrent;
using System.Threading;
using System.Threading.Tasks;
using Comms.Visualiser;
using MQTTnet;
using MQTTnet.Client;
using MQTTnet.Protocol;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace BridgeRush.Networking
{
    /// <summary>
    /// The one shared MQTT connection, speaking the comms team's contract
    /// (comms/notes/mqtt_topics.md):
    ///  - every message is wrapped in the five-field envelope on the way out, and
    ///    validated by type and de-duplicated on event_id on the way in (QoS 1 redelivers);
    ///  - handlers receive the envelope's inner payload, so callers never see the envelope;
    ///  - the connection is supervised: connect, run, back off, retry, forever, so a broker
    ///    that is not up yet does not make start order matter.
    /// Public API is unchanged from the M2Mqtt version, plus Unsubscribe and a qos argument.
    /// </summary>
    public class MqttConnection : MonoBehaviour
    {
        [Tooltip("Broker address. A host saved from the debug menu (broker_host.txt) overrides this at startup.")]
        [SerializeField] private string brokerHost = "127.0.0.1";
        [SerializeField] private int brokerPort = MqttConfig.PlaintextPort;
        [Tooltip("Connect on Start. Turn off to connect manually from the debug menu.")]
        [SerializeField] private bool connectOnStart = false;

        private readonly struct Subscription
        {
            public readonly Action<string, string> Handler;
            public readonly int Qos;
            public Subscription(Action<string, string> handler, int qos) { Handler = handler; Qos = qos; }
        }

        // MQTTnet raises its handlers on a thread-pool thread and nothing in UnityEngine may
        // be touched from there, so received messages cross to the main thread on this queue
        // and are validated, de-duplicated and dispatched in Update.
        private readonly ConcurrentQueue<(string topic, string payload)> incoming = new();
        private readonly ConcurrentQueue<string> notices = new();
        // topic filter (may contain + and #) -> handler. This connection's own subscriptions.
        private readonly ConcurrentDictionary<string, Subscription> subscriptions = new();
        private readonly SeenEvents seen = new();

        // Per app launch, so two phones can never share a client id (the broker evicts the
        // older one, which presents as an unexplained reconnect loop).
        private readonly string session = Guid.NewGuid().ToString("N").Substring(0, 8);
        private string ClientId => $"phone-visualiser-{session}";

        private IMqttClient client;
        private CancellationTokenSource cts;
        private volatile bool isConnecting;
        private int seq;

        public bool IsConnected => client != null && client.IsConnected;
        public bool IsConnecting => isConnecting;
        /// <summary>One line describing the link, for the debug menu.</summary>
        public string Status { get; private set; } = "not started";
        public int Duplicates { get; private set; }
        public int BadEnvelopes { get; private set; }

        public string BrokerHost
        {
            get => brokerHost;
            set => brokerHost = value;
        }

        private void Start()
        {
            // The address changes from network to network, so remember the last one used
            // instead of rebuilding the app. The serialized value is only the first-run default.
            var saved = MqttConfig.LoadHost();
            if (saved != MqttConfig.DefaultHost) brokerHost = saved;

            if (connectOnStart) Connect();
        }

        public void Connect() => Connect(brokerHost, brokerPort);

        public void Connect(string host, int port)
        {
            if (IsConnected || isConnecting) return;

            brokerHost = host;
            brokerPort = port;
            MqttConfig.SaveHost(host);

            // Cancel the old supervisor but do not dispose its token: it may still be observing it.
            cts?.Cancel();
            cts = new CancellationTokenSource();

            // The client is handed to RunAsync rather than read back from the field: a session
            // being replaced is still unwinding and would otherwise find its replacement there
            // and disconnect it (the defect the comms team hit in Stage B).
            var newClient = new MqttFactory().CreateMqttClient();
            newClient.ApplicationMessageReceivedAsync += OnMessageReceived;
            client = newClient;

            isConnecting = true;
            Status = $"connecting to {host}:{port}";
            _ = RunAsync(newClient, host, port, cts.Token);
        }

        public void Disconnect()
        {
            cts?.Cancel();
            cts = null;
            isConnecting = false;
            if (client != null)
            {
                client.ApplicationMessageReceivedAsync -= OnMessageReceived;
                client.Dispose();
                client = null;
            }
            Status = "disconnected";
        }

        /// <summary>
        /// Subscribe to a topic or filter (+ and # wildcards allowed). The handler gets the
        /// topic and the envelope's inner payload as a JSON string, on the main thread.
        /// Safe to call before the broker is up: it is applied on every (re)connect.
        /// </summary>
        public void Subscribe(string topicFilter, Action<string, string> onMessage, int qos = 1)
        {
            subscriptions[topicFilter] = new Subscription(onMessage, qos);
            if (IsConnected) _ = SubscribeOnBrokerAsync(client, topicFilter, qos);
        }

        public void Unsubscribe(string topicFilter)
        {
            subscriptions.TryRemove(topicFilter, out _);
            if (!IsConnected) return;
            var c = client;
            _ = SafeAsync(() => c.UnsubscribeAsync(new MqttClientUnsubscribeOptionsBuilder().WithTopicFilter(topicFilter).Build()),
                          $"unsubscribe '{topicFilter}'");
        }

        /// <summary>
        /// Wrap <paramref name="json"/> (the game payload) in the envelope and publish it.
        /// QoS 1 by default, matching the contract; pass 0 for streams where the next
        /// message corrects a lost one (game/state).
        /// </summary>
        public void Publish(string topic, string json, bool retain = false, int qos = 1)
        {
            if (!IsConnected)
            {
                Debug.LogWarning($"MqttConnection: cannot publish to '{topic}', not connected.");
                return;
            }

            JToken payload;
            try { payload = JToken.Parse(json); }
            catch (Exception e)
            {
                Debug.LogError($"MqttConnection: payload for '{topic}' is not valid JSON: {e.Message}");
                return;
            }

            string envelope = Envelope.Build(payload, Interlocked.Increment(ref seq), ClientId, session);
            var message = new MqttApplicationMessageBuilder()
                .WithTopic(topic)
                .WithPayload(envelope)
                .WithQualityOfServiceLevel((MqttQualityOfServiceLevel)qos)
                .WithRetainFlag(retain)
                .Build();

            var c = client;
            _ = SafeAsync(() => c.PublishAsync(message, CancellationToken.None), $"publish '{topic}'");
        }

        // ---- main thread ----------------------------------------------------------------

        private void Update()
        {
            while (notices.TryDequeue(out var notice)) Debug.Log($"MqttConnection: {notice}");

            while (incoming.TryDequeue(out var msg)) Dispatch(msg.topic, msg.payload);
        }

        private void Dispatch(string topic, string raw)
        {
            JObject envelope;
            try
            {
                envelope = Envelope.Parse(raw);
            }
            catch (EnvelopeException e)
            {
                // One bad message says nothing about the next: drop it and keep going.
                BadEnvelopes++;
                Debug.LogWarning($"MqttConnection: bad envelope on {topic}: {e.Message}");
                return;
            }

            if (!seen.Check(envelope["event_id"].Value<string>()))
            {
                Duplicates++;
                return;
            }

            string payload = envelope["payload"].ToString(Newtonsoft.Json.Formatting.None);
            foreach (var entry in subscriptions)
            {
                if (!TopicMatches(entry.Key, topic)) continue;
                try { entry.Value.Handler(topic, payload); }
                catch (Exception e)
                {
                    // A faulty handler must not stop the rest of the queue being delivered.
                    Debug.LogException(e);
                }
            }
        }

        /// <summary>MQTT topic-filter matching: + is one level, # is the rest (last level only).</summary>
        public static bool TopicMatches(string filter, string topic)
        {
            var f = filter.Split('/');
            var t = topic.Split('/');
            for (int i = 0; i < f.Length; i++)
            {
                if (f[i] == "#") return true;
                if (i >= t.Length) return false;
                if (f[i] != "+" && f[i] != t[i]) return false;
            }
            return f.Length == t.Length;
        }

        private void OnDestroy() => Disconnect();

        // ---- MQTT task side ---------------------------------------------------------------

        private Task OnMessageReceived(MqttApplicationMessageReceivedEventArgs args)
        {
            incoming.Enqueue((args.ApplicationMessage.Topic, args.ApplicationMessage.ConvertPayloadToString() ?? ""));
            return Task.CompletedTask;
        }

        // Supervisor: same backoff ladder as the rest of the project (double on a fast
        // failure, reset after a healthy run, cap at 60 s).
        private async Task RunAsync(IMqttClient c, string host, int port, CancellationToken ct)
        {
            float backoff = MqttConfig.MinBackoffSeconds;

            while (!ct.IsCancellationRequested)
            {
                var started = DateTime.UtcNow;
                try
                {
                    await OneSessionAsync(c, host, port, ct);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception e)
                {
                    if (!ct.IsCancellationRequested)
                    {
                        Status = $"session failed: {e.Message}";
                        notices.Enqueue($"session failed: {e.Message}");
                    }
                }

                isConnecting = false;
                if (ct.IsCancellationRequested) return;

                double elapsed = (DateTime.UtcNow - started).TotalSeconds;
                if (elapsed >= MqttConfig.GoodRunSeconds) backoff = MqttConfig.MinBackoffSeconds;

                Status = $"reconnecting in {backoff:0}s";
                try { await Task.Delay(TimeSpan.FromSeconds(backoff), ct); }
                catch (OperationCanceledException) { return; }

                if (elapsed < MqttConfig.GoodRunSeconds) backoff = Mathf.Min(backoff * 2f, MqttConfig.MaxBackoffSeconds);
                isConnecting = true;
            }
        }

        private async Task OneSessionAsync(IMqttClient c, string host, int port, CancellationToken ct)
        {
            var options = new MqttClientOptionsBuilder()
                .WithTcpServer(host, port)
                .WithClientId(ClientId)
                .WithKeepAlivePeriod(TimeSpan.FromSeconds(MqttConfig.KeepAliveSeconds))
                .WithCleanSession()
                .Build();

            Status = $"connecting to {host}:{port}";
            await c.ConnectAsync(options, ct);
            isConnecting = false;
            Status = $"connected to {host}:{port} as {ClientId}";
            notices.Enqueue(Status);

            try
            {
                // Subscribe before anything can arrive on a topic we have not asked for.
                foreach (var entry in subscriptions)
                    await SubscribeOnBrokerAsync(c, entry.Key, entry.Value.Qos);

                while (c.IsConnected && !ct.IsCancellationRequested)
                    await Task.Delay(500, ct);
            }
            finally
            {
                // Best effort, on `c` so a replaced session says goodbye to its own connection.
                try { await c.DisconnectAsync(); }
                catch (Exception) { }
            }
        }

        private Task SubscribeOnBrokerAsync(IMqttClient c, string filter, int qos)
        {
            var options = new MqttClientSubscribeOptionsBuilder()
                .WithTopicFilter(f => f.WithTopic(filter).WithQualityOfServiceLevel((MqttQualityOfServiceLevel)qos))
                .Build();
            return SafeAsync(() => c.SubscribeAsync(options, CancellationToken.None), $"subscribe '{filter}'");
        }

        // Fire-and-forget calls must not lose their exceptions: report them through the log.
        private async Task SafeAsync(Func<Task> work, string what)
        {
            try { await work(); }
            catch (Exception e) { notices.Enqueue($"{what} failed: {e.Message}"); }
        }
    }
}
