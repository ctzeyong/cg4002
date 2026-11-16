using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace Comms.Visualiser
{
    /// <summary>One SSH hop's address and credentials. Secrets live here and nowhere on disk.</summary>
    public sealed class SshHopSettings
    {
        public string Host;
        public int Port = 22;
        public string Username;

        /// <summary>Kept in memory for every reconnect (the jump host asks each time). Never written to disk.</summary>
        public string Password;

        public string PrivateKeyPath;
        public string PrivateKeyPassphrase;
        public string ExpectedHostKeySha256;

        public SshHopSettings Clone() => (SshHopSettings)MemberwiseClone();
    }

    /// <summary>Both hops plus the ports used on the phone and on the board.</summary>
    public sealed class SshTunnelSettings
    {
        public SshHopSettings Jump = new SshHopSettings();
        public SshHopSettings Board = new SshHopSettings();

        /// <summary>Phone-local port that carries the second SSH session to the board.</summary>
        public int BoardSshLocalPort = 12222;

        /// <summary>Phone-local port MQTTnet connects to.</summary>
        public int PayloadLocalPort = 1883;

        /// <summary>Port on the board's own loopback: 1883 for Mosquitto, 18080 for the echo probe.</summary>
        public int PayloadRemotePort = 1883;

        public int ConnectionTimeoutSeconds = 15;
        public bool AllowUnknownHostKeysOnce;

        public SshTunnelSettings Clone()
        {
            var copy = (SshTunnelSettings)MemberwiseClone();
            copy.Jump = Jump.Clone();
            copy.Board = Board.Clone();
            return copy;
        }
    }

    /// <summary>
    /// The phone's two-hop SSH tunnel to the broker on the board's loopback:
    ///
    ///   phone -> jump host -> board:22 (second SSH session) -> board 127.0.0.1:1883
    ///
    /// MQTTnet then connects to the phone's own 127.0.0.1:1883. Two hops are needed
    /// because the jump host refuses to forward to any board port except 22 (tested).
    ///
    /// No Unity calls here, so the blocking SSH.NET calls can run on a worker thread;
    /// the `log` callback is called from those threads.
    /// </summary>
    public sealed class SshMqttTunnel : IDisposable
    {
        private const string ProbeRequestPrefix = "CG4002_TUNNEL_PROBE ";
        private const string ProbeResponsePrefix = "CG4002_TUNNEL_OK ";

        private readonly object _lifecycle = new object();
        private readonly List<IDisposable> _ownedAuth = new List<IDisposable>();

        private SshClient _jumpClient;
        private SshClient _boardClient;
        private ForwardedPortLocal _boardSshForward;
        private ForwardedPortLocal _payloadForward;
        private Action<string> _log;
        private bool _disposed;

        /// <summary>
        /// Set only when a whole SSH session fails. Not set when one forwarded connection
        /// fails (e.g. broker down), which must not tear down a healthy tunnel.
        /// </summary>
        private volatile bool _sessionFaulted;

        /// <summary>
        /// Last forwarding error, if SSH.NET reported one. Extra detail for the status line
        /// only: it is NOT raised when the broker is down, so use IsReady to tell
        /// "tunnel up, broker down" apart.
        /// </summary>
        public string LastForwardError { get; private set; }

        public int ForwardFaults { get; private set; }

        /// <summary>True while both SSH sessions and both port forwards are up.</summary>
        public bool IsReady
        {
            get
            {
                lock (_lifecycle)
                {
                    return !_disposed && !_sessionFaulted &&
                           _jumpClient != null && _jumpClient.IsConnected &&
                           _boardClient != null && _boardClient.IsConnected &&
                           _boardSshForward != null && _boardSshForward.IsStarted &&
                           _payloadForward != null && _payloadForward.IsStarted;
                }
            }
        }

        /// <summary>Check the settings, then build the tunnel on a worker thread (see StartBlocking).</summary>
        public Task StartAsync(SshTunnelSettings settings, Action<string> logger, CancellationToken ct)
        {
            if (settings == null) throw new ArgumentNullException(nameof(settings));

            Validate(settings);
            _log = logger ?? delegate { };

            return Task.Run(() =>
            {
                ct.ThrowIfCancellationRequested();
                StartBlocking(settings, ct);
            }, ct);
        }

        /// <summary>
        /// Test only: echo one line off ultra96/tunnel_probe_server.py, to check the tunnel
        /// works before any broker is running on the board.
        /// </summary>
        public async Task EchoProbeAsync(int localPort, int timeoutMilliseconds, CancellationToken ct)
        {
            if (!IsReady) throw new InvalidOperationException("Both SSH hops must be ready first.");

            ct.ThrowIfCancellationRequested();
            var nonce = Guid.NewGuid().ToString("N");
            var expected = ProbeResponsePrefix + nonce;

            using (var client = new TcpClient())
            {
                await AwaitWithTimeout(
                    client.ConnectAsync("127.0.0.1", localPort), timeoutMilliseconds, ct).ConfigureAwait(false);

                using (var stream = client.GetStream())
                using (var writer = new StreamWriter(stream, Encoding.ASCII, 256, true))
                using (var reader = new StreamReader(stream, Encoding.ASCII, false, 256, true))
                {
                    writer.NewLine = "\n";
                    await writer.WriteLineAsync(ProbeRequestPrefix + nonce).ConfigureAwait(false);
                    await writer.FlushAsync().ConfigureAwait(false);

                    var readTask = reader.ReadLineAsync();
                    await AwaitWithTimeout(readTask, timeoutMilliseconds, ct).ConfigureAwait(false);
                    var response = await readTask.ConfigureAwait(false);
                    if (!string.Equals(response, expected, StringComparison.Ordinal))
                        throw new IOException("Unexpected probe response: " + (response ?? "<connection closed>"));
                }
            }

            WriteLog("PROBE_OK: echo crossed both hops");
        }

        /// <summary>Close the tunnel on a worker thread, because SSH disconnect blocks.</summary>
        public Task StopAsync() => Task.Run((Action)Dispose);

        public void Dispose()
        {
            lock (_lifecycle)
            {
                if (_disposed) return;
                _disposed = true;

                // Close innermost first, so nothing is closed while something still uses it.
                StopAndDispose(_payloadForward);
                _payloadForward = null;

                DisconnectAndDispose(_boardClient);
                _boardClient = null;

                StopAndDispose(_boardSshForward);
                _boardSshForward = null;

                DisconnectAndDispose(_jumpClient);
                _jumpClient = null;

                for (var i = _ownedAuth.Count - 1; i >= 0; i--)
                {
                    try { _ownedAuth[i].Dispose(); }
                    catch (Exception) { /* Best-effort; every network owner is already stopped. */ }
                }

                _ownedAuth.Clear();
            }

            WriteLog("TUNNEL_DISCONNECTED");
        }

        /// <summary>
        /// Build the tunnel in order: SSH to the jump host, forward a phone port to board:22,
        /// SSH to the board through it, then forward the phone's payload port to the board's
        /// 127.0.0.1 (1883 = broker). If any step fails, tear down what was built.
        /// </summary>
        private void StartBlocking(SshTunnelSettings settings, CancellationToken ct)
        {
            lock (_lifecycle)
            {
                if (_disposed) throw new ObjectDisposedException(nameof(SshMqttTunnel));
                if (_jumpClient != null || _boardClient != null)
                    throw new InvalidOperationException("Tunnel has already been started.");
            }

            try
            {
                WriteLog($"JUMP_CONNECTING: {settings.Jump.Host}:{settings.Jump.Port}");
                _jumpClient = new SshClient(BuildConnectionInfo(settings.Jump, settings.ConnectionTimeoutSeconds));
                ConfigureHostKeyValidation(_jumpClient, "jump", settings.Jump.ExpectedHostKeySha256,
                    settings.AllowUnknownHostKeysOnce);
                _jumpClient.ErrorOccurred += SessionError;
                _jumpClient.KeepAliveInterval = TimeSpan.FromSeconds(15);
                _jumpClient.Connect();
                WriteLog("JUMP_CONNECTED");

                ct.ThrowIfCancellationRequested();
                _boardSshForward = new ForwardedPortLocal(
                    "127.0.0.1",
                    checked((uint)settings.BoardSshLocalPort),
                    settings.Board.Host,
                    checked((uint)settings.Board.Port));
                _boardSshForward.Exception += ForwardError;
                _jumpClient.AddForwardedPort(_boardSshForward);
                _boardSshForward.Start();
                WriteLog($"BOARD_SSH_FORWARD_READY: 127.0.0.1:{settings.BoardSshLocalPort} -> " +
                         $"{settings.Board.Host}:{settings.Board.Port}");

                ct.ThrowIfCancellationRequested();
                WriteLog("BOARD_CONNECTING: through phone-local SSH forward");
                _boardClient = new SshClient(BuildConnectionInfo(
                    ForwardedBoardHop(settings.Board, settings.BoardSshLocalPort),
                    settings.ConnectionTimeoutSeconds));
                ConfigureHostKeyValidation(_boardClient, "Ultra96", settings.Board.ExpectedHostKeySha256,
                    settings.AllowUnknownHostKeysOnce);
                _boardClient.ErrorOccurred += SessionError;
                _boardClient.KeepAliveInterval = TimeSpan.FromSeconds(15);
                _boardClient.Connect();
                WriteLog("BOARD_CONNECTED");

                ct.ThrowIfCancellationRequested();
                _payloadForward = new ForwardedPortLocal(
                    "127.0.0.1",
                    checked((uint)settings.PayloadLocalPort),
                    "127.0.0.1",
                    checked((uint)settings.PayloadRemotePort));
                _payloadForward.Exception += ForwardError;
                _boardClient.AddForwardedPort(_payloadForward);
                _payloadForward.Start();
                WriteLog($"PAYLOAD_FORWARD_READY: phone 127.0.0.1:{settings.PayloadLocalPort} -> " +
                         $"Ultra96 127.0.0.1:{settings.PayloadRemotePort}");
            }
            catch
            {
                // Undo whatever was built; nothing else is using these objects yet.
                Dispose();
                throw;
            }
        }

        /// <summary>Login methods for one hop: the private key if set, plus the password if set.</summary>
        private ConnectionInfo BuildConnectionInfo(SshHopSettings hop, int timeoutSeconds)
        {
            var methods = new List<AuthenticationMethod>();

            if (!string.IsNullOrWhiteSpace(hop.PrivateKeyPath))
            {
                var keyStream = File.OpenRead(hop.PrivateKeyPath);
                _ownedAuth.Add(keyStream);

                var keyFile = new PrivateKeyFile(keyStream, EmptyToNull(hop.PrivateKeyPassphrase));
                _ownedAuth.Add(keyFile);

                var keyMethod = new PrivateKeyAuthenticationMethod(hop.Username, keyFile);
                _ownedAuth.Add(keyMethod);
                methods.Add(keyMethod);
            }

            if (!string.IsNullOrEmpty(hop.Password))
            {
                // The jump host may ask for the password in either of two ways, so offer both.
                var keyboard = new KeyboardInteractiveAuthenticationMethod(hop.Username);
                keyboard.AuthenticationPrompt += (sender, args) =>
                {
                    foreach (var prompt in args.Prompts)
                        if (!prompt.IsEchoed) prompt.Response = hop.Password;
                };
                _ownedAuth.Add(keyboard);
                methods.Add(keyboard);

                var password = new PasswordAuthenticationMethod(hop.Username, hop.Password);
                _ownedAuth.Add(password);
                methods.Add(password);
            }

            if (methods.Count == 0)
                throw new InvalidOperationException($"No authentication method configured for {hop.Host}.");

            return new ConnectionInfo(hop.Host, hop.Port, hop.Username, methods.ToArray())
            {
                Timeout = TimeSpan.FromSeconds(timeoutSeconds)
            };
        }

        /// <summary>
        /// Trust the server only if its SHA-256 host key matches the saved fingerprint.
        /// With no fingerprint saved, trust it only when the first-run toggle is on; log it either way.
        /// </summary>
        private void ConfigureHostKeyValidation(
            SshClient client, string label, string expectedSha256, bool allowUnknown)
        {
            client.HostKeyReceived += (sender, args) =>
            {
                var observed = "SHA256:" + args.FingerPrintSHA256;
                var expected = NormalizeSha256(expectedSha256);
                var hasExpected = !string.IsNullOrEmpty(expected);
                var matched = hasExpected && string.Equals(observed, expected, StringComparison.Ordinal);
                var temporary = !hasExpected && allowUnknown;

                args.CanTrust = matched || temporary;
                WriteLog($"{label} host key {observed} " +
                         (matched ? "MATCHED" : temporary ? "TEMPORARILY_ACCEPTED" : "REJECTED"));
            };
        }

        /// <summary>The board hop re-addressed at the phone-local end of the jump forward.</summary>
        private static SshHopSettings ForwardedBoardHop(SshHopSettings source, int localPort)
        {
            var hop = source.Clone();
            hop.Host = "127.0.0.1";
            hop.Port = localPort;
            return hop;
        }

        private static async Task AwaitWithTimeout(Task task, int timeoutMilliseconds, CancellationToken ct)
        {
            var timeout = Task.Delay(timeoutMilliseconds, ct);
            var completed = await Task.WhenAny(task, timeout).ConfigureAwait(false);
            if (completed != task)
            {
                ct.ThrowIfCancellationRequested();
                throw new TimeoutException("Network operation timed out.");
            }

            await task.ConfigureAwait(false);
            ct.ThrowIfCancellationRequested();
        }

        private static string NormalizeSha256(string fingerprint)
        {
            if (string.IsNullOrWhiteSpace(fingerprint)) return string.Empty;

            var trimmed = fingerprint.Trim();
            return trimmed.StartsWith("SHA256:", StringComparison.OrdinalIgnoreCase)
                ? "SHA256:" + trimmed.Substring("SHA256:".Length)
                : "SHA256:" + trimmed;
        }

        private static string EmptyToNull(string value) => string.IsNullOrEmpty(value) ? null : value;

        private void ForwardError(object sender, ExceptionEventArgs args)
        {
            ForwardFaults++;
            LastForwardError = args.Exception.Message;
            WriteLog("FORWARD_ERROR: " + args.Exception.Message);
        }

        private void SessionError(object sender, ExceptionEventArgs args)
        {
            _sessionFaulted = true;
            WriteLog("SESSION_ERROR: " + args.Exception.Message);
        }

        private static void StopAndDispose(ForwardedPortLocal forwardedPort)
        {
            if (forwardedPort == null) return;

            try
            {
                if (forwardedPort.IsStarted) forwardedPort.Stop();
            }
            catch (Exception) { /* Continue teardown. */ }

            forwardedPort.Dispose();
        }

        private static void DisconnectAndDispose(SshClient client)
        {
            if (client == null) return;

            try
            {
                if (client.IsConnected) client.Disconnect();
            }
            catch (Exception) { /* Continue teardown. */ }

            client.Dispose();
        }

        private void WriteLog(string message) => _log?.Invoke(message);

        private static void Validate(SshTunnelSettings settings)
        {
            ValidateHop(settings.Jump, "jump");
            ValidateHop(settings.Board, "Ultra96");
            ValidatePort(settings.BoardSshLocalPort, "BoardSshLocalPort");
            ValidatePort(settings.PayloadLocalPort, "PayloadLocalPort");
            ValidatePort(settings.PayloadRemotePort, "PayloadRemotePort");

            if (settings.BoardSshLocalPort == settings.PayloadLocalPort)
                throw new ArgumentException("The two phone-local ports must be different.");

            if (settings.ConnectionTimeoutSeconds < 1 || settings.ConnectionTimeoutSeconds > 120)
                throw new ArgumentOutOfRangeException(nameof(settings.ConnectionTimeoutSeconds));
        }

        private static void ValidateHop(SshHopSettings hop, string label)
        {
            if (hop == null || string.IsNullOrWhiteSpace(hop.Host))
                throw new ArgumentException($"{label} host is required.");

            if (string.IsNullOrWhiteSpace(hop.Username))
                throw new ArgumentException($"{label} username is required.");

            ValidatePort(hop.Port, label + " port");
        }

        private static void ValidatePort(int port, string name)
        {
            if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException(name);
        }
    }
}
