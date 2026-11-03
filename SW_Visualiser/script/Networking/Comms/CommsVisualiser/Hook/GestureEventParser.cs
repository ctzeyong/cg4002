using System;
using Newtonsoft.Json.Linq;

namespace Comms.Visualiser
{
    /// <summary>
    /// Reads one AI event off game/gesture/{player} and returns the gesture's name, or why
    /// not. Our wire format only: no Unity and no Vis types, so it runs in a plain console
    /// check (V0). AiGestureHook maps the name onto the game's enum.
    ///
    /// It has no list of gesture names. It checks only the name's shape; the game's
    /// GestureSymbol enum is the list. notes/integration/vis_integration.md section 4.4.
    /// </summary>
    public sealed class GestureEventParser
    {
        private readonly SeenEvents _seen;

        public GestureEventParser(int capacity = SeenEvents.DefaultCapacity)
        {
            _seen = new SeenEvents(capacity);
        }

        /// <summary>
        /// True with the gesture's name if the event should act. On true, reason is null or
        /// a warning that does not stop the event. On false, reason says why it was dropped.
        /// </summary>
        /// <param name="expectedTopic">
        /// This phone's topic now. Any other is dropped: there is no unsubscribe, so a
        /// player switch leaves the old topic subscribed (section 10, F2).
        /// </param>
        public bool TryRead(string topic, string json, string expectedTopic,
            out string gesture, out string reason)
        {
            gesture = null;
            reason = null;

            if (!string.Equals(topic, expectedTopic, StringComparison.Ordinal))
            {
                reason = $"ignored {topic}: this phone listens on {expectedTopic}";
                return false;
            }

            JObject envelope;
            try
            {
                envelope = Envelope.Parse(json);
            }
            catch (EnvelopeException exc)
            {
                reason = $"bad envelope on {topic}: {exc.Message}";
                return false;
            }

            // QoS 1 can deliver twice, and a later QoS change on the phone must not double-spawn.
            var eventId = envelope["event_id"].Value<string>();
            if (!_seen.Check(eventId))
            {
                reason = $"duplicate {eventId} ignored";
                return false;
            }

            // Envelope.Parse accepts "payload": null, since the key is present.
            if (!(envelope["payload"] is JObject payload))
            {
                reason = $"{eventId}: payload is not an object";
                return false;
            }

            var name = payload["gesture"];
            if (name == null || name.Type != JTokenType.String)
            {
                reason = $"{eventId}: payload.gesture is not a string";
                return false;
            }

            // Letters only, because Enum.TryParse also takes "3" as a value (classifier
            // label "3" would become Z), trims " star", and splits "circle,star".
            var text = name.Value<string>();
            if (!IsLettersOnly(text))
            {
                reason = $"unknown gesture '{text}'";
                return false;
            }

            gesture = text;

            // The topic carries attribution; payload.player is only compared.
            var player = payload["player"];
            var topicPlayer = topic.Substring(topic.LastIndexOf('/') + 1);
            if (player != null && player.Type == JTokenType.Integer && player.ToString() != topicPlayer)
                reason = $"{eventId}: payload.player {player} differs from {topic}; the topic wins";

            return true;
        }

        private static bool IsLettersOnly(string text)
        {
            if (string.IsNullOrEmpty(text)) return false;
            foreach (var c in text)
                if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'))) return false;
            return true;
        }
    }
}
