using System;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// The on-screen status lines, settings form and log. Drawn with IMGUI, so no scene
    /// setup (Canvas, prefabs, fonts) is needed. The whole form scrolls, as it does not
    /// fit on a portrait phone screen.
    /// </summary>
    [RequireComponent(typeof(TunnelledVisualiser))]
    public class TunnelledUI : MonoBehaviour
    {
        private const float ReferenceWidth = 800f;
        private const float LabelWidth = 260f;

        private TunnelledVisualiser _visualiser;
        private TunnelConfig _config;
        private Vector2 _scroll;
        private Vector2 _logScroll;
        private int _renderedVersion;
        private bool _showForm = true;

        // Typed here, handed to the visualiser on Connect, then cleared so they are not
        // left on screen. (The board password is not here: it is saved in _config.)
        private string _jumpPassword = "";
        private string _jumpPassphrase = "";
        private string _boardPassphrase = "";

        private Texture2D _background;
        private bool _skinTuned;

        private void Awake()
        {
            _visualiser = GetComponent<TunnelledVisualiser>();

            // A black pixel stretched behind the form, so the white log text stays readable
            // on the phone and in recordings (the default scene background is grey).
            _background = new Texture2D(1, 1) { hideFlags = HideFlags.HideAndDontSave };
            _background.SetPixel(0, 0, Color.black);
            _background.Apply();
        }

        private void OnDestroy()
        {
            // Nothing else destroys this texture, so do it here.
            if (_background != null) Destroy(_background);
        }

        private void OnGUI()
        {
            // Fetched on the first frame, after TunnelledVisualiser.Start has loaded it.
            _config ??= _visualiser.Config;

            // Drawn before the scaling below, so it covers the real full screen.
            GUI.DrawTexture(new Rect(0f, 0f, Screen.width, Screen.height), _background,
                ScaleMode.StretchToFill);

            if (!_skinTuned)
            {
                // Force white text so it shows on the black background (once is enough).
                GUI.skin.label.normal.textColor = Color.white;
                GUI.skin.toggle.normal.textColor = Color.white;
                GUI.skin.toggle.onNormal.textColor = Color.white;
                GUI.skin.toggle.hover.textColor = Color.white;
                GUI.skin.toggle.onHover.textColor = Color.white;
                _skinTuned = true;
            }

            // Scale everything so the layout fits any screen width.
            var scale = Screen.width / ReferenceWidth;
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));

            GUILayout.BeginArea(new Rect(10, 10, ReferenceWidth - 20, Screen.height / scale - 20));

            // Status and counters. received/dup/bad are the numbers to show in the demo.
            GUILayout.Label($"tunnel: {_visualiser.TunnelStatus}");
            GUILayout.Label($"payload: {_visualiser.MqttStatus}");
            GUILayout.Label(
                $"received={_visualiser.Received} dup={_visualiser.Duplicates} " +
                $"bad={_visualiser.BadEnvelope} mqtt_sessions={_visualiser.MqttSessions} " +
                $"mqtt_reconnects={_visualiser.MqttReconnects}");
            GUILayout.Label(
                $"tunnel_sessions={_visualiser.TunnelSessions} tunnel_retries={_visualiser.TunnelRetries} " +
                $"probes_ok={_visualiser.ProbesOk}");

            GUILayout.BeginHorizontal();
            if (GUILayout.Button(_visualiser.IsRunning ? "Disconnect" : "Connect", GUILayout.Height(44f)))
            {
                if (_visualiser.IsRunning) _visualiser.Disconnect();
                else StartTunnel();
            }
            if (GUILayout.Button(_showForm ? "Hide settings" : "Show settings", GUILayout.Height(44f)))
                _showForm = !_showForm;
            GUILayout.EndHorizontal();

            _scroll = GUILayout.BeginScrollView(_scroll);

            if (_showForm) DrawForm();
            DrawLog();

            GUILayout.EndScrollView();
            GUILayout.EndArea();
        }

        /// <summary>The settings form: mode, both SSH hops, keys, local ports and player number.</summary>
        private void DrawForm()
        {
            GUILayout.Space(6f);
            GUILayout.Label("Payload");
            var probe = GUILayout.Toggle(
                _visualiser.Mode == PayloadMode.Probe,
                "T1 staging: echo probe instead of MQTT (no broker needed on the board)");
            _visualiser.Mode = probe ? PayloadMode.Probe : PayloadMode.Mqtt;

            // The board port follows the mode, so MQTT can never be pointed at the test port.
            _config.PayloadRemotePort = probe ? TunnelConfig.ProbeRemotePort : MqttConfig.PlaintextPort;
            GUILayout.Label($"   board loopback port: {_config.PayloadRemotePort}");

            _visualiser.AllowUnknownHostKeysOnce = GUILayout.Toggle(
                _visualiser.AllowUnknownHostKeysOnce,
                "FIRST RUN ONLY: accept unknown host keys and print their fingerprints");

            GUILayout.Space(6f);
            GUILayout.Label("Hop 1 - NUS jump host");
            _config.JumpHost = Field("Host", _config.JumpHost);
            _config.JumpPort = PortField("Port", _config.JumpPort);
            _config.JumpUsername = Field("Username", _config.JumpUsername);
            _jumpPassword = Secret("Password (NOT saved - retype each launch)", _jumpPassword);
            _config.JumpFingerprint = Field("Expected SHA256 fingerprint", _config.JumpFingerprint);

            GUILayout.Space(6f);
            GUILayout.Label("Hop 2 - Ultra96, through hop 1");
            _config.BoardHost = Field("Board host as the jump host sees it", _config.BoardHost);
            _config.BoardPort = PortField("SSH port", _config.BoardPort);
            _config.BoardUsername = Field("Username", _config.BoardUsername);
            _config.BoardPassword = Secret("Password (saved on this device)", _config.BoardPassword);
            _config.BoardFingerprint = Field("Expected SHA256 fingerprint", _config.BoardFingerprint);

            GUILayout.Space(6f);
            _config.UsePrivateKeys = GUILayout.Toggle(
                _config.UsePrivateKeys, "Use private keys from the app data directory");
            if (_config.UsePrivateKeys)
            {
                _config.JumpKeyFile = Field("Jump key filename", _config.JumpKeyFile);
                _jumpPassphrase = Secret("Jump key passphrase", _jumpPassphrase);
                _config.BoardKeyFile = Field("Board key filename", _config.BoardKeyFile);
                _boardPassphrase = Secret("Board key passphrase", _boardPassphrase);
            }

            GUILayout.Space(6f);
            GUILayout.Label("Phone-local ports");
            _config.BoardSshLocalPort = PortField("Ultra96 SSH forward", _config.BoardSshLocalPort);
            _config.PayloadLocalPort = PortField("Payload forward", _config.PayloadLocalPort);
            _config.Player = PortField("Player", _config.Player);

            GUILayout.Space(6f);
        }

        /// <summary>The scrolling log, newest line at the bottom.</summary>
        private void DrawLog()
        {
            GUILayout.Label("Log");

            // Stick to the bottom as lines arrive, the way a log viewer does.
            if (_visualiser.LogVersion != _renderedVersion)
            {
                _renderedVersion = _visualiser.LogVersion;
                _logScroll.y = float.MaxValue;
            }

            _logScroll = GUILayout.BeginScrollView(_logScroll, GUI.skin.box, GUILayout.MinHeight(260f));
            foreach (var line in _visualiser.Log) GUILayout.Label(line);
            GUILayout.EndScrollView();
        }

        /// <summary>Hand the typed secrets to the visualiser, clear them from the form, and Connect.</summary>
        private void StartTunnel()
        {
            _visualiser.JumpPassword = _jumpPassword;
            _visualiser.JumpPassphrase = _jumpPassphrase;
            _visualiser.BoardPassword = _config.BoardPassword;
            _visualiser.BoardPassphrase = _boardPassphrase;

            // Clear only the unsaved secrets; the board password stays because it is saved anyway.
            _jumpPassword = _jumpPassphrase = _boardPassphrase = "";

            try
            {
                _visualiser.Connect();
                _showForm = false;
            }
            catch (Exception exc)
            {
                // e.g. a missing key file: show it on screen, not only in logcat.
                _visualiser.AddLine($"cannot start: {exc.Message}");
            }
        }

        private static string Field(string label, string value)
        {
            GUILayout.BeginHorizontal();
            GUILayout.Label(label, GUILayout.Width(LabelWidth));
            var updated = GUILayout.TextField(value ?? "");
            GUILayout.EndHorizontal();
            return updated;
        }

        private static string Secret(string label, string value)
        {
            GUILayout.BeginHorizontal();
            GUILayout.Label(label, GUILayout.Width(LabelWidth));
            var updated = GUILayout.PasswordField(value ?? "", '*');
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
