using UnityEngine;

namespace BridgeRush.Gameplay
{
    // Keeps chosen objects (the towers, the capture bubbles) from being hidden behind real-world objects by AR occlusion, while
    // everything else is still occluded. AR occlusion works through the depth buffer: the camera background writes the real
    // world's depth there. For each renderer we add an invisible copy that draws BEFORE it and resets the depth over its outline
    // (Resources/BridgeRushTowerDepthClear.shader), so the real object's draw then passes its depth test.
    public static class ArDepthClear
    {
        private static Material material;

        // The shared material, or null when the shader is missing (then the objects can still be occluded).
        private static Material GetMaterial()
        {
            if (material != null) return material;
            var shader = Resources.Load<Shader>("BridgeRushTowerDepthClear");
            if (shader == null) shader = Shader.Find("BridgeRush/TowerDepthClear");
            if (shader == null || !shader.isSupported)
            {
                Debug.LogWarning("[ArDepthClear] BridgeRush/TowerDepthClear shader missing: AR occlusion can still hide towers and bubbles.");
                return null;
            }
            material = new Material(shader) { name = "ArDepthClear" };
            return material;
        }

        // Adds a depth-clear copy for every mesh and skinned-mesh renderer under `root`. A skinned copy shares the original's
        // bones, so it follows the animation.
        public static void AddCopies(GameObject root)
        {
            if (GetMaterial() == null) return;

            foreach (var renderer in root.GetComponentsInChildren<Renderer>())
            {
                if (renderer is SkinnedMeshRenderer skinned && skinned.sharedMesh != null)
                {
                    var copy = NewCopyObject(renderer);
                    var clone = copy.AddComponent<SkinnedMeshRenderer>();
                    clone.sharedMesh = skinned.sharedMesh;
                    clone.bones = skinned.bones;
                    clone.rootBone = skinned.rootBone;
                    clone.updateWhenOffscreen = true;
                    Setup(clone, skinned.sharedMesh.subMeshCount);
                }
                else if (renderer is MeshRenderer && renderer.TryGetComponent(out MeshFilter filter) && filter.sharedMesh != null)
                {
                    var copy = NewCopyObject(renderer);
                    copy.AddComponent<MeshFilter>().sharedMesh = filter.sharedMesh;
                    Setup(copy.AddComponent<MeshRenderer>(), filter.sharedMesh.subMeshCount);
                }
            }
        }

        // A sphere-shaped depth-clear disc of the given world diameter under `parent`, for things that are not meshes of their
        // own (a bubble's progress ring is a line, so the disc is made larger than the ring). Returns it, or null.
        public static Renderer AddSphere(Transform parent, float diameter, int layer)
        {
            if (GetMaterial() == null) return null;
            var sphere = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            sphere.name = "DepthClear";
            Object.Destroy(sphere.GetComponent<Collider>());
            sphere.transform.SetParent(parent, false);
            sphere.transform.localScale = Vector3.one * diameter;
            sphere.layer = layer;
            var renderer = sphere.GetComponent<MeshRenderer>();
            Setup(renderer, 1);
            return renderer;
        }

        private static GameObject NewCopyObject(Renderer original)
        {
            var copy = new GameObject(original.name + "_DepthClear");
            copy.layer = original.gameObject.layer;
            copy.transform.SetParent(original.transform.parent, false);
            copy.transform.SetLocalPositionAndRotation(original.transform.localPosition, original.transform.localRotation);
            copy.transform.localScale = original.transform.localScale;
            copy.SetActive(original.gameObject.activeSelf);
            return copy;
        }

        private static void Setup(Renderer clone, int subMeshes)
        {
            var materials = new Material[Mathf.Max(1, subMeshes)];
            for (int i = 0; i < materials.Length; i++) materials[i] = material;
            clone.sharedMaterials = materials;
            clone.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            clone.receiveShadows = false;
        }
    }
}
