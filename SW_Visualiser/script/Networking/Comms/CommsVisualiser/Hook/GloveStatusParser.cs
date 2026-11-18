using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Comms.Visualiser
{
    /// <summary>
    /// Reads the board's retained status/gloves message: whether each glove is ready, down or
    /// offline. Our wire format only, with no Unity types, so it runs in a plain console check.
    /// The board's envelope.py holds the same topic and values, so change both together.
    /// </summary>
    public static class GloveStatusParser
    {
        public const string Topic = "status/gloves";

        // ready: streaming with a working IMU. down: silent, too slow, or no IMU reading.
        // offline: the board's client is not running.
        private static readonly string[] States = { "ready", "down", "offline" };

        private const int Players = 2;

        /// <summary>
        /// True with each glove's state, gloves[0] being player 1's. On false, reason says why
        /// the message was dropped.
        ///
        /// Takes the envelope's inner payload, since MqttConnection checks the envelope and
        /// drops duplicates before it calls a handler.
        /// </summary>
        public static bool TryRead(string json, out string[] gloves, out string reason)
        {
            gloves = null;
            reason = null;

            JToken token;
            try
            {
                token = JToken.Parse(json ?? "");
            }
            catch (JsonException exc)
            {
                reason = $"{Topic}: payload is not JSON: {exc.Message}";
                return false;
            }

            // An envelope with "payload": null passes MqttConnection's check, and arrives as "null".
            if (!(token is JObject payload))
            {
                reason = $"{Topic}: payload is not an object";
                return false;
            }

            var states = new string[Players];
            for (var player = 1; player <= Players; player++)
            {
                var state = payload[$"player{player}"];
                if (state == null || state.Type != JTokenType.String ||
                    Array.IndexOf(States, state.Value<string>()) < 0)
                {
                    reason = $"{Topic}: player{player} is not one of ready, down, offline";
                    return false;
                }
                states[player - 1] = state.Value<string>();
            }

            gloves = states;
            return true;
        }
    }
}
