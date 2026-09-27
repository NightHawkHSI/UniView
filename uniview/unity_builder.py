"""The editor script UniView puts in exported Unity projects (Assets/UniView/Editor/UniViewBuilder.cs).

Unity builds the prefabs itself from the JSON descriptions UniView writes to Assets/UniView/Build/,
using its own API - no guessing at .prefab YAML or at the IDs of meshes inside imported GLBs.
Plain C# 4 so it compiles in old Unity versions too.
"""

BUILDER_CS = r'''// Written by UniView (Game Asset Viewer). Builds the game's prefabs from the descriptions in
// Assets/UniView/Build/ the first time the project opens; run it again with UniView > Rebuild prefabs.
#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace UniView
{
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
    }

    [Serializable]
    public class Description
    {
        public int version;
        public string kind;
        public string target;
        public Node[] nodes;
    }

    [InitializeOnLoad]
    public static class Builder
    {
        const string BuildDir = "Assets/UniView/Build";

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

        [MenuItem("UniView/Rebuild prefabs")]
        public static void RebuildAll() { Build(false); }

        [MenuItem("UniView/Build missing prefabs")]
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
            string[] files = Descriptions();
            var parts = new Dictionary<string, Renderer>();
            int built = 0, failed = 0;
            try
            {
                AssetDatabase.StartAssetEditing();
                for (int i = 0; i < files.Length; i++)
                {
                    Description d = Load(files[i]);
                    if (d == null || d.nodes == null || d.nodes.Length == 0 || d.kind != "prefab") continue;
                    if (onlyMissing && File.Exists(d.target)) continue;
                    if (EditorUtility.DisplayCancelableProgressBar("UniView", "Building " + d.target, (float)i / files.Length)) break;
                    try { BuildPrefab(d, parts); built++; }
                    catch (Exception e) { failed++; Debug.LogWarning("UniView: couldn't build " + d.target + ": " + e.Message); }
                }
            }
            finally
            {
                AssetDatabase.StopAssetEditing();
                EditorUtility.ClearProgressBar();
            }
            AssetDatabase.Refresh();
            Debug.Log("UniView: built " + built + " prefab(s)" + (failed > 0 ? ", " + failed + " failed (see warnings)" : ""));
        }

        static Vector3 V(float[] a, Vector3 fallback)
        {
            return a != null && a.Length >= 3 ? new Vector3(a[0], a[1], a[2]) : fallback;
        }

        static Quaternion Q(float[] a)
        {
            return a != null && a.Length >= 4 ? new Quaternion(a[0], a[1], a[2], a[3]) : Quaternion.identity;
        }

        static GameObject BuildObjects(Description d, Dictionary<string, Renderer> parts)
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
                if (!string.IsNullOrEmpty(d.nodes[i].model)) AddRenderer(objects[i], d.nodes[i], objects[0].transform, parts);
                else if (!string.IsNullOrEmpty(d.nodes[i].builtin)) AddBuiltin(objects[i], d.nodes[i]);
            }
            for (int i = 0; i < d.nodes.Length; i++)
                if (i > 0 && !d.nodes[i].active) objects[i].SetActive(false);
            return objects[0];
        }

        static void BuildPrefab(Description d, Dictionary<string, Renderer> parts)
        {
            GameObject root = BuildObjects(d, parts);
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

        static Material defaultMaterial;

        static Material[] Materials(Renderer src, Mesh mesh)
        {
            // Models the game never showed with a material have empty slots: use Unity's default
            // material there instead of leaving them pink.
            if (defaultMaterial == null) defaultMaterial = AssetDatabase.GetBuiltinExtraResource<Material>("Default-Material.mat");
            Material[] mats = src.sharedMaterials;
            int count = Math.Max(mats.Length, mesh != null ? mesh.subMeshCount : 1);
            var result = new Material[count];
            for (int i = 0; i < count; i++) result[i] = i < mats.Length && mats[i] != null ? mats[i] : defaultMaterial;
            return result;
        }

        static void AddBuiltin(GameObject go, Node n)
        {
            // Cube, Sphere, Capsule...: the meshes every Unity editor has built in.
            Mesh mesh = null;
            try { mesh = Resources.GetBuiltinResource<Mesh>(n.builtin + ".fbx"); } catch (Exception) { }
            if (mesh == null) return;
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            if (defaultMaterial == null) defaultMaterial = AssetDatabase.GetBuiltinExtraResource<Material>("Default-Material.mat");
            mr.sharedMaterial = defaultMaterial;
            mr.enabled = n.rendererEnabled;
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
