using System;
using System.IO;
using Newtonsoft.Json;
using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// Tunnel settings saved on the phone (persistentDataPath), so nothing is retyped between
    /// runs and no network-specific address is compiled in.
    ///
    /// The board password IS saved (it is a shared lab password). The jump-host password is
    /// NOT: it is the user's NUS account, so it is typed each launch and kept in memory only.
    /// Key passphrases are not saved either; only key filenames are.
    /// Why: notes/tunnelled_visualiser.md, "Credentials".
    /// </summary>
    [Serializable]
    public sealed class TunnelConfig
    {
        public const string DefaultJumpHost = "stujump.comp.nus.edu.sg";
        public const string DefaultBoardUser = "xilinx";

        /// <summary>Board loopback port for the echo test, matching ultra96/tunnel_probe_server.py.</summary>
        public const int ProbeRemotePort = 18080;

        public string JumpHost = DefaultJumpHost;
        public int JumpPort = 22;
        public string JumpUsername = "";
        public string JumpKeyFile = "jump_key";
        public string JumpFingerprint = "";

        public string BoardHost = "";
        public int BoardPort = 22;
        public string BoardUsername = DefaultBoardUser;
        public string BoardKeyFile = "ultra96_key";
        public string BoardFingerprint = "";

        /// <summary>
        /// The board hop's password, saved on purpose. The ONLY secret this class may hold:
        /// test_tunnel_config_secrets.py fails if another password field is added.
        /// </summary>
        public string BoardPassword = "";

        public int BoardSshLocalPort = 12222;
        public int PayloadLocalPort = MqttConfig.PlaintextPort;
        public int PayloadRemotePort = MqttConfig.PlaintextPort;

        public bool UsePrivateKeys = true;
        public int Player = 1;

        private static string ConfigFile =>
            Path.Combine(Application.persistentDataPath, "tunnel_config.json");

        public static string KeyDirectory => Application.persistentDataPath;

        /// <summary>Load the saved settings, or defaults if there are none.</summary>
        public static TunnelConfig Load()
        {
            try
            {
                if (File.Exists(ConfigFile))
                {
                    var loaded = JsonConvert.DeserializeObject<TunnelConfig>(File.ReadAllText(ConfigFile));
                    if (loaded != null) return loaded;
                }
            }
            catch (Exception exc) when (exc is IOException || exc is JsonException)
            {
                // A bad config file must not stop the app; the fields can be retyped.
                Debug.LogWarning($"could not read {ConfigFile}: {exc.Message}");
            }

            return new TunnelConfig();
        }

        public void Save()
        {
            try
            {
                File.WriteAllText(ConfigFile, JsonConvert.SerializeObject(this, Formatting.Indented));
            }
            catch (Exception exc) when (exc is IOException || exc is JsonException)
            {
                Debug.LogWarning($"could not write {ConfigFile}: {exc.Message}");
            }
        }

        /// <summary>
        /// Full path of a private key file in the app's data folder. Any folder part typed
        /// in is stripped, so the app can only read keys from that one folder.
        /// </summary>
        public static string ResolveKeyPath(string filename)
        {
            if (string.IsNullOrWhiteSpace(filename))
                throw new InvalidOperationException("Private-key filename is required in key mode.");

            var path = Path.Combine(KeyDirectory, Path.GetFileName(filename.Trim()));
            if (!File.Exists(path)) throw new FileNotFoundException("Private key not found", path);

            return path;
        }

        /// <summary>
        /// Build the settings SshMqttTunnel needs. Secrets come in as arguments, not fields,
        /// so they are never saved.
        /// </summary>
        public SshTunnelSettings ToTunnelSettings(
            string jumpPassword,
            string jumpPassphrase,
            string boardPassword,
            string boardPassphrase,
            bool allowUnknownHostKeysOnce)
        {
            var settings = new SshTunnelSettings
            {
                BoardSshLocalPort = BoardSshLocalPort,
                PayloadLocalPort = PayloadLocalPort,
                PayloadRemotePort = PayloadRemotePort,
                AllowUnknownHostKeysOnce = allowUnknownHostKeysOnce
            };

            settings.Jump.Host = JumpHost?.Trim();
            settings.Jump.Port = JumpPort;
            settings.Jump.Username = JumpUsername?.Trim();
            settings.Jump.Password = jumpPassword;
            settings.Jump.PrivateKeyPath = UsePrivateKeys ? ResolveKeyPath(JumpKeyFile) : null;
            settings.Jump.PrivateKeyPassphrase = jumpPassphrase;
            settings.Jump.ExpectedHostKeySha256 = JumpFingerprint;

            settings.Board.Host = BoardHost?.Trim();
            settings.Board.Port = BoardPort;
            settings.Board.Username = BoardUsername?.Trim();
            settings.Board.Password = boardPassword;
            settings.Board.PrivateKeyPath = UsePrivateKeys ? ResolveKeyPath(BoardKeyFile) : null;
            settings.Board.PrivateKeyPassphrase = boardPassphrase;
            settings.Board.ExpectedHostKeySha256 = BoardFingerprint;

            return settings;
        }
    }
}
