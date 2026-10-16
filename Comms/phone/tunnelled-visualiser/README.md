# Tunnelled phone visualiser

The visualiser for the configuration where Mosquitto runs on the **Ultra96's loopback**
instead of the laptop. Reaches it through a two-hop SSH nest owned by the phone.

Setup (network, credentials, fingerprints) is in [`Comms/README.md`](../../README.md#phone).
It is the Configuration B test client and has run end to end on a phone; it is separate
from the team's `SW_Visualiser`.

## What is a copy and what is not

`Envelope.cs`, `SeenEvents.cs` and `MqttConfig.cs` are **byte-identical copies** of
`phone/demo-visualiser/Assets/Scripts/`. They encode the wire contract with
`laptop/mqtt_client.py`, and `laptop/tests/test_unity_shared_scripts.py` fails if they drift.

Fix them in `demo-visualiser` and copy across. Do not edit them here.

The rest is specific to this project:

| file | |
|---|---|
| `SshMqttTunnel.cs` | the nest, adapted from `unity_tunnel_spike/NestedSshTunnel.cs` |
| `TunnelConfig.cs` | addresses, ports, fingerprints, key filenames -- never secrets |
| `TunnelledVisualiser.cs` | the two supervised legs and their dependency |
| `TunnelledUI.cs` | IMGUI form and log |

`MqttVisualiser.cs` and `VisualiserUI.cs` from the LAN client must **not** be copied in --
both bootstrap at startup, so two visualisers would fight over the broker client id. The
same test guards that.

## Build

Separate `applicationIdentifier` (`com.b16.tunnelledvisualiser`), so this APK installs
alongside the LAN one rather than replacing it. Unity 6000.6.0f1, IL2CPP, ARM64, as in
`unity_visualiser.md`.

Private keys go to `Application.persistentDataPath` via `adb push` and are never packaged.
