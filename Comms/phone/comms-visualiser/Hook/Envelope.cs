using System;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Comms.Visualiser
{
    /// <summary>
    /// A message on a game topic that is not a valid envelope. MQTT keeps messages
    /// separate, so drop just this one and keep the connection.
    /// </summary>
    public class EnvelopeException : Exception
    {
        public EnvelopeException(string message) : base(message) { }
    }

    /// <summary>
    /// The five-field envelope on every MQTT message: event_id, source, seq, ts, payload.
    /// Matches the Python side (laptop mqtt_client, ultra96 envelope.py); notes/mqtt_topics.md.
    /// Fields are checked by type, not just presence, the same way the laptop checks them.
    /// </summary>
    public static class Envelope
    {
        /// <summary>Wall-clock seconds, matching Python's time.time(). Displayed, not timed with.</summary>
        public static double UnixNow() =>
            (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;

        /// <summary>
        /// Wrap a payload for publishing. event_id is source:session:seq, e.g.
        /// "phone-visualiser-1:3f2a1b9c:17", so ids stay unique after an app restart.
        /// </summary>
        public static string Build(JToken payload, int seq, string source, string session)
        {
            var envelope = new JObject
            {
                ["event_id"] = $"{source}:{session}:{seq}",
                ["source"] = source,
                ["seq"] = seq,
                ["ts"] = UnixNow(),
                ["payload"] = payload,
            };
            return envelope.ToString(Formatting.None);
        }

        /// <summary>Decode and validate one received payload.</summary>
        /// <exception cref="EnvelopeException">
        /// Not a JSON object, or a field missing, empty or of the wrong type.
        /// </exception>
        public static JObject Parse(string raw)
        {
            JObject obj;
            try
            {
                obj = JObject.Parse(raw);
            }
            catch (JsonException exc)
            {
                throw new EnvelopeException($"envelope must be a JSON object: {exc.Message}");
            }

            RequireType(obj, "event_id", JTokenType.String, "a string");
            RequireType(obj, "source", JTokenType.String, "a string");
            // Integer does not include true/false, so "seq": true is rejected.
            RequireType(obj, "seq", JTokenType.Integer, "an integer");
            RequireNumber(obj, "ts");

            // payload must exist, but its contents are the game's business, so not checked.
            if (obj["payload"] == null) throw new EnvelopeException("envelope missing 'payload'");

            if (obj["event_id"].Value<string>().Length == 0)
                throw new EnvelopeException("event_id must not be empty");

            return obj;
        }

        private static JToken Require(JObject obj, string key)
        {
            var value = obj[key];
            if (value == null) throw new EnvelopeException($"envelope missing '{key}'");
            return value;
        }

        private static void RequireType(JObject obj, string key, JTokenType type, string described)
        {
            var value = Require(obj, key);
            if (value.Type != type)
                throw new EnvelopeException($"{key} must be {described}, got {value.Type}");
        }

        private static void RequireNumber(JObject obj, string key)
        {
            var value = Require(obj, key);
            if (value.Type != JTokenType.Integer && value.Type != JTokenType.Float)
                throw new EnvelopeException($"{key} must be a number, got {value.Type}");
        }
    }
}
