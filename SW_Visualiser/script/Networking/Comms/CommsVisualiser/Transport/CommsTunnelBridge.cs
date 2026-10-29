using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using BridgeRush.Networking;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// Configuration B for the Vis app: the phone's two-hop SSH nest to the broker on the
    /// Ultra96, supervised, in front of the app's own MqttConnection, which is not edited.
    ///
    ///   MqttConnection -> phone 127.0.0.1:1883 == nest == board 127.0.0.1:1883 (Mosquitto)
    ///
    /// MqttConnection has no reconnect, so this supplies one from outside: it calls Connect()
    /// again whenever the client is down, and only after probing the broker itself, because
    /// the nest accepts locally whether or not the broker is there, and M2Mqtt leaks a
    /// spinning thread when a connect fails after its socket opened (section 10, F1).
    /// MQTT always goes down before the tunnel. notes/integration/vis_integration.md 4.3.
    /// </summary>
    public class CommsTunnelBridge : MonoBehaviour
    {
        private const int PollMs = 500;
        private const int ProbeTimeoutMs = 5000;
        private const int ReleaseWaitMs = 3000;
        private const int MaxLogLines = 15;
        private const float RebuildAfterPauseSeconds = 2f;
        private const float LabelWidth = 240f;

        [SerializeField] private MqttConnection mqttConnection;

        public TunnelConfig Config { get; private set; } = new TunnelConfig();

        /// <summary>The latest stage, for the collapsed status line.</summary>
        public string Status { get; private set; } = "idle";

        public bool IsRunning => _cts != null;

        // NUS account password: typed every launch, held here for reconnects, never saved.
        private string _jumpPassword = "";

        private readonly List<string> _log = new List<string>();

        // SSH.NET logs from its worker threads; Update() moves the lines to the main thread.
        private readonly ConcurrentQueue<string> _pendingLog = new ConcurrentQueue<string>();

        private CancellationTokenSource _cts;
        private Task _run = Task.CompletedTask;
        private SshMqttTunnel _tunnel;
        private float _pausedAt = -1f;
        private Vector2 _scroll;

        private void Awake()
        {
            if (mqttConnection == null)
            {
                Debug.LogError($"{nameof(CommsTunnelBridge)}: needs the scene's Mqtt Connection.", this);
                enabled = false;
            }
        }

        private void Start()
        {
            Config = TunnelConfig.Load();

            // No key files on an iPhone (no adb push), so passwords only. TunnelConfig
            // defaults this to true, which fails with "Private key not found" (F7).
            Config.UsePrivateKeys = false;

            // Gloves mean nobody touches the screen, so without this iOS auto-locks the
            // phone mid-match, suspends the app and the nest dies. Same as TunnelledVisualiser.
            Screen.sleepTimeout = SleepTimeout.NeverSleep;

            AddLine("enter the jump-host password, then press Connect.");
        }

        private void Update()
        {
            while (_pendingLog.TryDequeue(out var line)) AddLine(line);
        }

        /// <summary>Save the form and start the supervisor, after any previous one has fully stopped.</summary>
        public void Connect()
        {
            var previous = _run;
            Disconnect();
            Config.Save();

            // Set before any await, so a second press chains behind this run instead of
            // starting a second supervisor beside it.
            _cts = new CancellationTokenSource();
            _run = RunAfterAsync(previous, _cts.Token);
        }

        /// <summary>MQTT first, while the nest can still carry its DISCONNECT; then the tunnel.</summary>
        public void Disconnect()
        {
            _cts?.Cancel();
            _cts = null;
            SafeDisconnect("disconnect pressed");
            Status = "stopped";
        }

        private async Task RunAfterAsync(Task previous, CancellationToken ct)
        {
            // The old run releases phone 127.0.0.1:1883 in its finally; wait so the new
            // nest does not race it for the port.
            try { await previous; }
            catch (Exception) { /* Already logged by the run itself. */ }

            await RunAsync(ct);
        }

        private void OnApplicationPause(bool paused)
        {
            if (paused)
            {
                _pausedAt = Time.realtimeSinceStartup;
                return;
            }

            // iOS closes a suspended app's sockets. Rebuild now rather than sit "connected"
            // until SSH.NET's keepalive notices. A blink of a pause is not worth a rebuild.
            var away = _pausedAt < 0f ? 0f : Time.realtimeSinceStartup - _pausedAt;
            _pausedAt = -1f;
            if (IsRunning && away >= RebuildAfterPauseSeconds)
            {
                AddLine($"RESUMED after {away:0}s: rebuilding the nest");
                Connect();
            }
        }

        private void OnApplicationQuit() => Disconnect();

        private void OnDestroy() => Disconnect();

        /// <summary>
        /// Outer loop: build the nest, keep MQTT up inside it until the nest drops, retry.
        /// The wait doubles after a short run and resets after a healthy one (MqttConfig).
        /// </summary>
        private async Task RunAsync(CancellationToken ct)
        {
            var backoff = MqttConfig.MinBackoffSeconds;

            while (!ct.IsCancellationRequested)
            {
                var started = DateTime.UtcNow;
                var tunnel = new SshMqttTunnel();
                _tunnel = tunnel;

                try
                {
                    Status = "building the nest";
                    var settings = Config.ToTunnelSettings(
                        _jumpPassword, null, Config.BoardPassword, null, allowUnknownHostKeysOnce: false);

                    await tunnel.StartAsync(settings, EnqueueLog, ct);
                    await KeepMqttUpAsync(tunnel, settings.PayloadLocalPort, ct);
                }
                catch (OperationCanceledException)
                {
                    // Disconnect or a rebuild; the finally below still tears down in order.
                }
                catch (Exception exc)
                {
                    if (!ct.IsCancellationRequested)
                        AddLine($"TUNNEL_FAILED: {exc.GetType().Name}: {exc.Message}");
                }
                finally
                {
                    await ReleaseAsync(tunnel);
                }

                if (ct.IsCancellationRequested) return;

                var elapsed = (DateTime.UtcNow - started).TotalSeconds;
                if (elapsed >= MqttConfig.GoodRunSeconds) backoff = MqttConfig.MinBackoffSeconds;

                Status = $"nest down, retrying in {backoff:0}s";
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
        /// Inner loop, while the nest is up: whenever his client is down and not mid-connect,
        /// probe the broker and only then call his Connect(), on the same backoff ladder.
        /// His Connect() re-subscribes every topic, so the hook's topic comes back with it.
        /// </summary>
        private async Task KeepMqttUpAsync(SshMqttTunnel tunnel, int localPort, CancellationToken ct)
        {
            // A client left over from the last nest would read as connected here.
            if (mqttConnection.IsConnected) SafeDisconnect("stale client from the previous nest");

            var backoff = MqttConfig.MinBackoffSeconds;
            var nextAttempt = DateTime.UtcNow;
            DateTime? connectedAt = null;
            var attempting = false;
            Status = "nest up";

            while (!ct.IsCancellationRequested && tunnel.IsReady)
            {
                var now = DateTime.UtcNow;

                if (mqttConnection.IsConnected)
                {
                    if (connectedAt == null)
                    {
                        connectedAt = now;
                        attempting = false;
                        AddLine("MQTT_CONNECTED");
                        Status = "connected";
                    }
                }
                else if (!mqttConnection.IsConnecting)
                {
                    if (connectedAt != null)
                    {
                        if ((now - connectedAt.Value).TotalSeconds >= MqttConfig.GoodRunSeconds)
                            backoff = MqttConfig.MinBackoffSeconds;
                        connectedAt = null;
                        nextAttempt = now;
                        AddLine("MQTT_LOST (nest up)");
                    }

                    if (attempting)
                    {
                        attempting = false;
                        AddLine("MQTT_CONNECT_FAILED: MqttConnection's own error is in the console");
                    }

                    if (now >= nextAttempt)
                    {
                        if (await ProbeBrokerAsync(localPort, ct) && tunnel.IsReady)
                        {
                            AddLine("MQTT_CONNECTING");
                            Status = "connecting MQTT";
                            mqttConnection.Connect();
                            attempting = true;
                        }
                        else
                        {
                            Status = $"no broker, retrying in {backoff:0}s";
                        }

                        nextAttempt = DateTime.UtcNow + TimeSpan.FromSeconds(backoff);
                        backoff = Math.Min(backoff * 2f, MqttConfig.MaxBackoffSeconds);
                    }
                }

                await Task.Delay(PollMs, ct);
            }

            if (!ct.IsCancellationRequested && connectedAt != null) AddLine("MQTT_LOST (nest down)");
        }

        /// <summary>
        /// One MQTT CONNECT/CONNACK on our own socket, then DISCONNECT. True only on a CONNACK
        /// with return code 0. Never touches his client.
        /// </summary>
        private async Task<bool> ProbeBrokerAsync(int localPort, CancellationToken ct)
        {
            try
            {
                using (var client = new TcpClient())
                {
                    await WithTimeout(client.ConnectAsync("127.0.0.1", localPort), ct);
                    var stream = client.GetStream();

                    // A unique id, so the probe can never evict a real client.
                    var id = Encoding.ASCII.GetBytes("comms-probe-" + Guid.NewGuid().ToString("N").Substring(0, 8));
                    var connect = new List<byte>
                    {
                        0x10, (byte)(12 + id.Length),            // CONNECT, remaining length
                        0x00, 0x04, (byte)'M', (byte)'Q', (byte)'T', (byte)'T',
                        0x04,                                    // protocol level 3.1.1
                        0x02,                                    // clean session
                        0x00, 0x0A,                              // keepalive 10 s
                        0x00, (byte)id.Length,
                    };
                    connect.AddRange(id);
                    await WithTimeout(stream.WriteAsync(connect.ToArray(), 0, connect.Count, ct), ct);

                    var connack = new byte[4];
                    var got = 0;
                    while (got < connack.Length)
                    {
                        var read = stream.ReadAsync(connack, got, connack.Length - got, ct);
                        await WithTimeout(read, ct);
                        if (read.Result == 0) break;
                        got += read.Result;
                    }

                    if (got < connack.Length || connack[0] != 0x20)
                    {
                        AddLine(got == 0 ? "NO_BROKER: closed from the far end" : "NO_BROKER: not a CONNACK");
                        return false;
                    }

                    if (connack[3] != 0)
                    {
                        AddLine($"NO_BROKER: CONNACK refused, code {connack[3]}");
                        return false;
                    }

                    try { await stream.WriteAsync(new byte[] { 0xE0, 0x00 }, 0, 2, ct); }
                    catch (Exception) { /* The probe already has its answer. */ }

                    AddLine("BROKER_OK");
                    return true;
                }
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception exc)
            {
                AddLine($"NO_BROKER: {exc.GetType().Name}: {exc.Message}");
                return false;
            }
        }

        private static async Task WithTimeout(Task task, CancellationToken ct)
        {
            var done = await Task.WhenAny(task, Task.Delay(ProbeTimeoutMs, ct));
            ct.ThrowIfCancellationRequested();
            if (done != task) throw new TimeoutException($"no answer in {ProbeTimeoutMs / 1000} s");
            await task;
        }

        /// <summary>
        /// Teardown, in order: his client first (a DISCONNECT if the nest still carries one),
        /// a short wait for M2Mqtt to close, then the tunnel off the main thread.
        /// </summary>
        private async Task ReleaseAsync(SshMqttTunnel tunnel)
        {
            SafeDisconnect(tunnel.IsReady ? "nest closing" : "nest dropped");

            var deadline = DateTime.UtcNow.AddMilliseconds(ReleaseWaitMs);
            while (mqttConnection.IsConnected && DateTime.UtcNow < deadline)
                await Task.Delay(100);

            // StopAsync, not Dispose: an SSH disconnect blocks, and this is the main thread.
            try { await tunnel.StopAsync(); }
            catch (Exception) { /* Dispose already swallows per-object failures. */ }

            if (ReferenceEquals(_tunnel, tunnel)) _tunnel = null;
        }

        /// <summary>
        /// His Disconnect() does not catch, and M2Mqtt's throws on a dead socket. Either way
        /// the client ends up closed or about to close itself.
        /// </summary>
        private void SafeDisconnect(string why)
        {
            if (mqttConnection == null || !mqttConnection.IsConnected) return;

            try
            {
                mqttConnection.Disconnect();
                AddLine($"MQTT disconnected ({why})");
            }
            catch (Exception exc)
            {
                AddLine($"MQTT disconnect failed ({why}): {exc.GetType().Name}: {exc.Message}");
            }
        }

        private void EnqueueLog(string line) => _pendingLog.Enqueue(line);

        /// <summary>Main thread only. The full line goes to Debug.Log; the screen gets it masked.</summary>
        private void AddLine(string line)
        {
            var stamped = $"{DateTime.Now:HH:mm:ss} {line}";
            Debug.Log($"[comms-bridge] {stamped}");

            _log.Add(Mask(stamped));
            if (_log.Count > MaxLogLines) _log.RemoveAt(0);
            _scroll.y = float.MaxValue;
        }

        /// <summary>A filmed screen is close to a commit: hide hosts and the NUS username.</summary>
        private string Mask(string line)
        {
            line = Replace(line, Config.JumpHost, "<jump-host>");
            line = Replace(line, Config.BoardHost, "<board>");
            line = Replace(line, Config.JumpUsername, "<user>");
            return line;
        }

        private static string Replace(string line, string secret, string mask)
        {
            var value = secret?.Trim();
            return string.IsNullOrEmpty(value) ? line : line.Replace(value, mask);
        }

        /// <summary>The credential form (while stopped) and the log, for a host panel such as the debug menu.</summary>
        public void DrawDetails()
        {
            _scroll = GUILayout.BeginScrollView(_scroll);
            if (!IsRunning) DrawForm();
            GUILayout.Label("Log");
            foreach (var line in _log) GUILayout.Label(line);
            GUILayout.EndScrollView();
        }

        private void DrawForm()
        {
            GUILayout.Label("Hop 1 - NUS jump host (VPN must be on)");
            Config.JumpHost = Field("Host", Config.JumpHost);
            Config.JumpUsername = Field("NUS username", Config.JumpUsername);
            GUILayout.BeginHorizontal();
            GUILayout.Label("Password (NOT saved)", GUILayout.Width(LabelWidth));
            _jumpPassword = GUILayout.PasswordField(_jumpPassword ?? "", '*');
            GUILayout.EndHorizontal();
            Config.JumpFingerprint = PasteField("Jump host SHA256", Config.JumpFingerprint);

            GUILayout.Label("Hop 2 - Ultra96, through hop 1");
            Config.BoardHost = Field("Board host", Config.BoardHost);
            Config.BoardUsername = Field("Username", Config.BoardUsername);
            GUILayout.BeginHorizontal();
            GUILayout.Label("Password (saved)", GUILayout.Width(LabelWidth));
            Config.BoardPassword = GUILayout.PasswordField(Config.BoardPassword ?? "", '*');
            GUILayout.EndHorizontal();
            Config.BoardFingerprint = PasteField("Board SHA256", Config.BoardFingerprint);
            Config.PayloadRemotePort = PortField("Board broker port", Config.PayloadRemotePort);
        }

        private static string Field(string label, string value)
        {
            GUILayout.BeginHorizontal();
            GUILayout.Label(label, GUILayout.Width(LabelWidth));
            var updated = GUILayout.TextField(value ?? "");
            GUILayout.EndHorizontal();
            return updated;
        }

        /// <summary>A text field plus Paste: fingerprints are sent privately and copied on the phone.</summary>
        private static string PasteField(string label, string value)
        {
            GUILayout.BeginHorizontal();
            GUILayout.Label(label, GUILayout.Width(LabelWidth));
            var updated = GUILayout.TextField(value ?? "");
            if (GUILayout.Button("Paste", GUILayout.Width(80f))) updated = GUIUtility.systemCopyBuffer?.Trim() ?? "";
            GUILayout.EndHorizontal();
            return updated;
        }

        private static int PortField(string label, int value)
        {
            GUILayout.BeginHorizontal();
            GUILayout.Label(label, GUILayout.Width(LabelWidth));
            var text = GUILayout.TextField(value.ToString());
            GUILayout.EndHorizontal();
            return int.TryParse(text, out var parsed) ? parsed : value;
        }
    }
}
