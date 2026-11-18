# Tunnelled phone visualiser

The visualiser for when Mosquitto runs on the **Ultra96's loopback**
instead of the laptop. Reaches it through a two-hop SSH tunnel that the phone opens.

It is the Comms test client and has run end to end on a phone; it is separate
from the team's `SW_Visualiser`.

## Setup

The phone needs the NUS VPN on. See the [network rule](../../../README.md#network-rule).

1. Build this project in Unity 6000.6.0f1 (Android, IL2CPP, ARM64) and install the APK.
2. In the app, fill in the jump host, NUS username, board host and board password. Turn **off**
   "Use private keys": the phone logs in to stujump by password, and turning it on without key
   files fails.
3. First run only: turn on "accept unknown host keys and print their fingerprints", connect,
   copy both printed fingerprints into the "Expected SHA256 fingerprint" fields, then turn the
   toggle off.

The app saves everything except the NUS password, which you type each launch.

## What is a copy and what is not

`Envelope.cs`, `SeenEvents.cs` and `MqttConfig.cs` are **byte-identical copies** of
`phone/demo-visualiser/Assets/Scripts/`. They encode the wire contract with
`laptop/mqtt_client.py`, and `laptop/tests/test_unity_shared_scripts.py` fails if they drift.

Fix them in `demo-visualiser` and copy across. Do not edit them here.

`phone/comms-visualiser/` in turn holds byte-identical copies of `Envelope.cs`, `SeenEvents.cs`,
`MqttConfig.cs`, `SshMqttTunnel.cs` and `TunnelConfig.cs` from this project. The same test checks
them, so fix those here and copy them on.

The rest is specific to this project:

| file | |
|---|---|
| `SshMqttTunnel.cs` | the two-hop SSH tunnel |
| `TunnelConfig.cs` | addresses, ports, fingerprints, key filenames -- never secrets |
| `TunnelledVisualiser.cs` | the two supervised legs and their dependency |
| `TunnelledUI.cs` | IMGUI form and log |

`MqttVisualiser.cs` and `VisualiserUI.cs` from the LAN client must **not** be copied in --
both bootstrap at startup, so two visualisers would both use the same broker client id. The
same test guards that.

## Build

Separate `applicationIdentifier` (`com.b16.tunnelledvisualiser`), so this APK installs
alongside the LAN one rather than replacing it. Unity 6000.6.0f1, IL2CPP, ARM64.

Private keys go to `Application.persistentDataPath` via `adb push` and are never packaged.
