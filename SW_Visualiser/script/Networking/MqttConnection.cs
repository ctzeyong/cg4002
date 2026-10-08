using System;
using System.Collections.Concurrent;
using System.Linq;
using System.Text;
using System.Threading;
using uPLibrary.Networking.M2Mqtt;
using uPLibrary.Networking.M2Mqtt.Messages;
using UnityEngine;

namespace BridgeRush.Networking
{
    public class MqttConnection : MonoBehaviour
    {
        [Tooltip("Broker address. This component owns the connection - nothing else should configure it.")]
        [SerializeField] private string brokerHost = "127.0.0.1";
        [SerializeField] private int brokerPort = 1883;
        [Tooltip("Connect on Start. Turn off to connect manually from the debug menu.")]
        [SerializeField] private bool connectOnStart = true;

        private MqttClient client;
        private readonly ConcurrentQueue<(string topic, string payload)> incoming = new();
        private readonly ConcurrentQueue<string> connectErrors = new();
        // This connection's own topic subscriptions (topic -> handler) - not other connected players/phones.
        private readonly ConcurrentDictionary<string, Action<string, string>> subscriptions = new();
        private volatile bool isConnecting;

        public bool IsConnected => client != null && client.IsConnected;
        public bool IsConnecting => isConnecting;

        public string BrokerHost
        {
            get => brokerHost;
            set => brokerHost = value;
        }

        private void Start()
        {
            if (connectOnStart) Connect();
        }

        public void Connect() => Connect(brokerHost, brokerPort);

        public void Connect(string host, int port)
        {
            if (IsConnected || isConnecting) return;

            isConnecting = true;
            var thread = new Thread(() => ConnectBlocking(host, port)) { IsBackground = true };
            thread.Start();
        }
        // blocking connect on a background thread, to avoid freezing the main thread
        private void ConnectBlocking(string host, int port)
        {
            try
            {
                var newClient = new MqttClient(host, port, false, null, null, MqttSslProtocols.None);
                string clientId = Guid.NewGuid().ToString();
                newClient.Connect(clientId);

                if (newClient.IsConnected)
                {
                    newClient.MqttMsgPublishReceived += OnMqttMsgPublishReceived;
                    client = newClient;

                    if (!subscriptions.IsEmpty)
                    {
                        client.Subscribe(subscriptions.Keys.ToArray(), subscriptions.Keys.Select(_ => MqttMsgBase.QOS_LEVEL_AT_MOST_ONCE).ToArray());
                    }
                }
                else
                {
                    connectErrors.Enqueue($"Failed to connect to {host}:{port}.");
                }
            }
            catch (Exception e)
            {
                connectErrors.Enqueue($"Connect to {host}:{port} threw: {e.Message}");
            }
            finally
            {
                isConnecting = false;
            }
        }

        public void Disconnect()
        {
            if (client == null) return;
            client.MqttMsgPublishReceived -= OnMqttMsgPublishReceived;
            if (client.IsConnected) client.Disconnect();
            client = null;
        }

        public void Subscribe(string topic, Action<string, string> onMessage)
        {
            subscriptions[topic] = onMessage;
            client?.Subscribe(new[] { topic }, new[] { MqttMsgBase.QOS_LEVEL_AT_MOST_ONCE });
        }

        public void Publish(string topic, string json, bool retain = false, byte qos = MqttMsgBase.QOS_LEVEL_AT_MOST_ONCE)
        {
            if (!IsConnected)
            {
                Debug.LogWarning($"MqttConnection: cannot publish to '{topic}', not connected.");
                return;
            }

            client.Publish(topic, Encoding.UTF8.GetBytes(json), qos, retain);
        }

        private void OnMqttMsgPublishReceived(object sender, MqttMsgPublishEventArgs e)
        {
            incoming.Enqueue((e.Topic, Encoding.UTF8.GetString(e.Message)));
        }

        private void Update()
        {
            while (incoming.TryDequeue(out var msg))
            {
                if (subscriptions.TryGetValue(msg.topic, out var handler))
                {
                    handler(msg.topic, msg.payload);
                }
            }

            while (connectErrors.TryDequeue(out var error))
            {
                Debug.LogError($"MqttConnection: {error}");
            }
        }

        private void OnDestroy()
        {
            Disconnect();
        }
    }
}
