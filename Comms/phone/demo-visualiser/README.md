# Demo phone visualiser

The Comms test client for the laptop-broker setup, which was evaluated but is not used in the
final system. It connects straight to Mosquitto on the laptop and shows each AI event it
receives. It is not the team's app, and it does not play the game.

How to build it and run the setup is in the
[Comms subsystem guide, section 2](../../README.md#alternative-evaluated-broker-on-the-laptop).

`Assets/Scripts/MqttTls.cs` switches to mutual TLS on port 8883 when `ca.crt` and a client
bundle are in the app's data folder. That path was only run in the Unity Editor, never on a
phone. See [Mutual TLS: built, not used](../../README.md#mutual-tls-built-not-used).
