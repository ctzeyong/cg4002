using System;
using System.Collections.Generic;

namespace BridgeRush.Match
{
    // Times how long a summon takes to show on this phone: from the board's "accepted" game/event arriving to the first game/state
    // snapshot that contains the unit. Both are read from this phone's own clock, so no clock sync is needed. The snapshot only
    // goes out on the board's next tick, so the wait is about half a tick on average. Plain C# (no Unity types), so it can be
    // checked outside the Editor. Times are in seconds.
    public class SummonLatencyTracker
    {
        private const float KeepSeconds = 5f;

        private readonly Dictionary<int, float> accepted = new Dictionary<int, float>();   // unit id -> when its event arrived
        private readonly Dictionary<int, float> seenFirst = new Dictionary<int, float>();  // unit id -> when its snapshot arrived, if first
        private readonly HashSet<int> known = new HashSet<int>();
        private double sum;
        private float min = float.MaxValue;
        private float max;

        public int Count { get; private set; }
        public float AverageMs => Count > 0 ? (float)(sum / Count * 1000.0) : 0f;
        public float MinMs => Count > 0 ? min * 1000f : 0f;
        public float MaxMs => Count > 0 ? max * 1000f : 0f;

        // Forget every figure.
        public void Reset()
        {
            NewMatch();
            sum = 0.0;
            min = float.MaxValue;
            max = 0f;
            Count = 0;
        }

        // A new match: unit ids start again, so forget what was waiting. The figures stay.
        public void NewMatch()
        {
            accepted.Clear();
            seenFirst.Clear();
            known.Clear();
        }

        // The board accepted a summon and named the unit.
        public void NoteAccepted(int unitId, float now)
        {
            if (unitId < 0) return;
            if (seenFirst.TryGetValue(unitId, out float seenAt))
            {
                // The snapshot beat its own event to the phone: the unit was already on screen, so the wait counts as none.
                seenFirst.Remove(unitId);
                Record(Math.Max(0f, seenAt - now));
                return;
            }
            Forget(accepted, now);
            accepted[unitId] = now;
        }

        // A snapshot has this unit. Only its first appearance counts; later snapshots are ignored.
        public void NoteSeen(int unitId, float now)
        {
            if (unitId < 0 || !known.Add(unitId)) return;
            if (accepted.TryGetValue(unitId, out float acceptedAt))
            {
                accepted.Remove(unitId);
                Record(now - acceptedAt);
                return;
            }
            Forget(seenFirst, now);
            seenFirst[unitId] = now;
        }

        private void Record(float seconds)
        {
            if (seconds < 0f || float.IsNaN(seconds) || float.IsInfinity(seconds)) return;
            Count++;
            sum += seconds;
            if (seconds < min) min = seconds;
            if (seconds > max) max = seconds;
        }

        // Drop entries that waited too long, so a unit that never shows (or an enemy's) cannot pile up.
        private static void Forget(Dictionary<int, float> map, float now)
        {
            if (map.Count == 0) return;
            List<int> old = null;
            foreach (var pair in map)
                if (now - pair.Value > KeepSeconds) (old ??= new List<int>()).Add(pair.Key);
            if (old != null) foreach (int id in old) map.Remove(id);
        }
    }
}
