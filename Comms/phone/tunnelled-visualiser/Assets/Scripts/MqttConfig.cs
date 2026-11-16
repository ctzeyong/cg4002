using System.IO;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// The phone's MQTT settings, matching laptop/mqtt_config.py.
    /// </summary>
    public static class MqttConfig
    {
        // Default broker address; a client can replace it at runtime (LoadHost).
        public const string DefaultHost = "127.0.0.1";

        public const int PlaintextPort = 1883;
        public const int TlsPort = 8883; // mTLS listener

        public const string GameGestureFilter = "game/gesture/+";

        // QoS 1 (at least once): the broker may deliver twice, which SeenEvents handles.
        public const int QosGameGesture = 1;
        public const int QosVizReady = 1;

        public const int KeepAliveSeconds = 15;

        // Same backoff ladder as ble_client.run_node and mqtt_client.run_relay:
        // double on a fast failure, reset after a healthy run, cap at 60 s.
        public const float MinBackoffSeconds = 2f;
        public const float MaxBackoffSeconds = 60f;
        public const float GoodRunSeconds = 60f;

        public static string GestureTopic(int player) => $"game/gesture/{player}";

        public static string VizReadyTopic(int player) => $"viz/ready/{player}";

        /// <summary>
        /// Client id for one phone. It includes the player number because the broker kicks
        /// off an older client with the same id, so two phones must never share one.
        /// </summary>
        public static string ClientId(int player) => $"phone-visualiser-{player}";

        /// <summary>
        /// Player number from the end of a `game/gesture/&lt;player&gt;` topic, or null if
        /// it is not a number. The player rides in the topic, not the payload.
        /// </summary>
        public static int? PlayerFromTopic(string topic)
        {
            var last = topic.Substring(topic.LastIndexOf('/') + 1);
            return int.TryParse(last, out var player) ? player : (int?)null;
        }

        /// <summary>
        /// File that remembers the broker address between launches. Not hard-coded, because
        /// the address changes from network to network.
        /// </summary>
        private static string HostFile => Path.Combine(Application.persistentDataPath, "broker_host.txt");

        public static string LoadHost()
        {
            try
            {
                if (File.Exists(HostFile))
                {
                    var host = File.ReadAllText(HostFile).Trim();
                    if (host.Length > 0) return host;
                }
            }
            catch (IOException exc)
            {
                Debug.LogWarning($"could not read {HostFile}: {exc.Message}");
            }
            return DefaultHost;
        }

        public static void SaveHost(string host)
        {
            try
            {
                File.WriteAllText(HostFile, host.Trim());
            }
            catch (IOException exc)
            {
                Debug.LogWarning($"could not write {HostFile}: {exc.Message}");
            }
        }
    }
}
