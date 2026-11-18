using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Comms.Visualiser
{
    /// <summary>
    /// Builds the latency echo, a gesture's seq and timestamp sent back on viz/echo/{player} so the
    /// board can time the trip. The phone's time is the envelope's ts. It has no Unity types, so a
    /// console check can run it. notes/latency.md 3.13.
    /// </summary>
    public static class GestureEcho
    {
        /// <summary>Every player's gestures, on a filter of its own so the game's handler keeps game/gesture/N.</summary>
        public const string GestureFilter = "game/gesture/+";

        public static string Topic(int player) => $"viz/echo/{player}";

        /// <summary>Build the echo's JSON from a gesture's inner payload, or return false with the reason.</summary>
        public static bool TryBuild(string json, out string echo, out string reason)
        {
            echo = null;
            reason = null;

            JToken token;
            try
            {
                token = JToken.Parse(json ?? "");
            }
            catch (JsonException exc)
            {
                reason = $"gesture payload is not JSON: {exc.Message}";
                return false;
            }

            if (!(token is JObject payload))
            {
                reason = "gesture payload is not an object";
                return false;
            }

            var seq = payload["seq"];
            if (seq == null || seq.Type != JTokenType.Integer)
            {
                reason = "gesture payload.seq is not an integer";
                return false;
            }

            // Dispatch turns NaN and infinity into strings, so this rejects them too
            var timestamp = payload["timestamp"];
            if (timestamp == null || (timestamp.Type != JTokenType.Float && timestamp.Type != JTokenType.Integer))
            {
                reason = "gesture payload.timestamp is not a number";
                return false;
            }

            // built from tokens, not text, so the phone's culture can't change a number
            echo = new JObject
            {
                ["seq"] = seq.Value<long>(),
                ["t_event"] = timestamp.Value<double>(),
            }.ToString(Formatting.None);
            return true;
        }
    }
}
