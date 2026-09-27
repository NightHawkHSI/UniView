"""The editor script UniView puts in exported Unity projects (Assets/UniView/Editor/UniViewBuilder.cs).

Unity builds the prefabs and scenes itself from the JSON descriptions UniView writes to
Assets/UniView/Build/, using its own API - no guessing at .prefab/.unity YAML or at the IDs of meshes
inside imported GLBs. Plain C# 4 so it compiles in old Unity versions too.
"""

BUILDER_CS = r'''// Written by UniView (Game Asset Viewer). Builds the game's prefabs and scenes from the descriptions
// in Assets/UniView/Build/ the first time the project opens; run it again with UniView > Rebuild.
#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace UniView
{
    [Serializable]
    public class LightInfo
    {
        public bool present;
        public int type = 1;
        public float[] color;
        public float intensity = 1f;
        public float range = 10f;
        public float spotAngle = 30f;
        public bool enabled = true;
    }

    [Serializable]
    public class Node
    {
        public string name;
        public int parent = -1;
        public bool active = true;
        public bool rendererEnabled = true;
        public float[] pos;
        public float[] rot;
        public float[] scale;
        public string model;
        public string builtin;
        public bool skinned;
        public LightInfo light;
    }

    [Serializable]
    public class BatchRef
    {
        public string model;
    }

    [Serializable]
    public class Description
    {
        public int version;
        public string kind;
        public string target;
        public Node[] nodes;
        public BatchRef[] batches;
    }

    [InitializeOnLoad]
    public static class Builder
    {
        const string BuildDir = "Assets/UniView/Build";
        static Material defaultMaterial;

        static Builder()
        {
            EditorApplication.delayCall += BuildMissingIfAny;
        }

        static void BuildMissingIfAny()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            foreach (string file in Descriptions())
            {
                Description d = Load(file);
                if (d != null && !File.Exists(d.target)) { BuildMissing(); return; }
            }
        }

        [MenuItem("UniView/Rebuild prefabs and scenes")]
        public static void RebuildAll() { Build(false); }

        [MenuItem("UniView/Build missing prefabs and scenes")]
        public static void BuildMissing() { Build(true); }

        static string[] Descriptions()
        {
            if (!Directory.Exists(BuildDir)) return new string[0];
            string[] files = Directory.GetFiles(BuildDir, "*.json", SearchOption.AllDirectories);
            Array.Sort(files, StringComparer.Ordinal);
            return files;
        }

        static Description Load(string file)
        {
            try { return JsonUtility.FromJson<Description>(File.ReadAllText(file)); }
            catch (Exception e) { Debug.LogWarning("UniView: can't read " + file + ": " + e.Message); return null; }
        }

        static void Build(bool onlyMissing)
        {
            var todo = new List<Description>();
            foreach (string file in Descriptions())
            {
                Description d = Load(file);
                if (d == null || d.nodes == null || d.nodes.Length == 0) continue;
                if (onlyMissing && File.Exists(d.target)) continue;
                todo.Add(d);
            }
            if (todo.Count == 0) return;
            // Prefabs first, then scenes (building a scene replaces the open one).
            todo.Sort(delegate(Description a, Description b) { return (a.kind == "scene").CompareTo(b.kind == "scene"); });
            bool anyScene = todo.Exists(delegate(Description d) { return d.kind == "scene"; });
            string openScene = EditorSceneManager.GetActiveScene().path;
            if (anyScene && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;

            var parts = new Dictionary<string, Renderer>();
            int prefabs = 0, scenes = 0, failed = 0;
            try
            {
                for (int i = 0; i < todo.Count; i++)
                {
                    Description d = todo[i];
                    if (EditorUtility.DisplayCancelableProgressBar("UniView", "Building " + d.target, (float)i / todo.Count)) break;
                    try
                    {
                        if (d.kind == "scene") { BuildScene(d, parts); scenes++; }
                        else { BuildPrefab(d, parts); prefabs++; }
                    }
                    catch (Exception e) { failed++; Debug.LogWarning("UniView: couldn't build " + d.target + ": " + e.Message); }
                }
            }
            finally
            {
                EditorUtility.ClearProgressBar();
            }
            AssetDatabase.Refresh();
            if (anyScene)
            {
                if (!string.IsNullOrEmpty(openScene) && File.Exists(openScene)) EditorSceneManager.OpenScene(openScene);
                else EditorSceneManager.NewScene(NewSceneSetup.DefaultGameObjects, NewSceneMode.Single);
            }
            Debug.Log("UniView: built " + prefabs + " prefab(s) and " + scenes + " scene(s)"
                      + (failed > 0 ? ", " + failed + " failed (see warnings)" : ""));
        }

        static Vector3 V(float[] a, Vector3 fallback)
        {
            return a != null && a.Length >= 3 ? new Vector3(a[0], a[1], a[2]) : fallback;
        }

        static Quaternion Q(float[] a)
        {
            return a != null && a.Length >= 4 ? new Quaternion(a[0], a[1], a[2], a[3]) : Quaternion.identity;
        }

        static GameObject[] BuildObjects(Description d, Dictionary<string, Renderer> parts, bool rootsCanBeInactive)
        {
            var objects = new GameObject[d.nodes.Length];
            for (int i = 0; i < d.nodes.Length; i++)
            {
                Node n = d.nodes[i];
                var go = new GameObject(string.IsNullOrEmpty(n.name) ? "GameObject" : n.name);
                if (n.parent >= 0 && n.parent < i) go.transform.SetParent(objects[n.parent].transform, false);
                go.transform.localPosition = V(n.pos, Vector3.zero);
                go.transform.localRotation = Q(n.rot);
                go.transform.localScale = V(n.scale, Vector3.one);
                objects[i] = go;
            }
            for (int i = 0; i < d.nodes.Length; i++)
            {
                Node n = d.nodes[i];
                Transform root = objects[i].transform.root;
                if (!string.IsNullOrEmpty(n.model)) AddRenderer(objects[i], n, root, parts);
                else if (!string.IsNullOrEmpty(n.builtin)) AddBuiltin(objects[i], n);
                if (n.light != null && n.light.present) AddLight(objects[i], n.light);
            }
            for (int i = 0; i < d.nodes.Length; i++)
                if (!d.nodes[i].active && (rootsCanBeInactive || d.nodes[i].parent >= 0)) objects[i].SetActive(false);
            return objects;
        }

        static void BuildPrefab(Description d, Dictionary<string, Renderer> parts)
        {
            GameObject root = BuildObjects(d, parts, false)[0];
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(d.target));
#if UNITY_2018_3_OR_NEWER
                PrefabUtility.SaveAsPrefabAsset(root, d.target);
#else
                PrefabUtility.CreatePrefab(d.target, root);
#endif
            }
            finally
            {
                UnityEngine.Object.DestroyImmediate(root);
            }
        }

        static void BuildScene(Description d, Dictionary<string, Renderer> parts)
        {
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            BuildObjects(d, parts, true);
            if (d.batches != null && d.batches.Length > 0)
            {
                // Static batching: the game merged these objects' meshes, already in world space.
                var holder = new GameObject("Static batches (UniView)");
                foreach (BatchRef b in d.batches)
                {
                    Renderer src = ModelRenderer(b.model, parts);
                    if (src == null) continue;
                    var go = new GameObject(Path.GetFileNameWithoutExtension(b.model));
                    go.transform.SetParent(holder.transform, false);
                    Mesh mesh = src.GetComponent<MeshFilter>() != null ? src.GetComponent<MeshFilter>().sharedMesh : null;
                    if (mesh == null) continue;
                    go.AddComponent<MeshFilter>().sharedMesh = mesh;
                    go.AddComponent<MeshRenderer>().sharedMaterials = Materials(src, mesh);
                    go.isStatic = true;
                }
            }
            Directory.CreateDirectory(Path.GetDirectoryName(d.target));
            EditorSceneManager.SaveScene(scene, d.target);
        }

        static Renderer ModelRenderer(string path, Dictionary<string, Renderer> parts)
        {
            Renderer r;
            if (parts.TryGetValue(path, out r)) return r;
            r = null;
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (model != null)
            {
                r = model.GetComponentInChildren<SkinnedMeshRenderer>(true);
                if (r == null) r = model.GetComponentInChildren<MeshRenderer>(true);
            }
            if (r == null) Debug.LogWarning("UniView: no mesh in " + path);
            parts[path] = r;
            return r;
        }

        static Material DefaultMaterial()
        {
            if (defaultMaterial == null) defaultMaterial = AssetDatabase.GetBuiltinExtraResource<Material>("Default-Material.mat");
            return defaultMaterial;
        }

        static Material[] Materials(Renderer src, Mesh mesh)
        {
            // Models the game never showed with a material have empty slots: use Unity's default
            // material there instead of leaving them pink.
            Material[] mats = src.sharedMaterials;
            int count = Math.Max(mats.Length, mesh != null ? mesh.subMeshCount : 1);
            var result = new Material[count];
            for (int i = 0; i < count; i++) result[i] = i < mats.Length && mats[i] != null ? mats[i] : DefaultMaterial();
            return result;
        }

        static void AddRenderer(GameObject go, Node n, Transform root, Dictionary<string, Renderer> parts)
        {
            Renderer src = ModelRenderer(n.model, parts);
            if (src == null) return;
            var skinned = src as SkinnedMeshRenderer;
            if (skinned != null && n.skinned && AddSkinned(go, skinned, root, n.rendererEnabled)) return;
            Mesh mesh = skinned != null ? skinned.sharedMesh : src.GetComponent<MeshFilter>().sharedMesh;
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            mr.sharedMaterials = Materials(src, mesh);
            mr.enabled = n.rendererEnabled;
        }

        static void AddBuiltin(GameObject go, Node n)
        {
            // Cube, Sphere, Capsule...: the meshes every Unity editor has built in.
            Mesh mesh = null;
            try { mesh = Resources.GetBuiltinResource<Mesh>(n.builtin + ".fbx"); } catch (Exception) { }
            if (mesh == null) return;
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            mr.sharedMaterial = DefaultMaterial();
            mr.enabled = n.rendererEnabled;
        }

        static void AddLight(GameObject go, LightInfo info)
        {
            var light = go.AddComponent<Light>();
            light.type = (LightType)info.type;
            if (info.color != null && info.color.Length >= 3)
                light.color = new Color(info.color[0], info.color[1], info.color[2], info.color.Length > 3 ? info.color[3] : 1f);
            light.intensity = info.intensity;
            light.range = info.range;
            light.spotAngle = info.spotAngle;
            light.enabled = info.enabled;
        }

        static bool AddSkinned(GameObject go, SkinnedMeshRenderer src, Transform root, bool enabled)
        {
            // The model's bones are matched to this prefab's objects by name.
            var byName = new Dictionary<string, Transform>();
            foreach (Transform t in root.GetComponentsInChildren<Transform>(true))
                if (!byName.ContainsKey(t.name)) byName[t.name] = t;
            var bones = new Transform[src.bones.Length];
            for (int i = 0; i < bones.Length; i++)
            {
                if (src.bones[i] == null || !byName.TryGetValue(src.bones[i].name, out bones[i])) return false;
            }
            var smr = go.AddComponent<SkinnedMeshRenderer>();
            smr.sharedMesh = src.sharedMesh;
            smr.sharedMaterials = Materials(src, src.sharedMesh);
            smr.bones = bones;
            Transform rootBone;
            if (src.rootBone != null && byName.TryGetValue(src.rootBone.name, out rootBone)) smr.rootBone = rootBone;
            smr.enabled = enabled;
            return true;
        }
    }
}
#endif
'''
