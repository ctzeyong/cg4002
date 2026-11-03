using System.Collections.Generic;

namespace Comms.Visualiser
{
    /// <summary>
    /// Remembers recent event_ids so an event that QoS 1 delivers twice is shown only once.
    /// Holds at most 4096 ids and forgets the oldest first, so memory stays bounded
    /// (a repeat arrives soon after the original).
    /// </summary>
    public class SeenEvents
    {
        public const int DefaultCapacity = 4096;

        private readonly int _capacity;
        private readonly Queue<string> _order = new Queue<string>();
        private readonly HashSet<string> _ids = new HashSet<string>();

        public SeenEvents(int capacity = DefaultCapacity)
        {
            _capacity = capacity;
        }

        public int Count => _ids.Count;

        /// <summary>Record <paramref name="eventId"/>; true if it is new, false if already seen.</summary>
        public bool Check(string eventId)
        {
            if (!_ids.Add(eventId)) return false;

            _order.Enqueue(eventId);
            if (_order.Count > _capacity) _ids.Remove(_order.Dequeue());
            return true;
        }
    }
}
