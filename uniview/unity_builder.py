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
    public class Prop
    {
        public string p;   // SerializedProperty path, e.g. m_Center.x or m_Materials.Array.data[0]
        public string t;   // f/i/b number, s string, n object n of this prefab/scene, m mesh of model s, a asset s
        public double v;
        public string s;
        public int n;
    }

    [Serializable]
    public class Comp
    {
        public string type;
        public Prop[] props;
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
        public string[] materials;
        public bool noMaterials;
        public LightInfo light;
        public Comp[] components;
        public int layer;
        public string tag;
    }

    [Serializable]
    public class BatchRef
    {
        public string model;
        public string[] materials;
    }

    [Serializable]
    public class Manager
    {
        public string type;
        public Prop[] props;
    }

    [Serializable]
    public class Description
    {
        public int version;
        public string kind;
        public string target;
        public Node[] nodes;
        public BatchRef[] batches;
        public Manager[] managers;   // kind "settings" only
        public string[] scenes;
        public string product;
        public string company;
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

        const string SettingsMarker = "ProjectSettings/UniViewSettingsApplied.txt";

        static void BuildMissingIfAny()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            if (!File.Exists(SettingsMarker) && File.Exists(BuildDir + "/ProjectSettings.json")) { BuildMissing(); return; }
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
                if (d != null && d.kind == "settings")
                {
                    // First: tags and layers must exist before objects can use them.
                    if (!onlyMissing || !File.Exists(SettingsMarker)) ApplySettings(d);
                    continue;
                }
                if (d == null || d.nodes == null || d.nodes.Length == 0) continue;
                if (onlyMissing && File.Exists(d.target)) continue;
                todo.Add(d);
            }
            if (todo.Count == 0) { AssetDatabase.SaveAssets(); return; }
            // Prefabs first, then scenes (building a scene replaces the open one).
            todo.Sort(delegate(Description a, Description b) { return (a.kind == "scene").CompareTo(b.kind == "scene"); });
            bool anyScene = todo.Exists(delegate(Description d) { return d.kind == "scene"; });
            string openScene = EditorSceneManager.GetActiveScene().path;
            if (anyScene && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;

            var parts = new Dictionary<string, Renderer>();
            int prefabs = 0, scenes = 0, failed = 0;
            valuesSet = valuesSkipped = componentsAdded = componentsFailed = 0;
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
                      + (failed > 0 ? ", " + failed + " failed (see warnings)" : "")
                      + "; components: " + componentsAdded + " added" + (componentsFailed > 0 ? ", " + componentsFailed + " not available" : "")
                      + ", " + valuesSet + " values set, " + valuesSkipped + " not applicable");
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
                if (n.layer > 0 && n.layer < 32) go.layer = n.layer;
                if (!string.IsNullOrEmpty(n.tag) && n.tag != "Untagged")
                {
                    try { go.tag = n.tag; } catch (Exception) { }  // a tag the settings didn't define
                }
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
            AddComponents(d, objects, parts);
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
                    go.AddComponent<MeshRenderer>().sharedMaterials = Materials(src, mesh, b.materials);
                    go.isStatic = true;
                }
            }
            Directory.CreateDirectory(Path.GetDirectoryName(d.target));
            EditorSceneManager.SaveScene(scene, d.target);
        }

        // ---- the game's project settings
        static readonly Dictionary<string, string> SettingsFiles = new Dictionary<string, string>
        {
            { "TagManager", "ProjectSettings/TagManager.asset" },
            { "PhysicsManager", "ProjectSettings/DynamicsManager.asset" },
            { "Physics2DSettings", "ProjectSettings/Physics2DSettings.asset" },
            { "InputManager", "ProjectSettings/InputManager.asset" },
            { "TimeManager", "ProjectSettings/TimeManager.asset" },
            { "AudioManager", "ProjectSettings/AudioManager.asset" },
            { "QualitySettings", "ProjectSettings/QualitySettings.asset" },
            { "NavMeshProjectSettings", "ProjectSettings/NavMeshAreas.asset" },
        };

        static void ApplySettings(Description d)
        {
            int set = 0, skipped = 0;
            if (d.managers != null)
            {
                foreach (Manager m in d.managers)
                {
                    string file;
                    if (!SettingsFiles.TryGetValue(m.type, out file) || m.props == null) continue;
                    UnityEngine.Object[] objs = AssetDatabase.LoadAllAssetsAtPath(file);
                    if (objs == null || objs.Length == 0 || objs[0] == null) { Debug.LogWarning("UniView: can't open " + file); continue; }
                    int before = valuesSet, beforeSkip = valuesSkipped;
                    var so = new SerializedObject(objs[0]);
                    foreach (Prop pr in m.props) SetProp(so, pr, null, null, null);
                    so.ApplyModifiedPropertiesWithoutUndo();
                    set += valuesSet - before;
                    skipped += valuesSkipped - beforeSkip;
                }
            }
            if (d.scenes != null && d.scenes.Length > 0)
            {
                var list = new List<EditorBuildSettingsScene>();
                foreach (string scene in d.scenes) list.Add(new EditorBuildSettingsScene(scene, true));
                EditorBuildSettings.scenes = list.ToArray();
            }
            if (!string.IsNullOrEmpty(d.product)) PlayerSettings.productName = d.product;
            if (!string.IsNullOrEmpty(d.company)) PlayerSettings.companyName = d.company;
            AssetDatabase.SaveAssets();
            File.WriteAllText(SettingsMarker, "UniView applied the game's project settings (delete this file to apply them again).\n");
            Debug.Log("UniView: project settings applied (" + set + " values set, " + skipped + " not applicable)");
        }

        // ---- built-in components (colliders, rigidbodies, audio sources, cameras...), set field by field
        static Dictionary<string, Type> componentTypes;
        static int valuesSet, valuesSkipped, componentsAdded, componentsFailed;
        static readonly HashSet<string> unknownTypes = new HashSet<string>();

        static Type ComponentType(string name)
        {
            if (componentTypes == null)
            {
                componentTypes = new Dictionary<string, Type>();
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] types;
                    try { types = asm.GetTypes(); } catch (Exception) { continue; }
                    foreach (Type t in types)
                        if (typeof(Component).IsAssignableFrom(t) && t.Namespace != null && t.Namespace.StartsWith("UnityEngine")
                            && !componentTypes.ContainsKey(t.Name))
                            componentTypes[t.Name] = t;
                }
            }
            Type found;
            return componentTypes.TryGetValue(name, out found) ? found : null;
        }

        static void AddComponents(Description d, GameObject[] objects, Dictionary<string, Renderer> parts)
        {
            var made = new Component[d.nodes.Length][];
            var byNode = new Dictionary<string, Component>();  // "node|class" -> first such component
            for (int i = 0; i < d.nodes.Length; i++)
            {
                Comp[] comps = d.nodes[i].components;
                if (comps == null || comps.Length == 0) continue;
                made[i] = new Component[comps.Length];
                var reused = new HashSet<Component>();
                for (int j = 0; j < comps.Length; j++)
                {
                    Type type = ComponentType(comps[j].type);
                    if (type == null)
                    {
                        if (unknownTypes.Add(comps[j].type)) Debug.LogWarning("UniView: no component type " + comps[j].type + " in this Unity version");
                        componentsFailed++;
                        continue;
                    }
                    // A component the build already added (e.g. a Light) is filled in instead of added twice.
                    Component comp = objects[i].GetComponent(type);
                    if (comp == null || reused.Contains(comp)) comp = objects[i].AddComponent(type);
                    if (comp == null) { componentsFailed++; continue; }
                    reused.Add(comp);
                    made[i][j] = comp;
                    componentsAdded++;
                    string key = i + "|" + comps[j].type;
                    if (!byNode.ContainsKey(key)) byNode[key] = comp;
                }
            }
            for (int i = 0; i < d.nodes.Length; i++)
            {
                if (made[i] == null) continue;
                Comp[] comps = d.nodes[i].components;
                for (int j = 0; j < comps.Length; j++)
                {
                    if (made[i][j] == null || comps[j].props == null) continue;
                    var so = new SerializedObject(made[i][j]);
                    foreach (Prop pr in comps[j].props) SetProp(so, pr, objects, byNode, parts);
                    so.ApplyModifiedPropertiesWithoutUndo();
                }
            }
        }

        static void SetProp(SerializedObject so, Prop pr, GameObject[] objects, Dictionary<string, Component> byNode,
                            Dictionary<string, Renderer> parts)
        {
            SerializedProperty sp = so.FindProperty(pr.p);
            if (sp == null) { valuesSkipped++; return; }
            try
            {
                if (pr.t == "n" || pr.t == "m" || pr.t == "a")
                {
                    if (sp.propertyType != SerializedPropertyType.ObjectReference) { valuesSkipped++; return; }
                    UnityEngine.Object target = null;
                    if (pr.t == "n" && pr.n >= 0 && pr.n < objects.Length)
                    {
                        if (pr.s == "GameObject") target = objects[pr.n];
                        else if (pr.s == "Transform") target = objects[pr.n].transform;
                        else { Component c; if (byNode.TryGetValue(pr.n + "|" + pr.s, out c)) target = c; }
                    }
                    else if (pr.t == "m")
                    {
                        Renderer r = ModelRenderer(pr.s, parts);
                        var skinned = r as SkinnedMeshRenderer;
                        if (skinned != null) target = skinned.sharedMesh;
                        else if (r != null && r.GetComponent<MeshFilter>() != null) target = r.GetComponent<MeshFilter>().sharedMesh;
                    }
                    else target = AssetDatabase.LoadAssetAtPath<UnityEngine.Object>(pr.s);
                    if (target == null) { valuesSkipped++; return; }
                    sp.objectReferenceValue = target;
                    valuesSet++;
                    return;
                }
                switch (sp.propertyType)
                {
                    case SerializedPropertyType.Boolean: sp.boolValue = pr.v != 0; break;
                    case SerializedPropertyType.Float: sp.doubleValue = pr.v; break;
                    case SerializedPropertyType.Integer:
                    case SerializedPropertyType.LayerMask:
                    case SerializedPropertyType.Character: sp.longValue = (long)pr.v; break;
                    case SerializedPropertyType.ArraySize: sp.intValue = (int)pr.v; break;
                    case SerializedPropertyType.Enum: sp.intValue = (int)pr.v; break;
                    case SerializedPropertyType.String: sp.stringValue = pr.s ?? ""; break;
                    default: valuesSkipped++; return;
                }
                valuesSet++;
            }
            catch (Exception) { valuesSkipped++; }
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

        static readonly Dictionary<string, Material> matCache = new Dictionary<string, Material>();

        static Material LoadMaterial(string path)
        {
            if (string.IsNullOrEmpty(path)) return null;
            Material m;
            if (!matCache.TryGetValue(path, out m) || m == null)
            {
                m = AssetDatabase.LoadAssetAtPath<Material>(path);
                matCache[path] = m;
            }
            return m;
        }

        static Material[] Materials(Renderer src, Mesh mesh, string[] exported)
        {
            // The renderer's own materials (exported as .mat) first, else the model's, else Unity's
            // default material (models the game never showed with a material would be pink otherwise).
            Material[] mats = src.sharedMaterials;
            int count = exported != null && exported.Length > 0 ? exported.Length
                                                              : Math.Max(mats.Length, mesh != null ? mesh.subMeshCount : 1);
            var result = new Material[count];
            for (int i = 0; i < count; i++)
            {
                Material m = exported != null && i < exported.Length ? LoadMaterial(exported[i]) : null;
                if (m == null && i < mats.Length) m = mats[i];
                result[i] = m != null ? m : DefaultMaterial();
            }
            return result;
        }

        static void AddRenderer(GameObject go, Node n, Transform root, Dictionary<string, Renderer> parts)
        {
            Renderer src = ModelRenderer(n.model, parts);
            if (src == null) return;
            var skinned = src as SkinnedMeshRenderer;
            if (skinned != null && n.skinned && AddSkinned(go, skinned, root, n.rendererEnabled, n)) return;
            Mesh mesh = skinned != null ? skinned.sharedMesh : src.GetComponent<MeshFilter>().sharedMesh;
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            mr.sharedMaterials = n.noMaterials ? new Material[0] : Materials(src, mesh, n.materials);
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
            Material own = n.materials != null && n.materials.Length > 0 ? LoadMaterial(n.materials[0]) : null;
            if (n.noMaterials) mr.sharedMaterials = new Material[0];  // not drawn in the game either
            else mr.sharedMaterial = own != null ? own : DefaultMaterial();
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

        static bool AddSkinned(GameObject go, SkinnedMeshRenderer src, Transform root, bool enabled, Node n)
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
            smr.sharedMaterials = n.noMaterials ? new Material[0] : Materials(src, src.sharedMesh, n.materials);
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
