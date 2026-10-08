using System.Collections.Generic;
using UnityEngine;

namespace BridgeRush.Input
{
    public enum GestureSymbol
    {
        Circle,
        Caret,
        Arc,
        Z,
        Infinity,
        Triangle,
        Star
    }

    public interface IGestureRecognizer
    {
        bool TryRecognize(IReadOnlyList<Vector2> strokePoints, out GestureSymbol symbol, out float confidence);
    }

    public class GestureRecognizer : MonoBehaviour, IGestureRecognizer
    {
        [SerializeField] private int minPointsForRecognition = 5;
        [SerializeField] private float minStrokeSizePixels = 40f;
        [SerializeField] private float simplificationEpsilonFraction = 0.06f;
        [SerializeField] private float closedGapFraction = 0.25f;
        [SerializeField] private float minConfidenceThreshold = 0.35f;
        [SerializeField] private float turnSignChangeThresholdDegrees = 35f;

        public bool TryRecognize(IReadOnlyList<Vector2> strokePoints, out GestureSymbol symbol, out float confidence)
        {
            symbol = default;
            confidence = 0f;
            if (strokePoints == null || strokePoints.Count < minPointsForRecognition) return false;
            var bounds = ComputeBounds(strokePoints);
            float diagonal = bounds.size.magnitude;
            if (diagonal < minStrokeSizePixels) return false;
            var points = new List<Vector2>(strokePoints);
            var simplified = SimplifyRDP(points, diagonal * simplificationEpsilonFraction);
            int corners = Mathf.Max(0, simplified.Count - 2);
            float startEndDistance = Vector2.Distance(points[0], points[^1]);
            bool closed = startEndDistance < diagonal * closedGapFraction;
            float totalTurning = ComputeTotalSignedTurningDegrees(points);
            float absTurning = Mathf.Abs(totalTurning);
            int signChanges = CountSignChanges(simplified, turnSignChangeThresholdDegrees);
            bool selfIntersects = HasSelfIntersection(simplified);
            if (closed)
            {
                if (selfIntersects && corners <= 4)
                {
                    symbol = GestureSymbol.Infinity;
                    confidence = 0.55f + 0.05f * Mathf.Clamp(signChanges, 0, 4);
                }
                else if (corners <= 1 && absTurning > 300f)
                {
                    symbol = GestureSymbol.Circle;
                    confidence = ScoreByTurning(absTurning, 360f, 180f);
                }
                else if (corners is >= 2 and <= 4)
                {
                    symbol = GestureSymbol.Triangle;
                    confidence = ScoreByCornerMatch(corners, 3);
                }
                else if (corners >= 5)
                {
                    symbol = GestureSymbol.Star;
                    confidence = ScoreByCornerMatch(corners, 5);
                }
                else
                {
                    return false;
                }
            }
            else
            {
                if (corners == 0)
                {
                    symbol = GestureSymbol.Arc;
                    confidence = Mathf.Clamp01(1f - Mathf.Abs(absTurning - 120f) / 180f);
                }
                else if (corners == 1)
                {
                    symbol = GestureSymbol.Caret;
                    confidence = ScoreByCornerMatch(corners, 1);
                }
                else if (corners == 2)
                {
                    symbol = GestureSymbol.Z;
                    confidence = ScoreByCornerMatch(corners, 2) * (signChanges >= 1 ? 1f : 0.6f);
                }
                else
                {
                    return false;
                }
            }

            confidence = Mathf.Clamp01(confidence);
            return confidence >= minConfidenceThreshold;
        }

        private static float ScoreByCornerMatch(int actualCorners, int targetCorners)
        {
            int diff = Mathf.Abs(actualCorners - targetCorners);
            return Mathf.Clamp01(1f - diff * 0.25f);
        }
        private static float ScoreByTurning(float actualDegrees, float targetDegrees, float tolerance)
        {
            float diff = Mathf.Abs(actualDegrees - targetDegrees);
            return Mathf.Clamp01(1f - diff / tolerance);
        }
        private static Rect ComputeBounds(IReadOnlyList<Vector2> points)
        {
            Vector2 min = points[0];
            Vector2 max = points[0];
            foreach (var point in points)
            {
                min = Vector2.Min(min, point);
                max = Vector2.Max(max, point);
            }
            return new Rect(min, max - min);
        }
        private static float ComputeTotalSignedTurningDegrees(IReadOnlyList<Vector2> points)
        {
            float total = 0f;
            for (int i = 1; i < points.Count - 1; i++)
            {
                Vector2 a = points[i] - points[i - 1];
                Vector2 b = points[i + 1] - points[i];
                if (a.sqrMagnitude < 0.0001f || b.sqrMagnitude < 0.0001f) continue;
                total += Vector2.SignedAngle(a, b);
            }
            return total;
        }
        private static int CountSignChanges(IReadOnlyList<Vector2> points, float angleThresholdDegrees)
        {
            int changes = 0;
            float lastSign = 0f;
            for (int i = 1; i < points.Count - 1; i++)
            {
                Vector2 a = points[i] - points[i - 1];
                Vector2 b = points[i + 1] - points[i];
                if (a.sqrMagnitude < 0.0001f || b.sqrMagnitude < 0.0001f) continue;

                float angle = Vector2.SignedAngle(a, b);
                if (Mathf.Abs(angle) < angleThresholdDegrees) continue;

                float sign = Mathf.Sign(angle);
                if (lastSign != 0f && sign != lastSign) changes++;
                lastSign = sign;
            }
            return changes;
        }
        private static List<Vector2> SimplifyRDP(List<Vector2> points, float epsilon)
        {
            if (points.Count < 3) return points;

            float maxDistance = 0f;
            int index = 0;
            Vector2 start = points[0];
            Vector2 end = points[^1];

            for (int i = 1; i < points.Count - 1; i++)
            {
                float distance = PerpendicularDistance(points[i], start, end);
                if (distance > maxDistance)
                {
                    maxDistance = distance;
                    index = i;
                }
            }
            if (maxDistance <= epsilon)
            {
                return new List<Vector2> { start, end };
            }
            var left = SimplifyRDP(points.GetRange(0, index + 1), epsilon);
            var right = SimplifyRDP(points.GetRange(index, points.Count - index), epsilon);
            left.RemoveAt(left.Count - 1);
            left.AddRange(right);
            return left;
        }

        private static float PerpendicularDistance(Vector2 point, Vector2 lineStart, Vector2 lineEnd)
        {
            Vector2 line = lineEnd - lineStart;
            float lineLength = line.magnitude;
            if (lineLength < 0.0001f) return Vector2.Distance(point, lineStart);
            Vector2 toPoint = point - lineStart;
            float cross = line.x * toPoint.y - line.y * toPoint.x;
            return Mathf.Abs(cross) / lineLength;
        }

        private static bool HasSelfIntersection(List<Vector2> points)
        {
            int n = points.Count;
            for (int i = 0; i < n - 1; i++)
            {
                for (int j = i + 2; j < n - 1; j++)
                {
                    if (i == 0 && j == n - 2) continue;
                    if (SegmentsIntersect(points[i], points[i + 1], points[j], points[j + 1])) return true;
                }
            }
            return false;
        }

        private static bool SegmentsIntersect(Vector2 p1, Vector2 p2, Vector2 p3, Vector2 p4)
        {
            float d1 = Cross(p4 - p3, p1 - p3);
            float d2 = Cross(p4 - p3, p2 - p3);
            float d3 = Cross(p2 - p1, p3 - p1);
            float d4 = Cross(p2 - p1, p4 - p1);
            return ((d1 > 0f && d2 < 0f) || (d1 < 0f && d2 > 0f)) &&
                   ((d3 > 0f && d4 < 0f) || (d3 < 0f && d4 > 0f));
        }

        private static float Cross(Vector2 a, Vector2 b) => a.x * b.y - a.y * b.x;
    }
}
