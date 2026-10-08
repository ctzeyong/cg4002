using UnityEngine;

namespace BridgeRush.Gameplay
{
    // helper class to compute the extent of a model along a given direction, and to normalize its size and position
    public static class ModelFit
    {
        //measure a model real woirld space extent along a given direction, by checking all corners of all renderers in the model hierarchy
        public static float ExtentAlong(GameObject model, Vector3 direction)
        {
            float min = float.MaxValue;
            float max = float.MinValue;

            foreach (var renderer in model.GetComponentsInChildren<Renderer>())
            {
                Bounds local = renderer.localBounds;
                Matrix4x4 toWorld = renderer.localToWorldMatrix;

                for (int corner = 0; corner < 8; corner++)
                {
                    Vector3 offset = new(
                        (corner & 1) == 0 ? -local.extents.x : local.extents.x,
                        (corner & 2) == 0 ? -local.extents.y : local.extents.y,
                        (corner & 4) == 0 ? -local.extents.z : local.extents.z);

                    float along = Vector3.Dot(toWorld.MultiplyPoint3x4(local.center + offset), direction);
                    min = Mathf.Min(min, along);
                    max = Mathf.Max(max, along);
                }
            }

            return max > min ? max - min : 0f;
        }
        // get the bounds of a model in world space
        public static bool TryGetBounds(GameObject model, out Bounds bounds)
        {
            var renderers = model.GetComponentsInChildren<Renderer>();
            if (renderers.Length == 0)
            {
                bounds = default;
                return false;
            }

            bounds = renderers[0].bounds;
            for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);
            return true;
        }
    }
}
