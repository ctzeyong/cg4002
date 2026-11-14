using Comms.Visualiser;
using UnityEngine;

namespace BridgeRush.Networking
{
    // Pre-fills the Comms panel's connection details the first time it runs, so only the usernames and passwords are typed.
    // It writes into the saved tunnel config before the panel loads it, and only into fields that are still empty, so
    // anything typed or pasted in the panel is never overwritten. TunnelConfig itself belongs to the comms team and is
    // not edited; change the board or the fingerprints here if the lab moves the board or re-keys it.
    //
    // The fingerprints are the public SSH host-key hashes of the two machines (what `ssh` prints on first connect), not
    // secrets. BoardPassword is the shared lab password for the Ultra96 (TunnelConfig saves it on the phone by design).
    // It is in source on purpose, at the user's request, so do not push this repo anywhere public. The usernames and the
    // NUS jump-host password are not set here: the NUS login is a personal account and is typed each launch.
    public static class CommsDefaults
    {
        public const string BoardHost = "makerslab-fpga-43.ddns.comp.nus.edu.sg";
        public const string JumpHostFingerprint = "SHA256:UZRh6MN1S3Q9DI2OEH4POiJQ4fEaxrpI83ZSTTZtMYw";
        public const string BoardPassword = "b16-u96";
        public const string BoardFingerprint = "SHA256:vrFwqkqWIfyDZ1S66aJb1gLy59wx3LakOqhnyQJEOhg";

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void Apply()
        {
            var config = TunnelConfig.Load();
            bool changed = false;
            changed |= Fill(ref config.BoardHost, BoardHost);
            changed |= Fill(ref config.JumpFingerprint, JumpHostFingerprint);
            changed |= Fill(ref config.BoardFingerprint, BoardFingerprint);
            changed |= Fill(ref config.BoardPassword, BoardPassword);
            if (changed) config.Save();
        }

        private static bool Fill(ref string field, string value)
        {
            if (!string.IsNullOrWhiteSpace(field)) return false;
            field = value;
            return true;
        }
    }
}
