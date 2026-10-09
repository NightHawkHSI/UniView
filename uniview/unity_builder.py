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
using UnityEditor.Animations;
using UnityEditor.SceneManagement;
using UnityEngine;
#if UNITY_2021_2_OR_NEWER
using UnityEditor.U2D.Sprites;
#endif

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
    public class RectInfo
    {
        public bool present;
        public float[] anchorMin;
        public float[] anchorMax;
        public float[] anchoredPosition;
        public float[] sizeDelta;
        public float[] pivot;
    }

    [Serializable]
    public class Prop
    {
        public string p;   // SerializedProperty path, e.g. m_Center.x or m_Materials.Array.data[0]
        public string t;   // f/i/b number, s string, n object n of this prefab/scene, m mesh of model s, a asset s
        public double v;
        public string s;
        public int n;
        public string c;   // "a" into another prefab: the object's class (GameObject, Transform, a component)
        public string q;   // ...and its child path in that prefab ("" = root)
    }

    [Serializable]
    public class Comp
    {
        public string type;
        public string script;  // "Namespace.Class" for MonoBehaviour
        public Prop[] props;
        public int shared = -1;  // >= 0: the values are Description.shared[shared] (identical components share them)
    }

    [Serializable]
    public class PropList
    {
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
        public RectInfo rect;
        public Comp[] components;
        public int layer;
        public string tag;
        public float[] lightmap;  // [scene lightmap index, tiling x, y, offset x, y]
    }

    [Serializable]
    public class BatchRef
    {
        public string model;
        public string[] materials;
        public int lightmap = -1;  // the scene lightmap it's baked into (tiling/offset already in its UVs)
    }

    [Serializable]
    public class SpriteInfo
    {
        public string name;
        public float[] rect;    // x, y, width, height (pixels, from the bottom-left)
        public float[] pivot;   // 0..1
        public float[] border;  // left, bottom, right, top
    }

    [Serializable]
    public class Sheet
    {
        public string texture;
        public float ppu = 100f;
        public SpriteInfo[] sprites;
    }

    [Serializable]
    public class ClipCurve
    {
        public string path;
        public string type;   // Transform, GameObject, Animator (humanoid), a component or script class
        public string prop;   // m_LocalPosition.x, localEulerAnglesRaw.y, m_IsActive, material._Color.r ...
        public bool script;
        public float[] keys;  // t0, v0, t1, v1 ...
    }

    [Serializable]
    public class ClipObjectCurve
    {
        public string path;
        public string type;
        public string prop;
        public float[] times;
        public string[] textures;  // sprite = texture file + sprite name
        public string[] sprites;
    }

    [Serializable]
    public class ClipEvent
    {
        public float time;
        public string function;
        public string data;
        public float floatValue;
        public int intValue;
    }

    [Serializable]
    public class ClipInfo
    {
        public float length;
        public float rate = 30f;
        public bool loop;
        public bool legacy;
        public ClipCurve[] curves;
        public ClipObjectCurve[] objectCurves;
        public ClipEvent[] events;
    }

    [Serializable]
    public class CtrlParam { public string name; public string type; public float defaultValue; }

    [Serializable]
    public class CtrlCondition { public int mode; public string param; public float threshold; }

    [Serializable]
    public class CtrlTransition
    {
        public int dest = -1;  // state index; -1 = exit
        public float duration, offset, exitTime;
        public bool hasExitTime, fixedDuration = true, ordered = true, toSelf = true;
        public int interruption;
        public CtrlCondition[] conditions;
    }

    [Serializable]
    public class TreeNode
    {
        public string type, param, paramY, clip;
        public int[] children;
        public float[] thresholds, positions;
    }

    [Serializable]
    public class CtrlState
    {
        public string name, tag, speedParam, clip;
        public float speed = 1f, cycleOffset;
        public bool mirror, ikOnFeet, writeDefaults = true;
        public TreeNode[] treeNodes;
        public CtrlTransition[] transitions;
    }

    [Serializable]
    public class CtrlLayer
    {
        public string name;
        public float weight = 1f;
        public int blending, defaultState;
        public bool ik;
        public CtrlState[] states;
        public CtrlTransition[] anyTransitions;
    }

    [Serializable]
    public class CtrlInfo { public CtrlParam[] parameters; public CtrlLayer[] layers; }

    [Serializable]
    public class TLayer
    {
        public string name;
        public string target;   // the .terrainlayer asset (shared by terrains that use the same layer)
        public string diffuse;
        public string normal;
        public string mask;
        public float[] tileSize;
        public float[] tileOffset;
        public float[] specular;
        public float metallic;
        public float smoothness;
        public float normalScale = 1f;
        public float[] diffuseRemapMin;
        public float[] diffuseRemapMax;
        public float[] maskRemapMin;
        public float[] maskRemapMax;
    }

    [Serializable]
    public class TTree { public string prefab; public float bendFactor; public int navMeshLod; }

    [Serializable]
    public class TDetail
    {
        public string prefab;
        public string texture;
        public float minWidth;
        public float maxWidth;
        public float minHeight;
        public float maxHeight;
        public int noiseSeed;
        public float noiseSpread;
        public float holeEdgePadding;
        public float density;
        public float[] healthyColor;
        public float[] dryColor;
        public int renderMode;
        public bool usePrototypeMesh;
        public bool useInstancing;
        public bool useDensityScaling;
        public float alignToGround;
        public float positionJitter;
        public float targetCoverage;
    }

    [Serializable]
    public class TerrainInfo
    {
        public string name;
        public int resolution;
        public float[] size;
        public string heights;     // raw files (see uniview/unity_terrains.py), project-relative paths
        public string holes;
        public int alphamapResolution;
        public string alphamaps;
        public int baseMapResolution;
        public TLayer[] layers;
        public TTree[] trees;
        public int treeCount;
        public string treeInstances;
        public TDetail[] details;
        public int detailResolution;
        public int detailPatch;
        public bool coverage;
        public string detailMap;
        public float[] grassTint;
        public float[] grass;
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
        public PropList[] shared;  // prop lists components share (Comp.shared)
        public BatchRef[] batches;
        public Manager[] managers;   // kind "settings": project settings; kind "scene": its RenderSettings
        public string[] scenes;
        public string product;
        public string company;
        public int colorSpace;  // the game's color space + 1 (0 unknown, 1 gamma, 2 linear)
        public Sheet[] sheets;  // kind "sprites"
        public ClipInfo clip;   // kind "clip"
        public CtrlInfo controller;  // kind "controller"
        public string script;  // kind "data": the ScriptableObject class
        public Prop[] props;   // kind "data": its values
        public TerrainInfo terrain;  // kind "terrain"
        public string[] lightmaps;   // kind "scene": its baked lightmaps (EXR, "" = missing)
        public string pipeline;      // kind "pipeline": "urp" or "hdrp" (props: the pipeline asset's settings)
        public MatSwap[] materials;  // kind "pipeline": materials to put back on the game's shaders
        public string stamp;         // kind "pipeline": changes with the contents (applied again after a new export)
    }

    [Serializable]
    public class MatSwap
    {
        public string path;
        public string shader;  // the game's shader, when the project has it (URP's own shaders)
        public string[] keywords;
        public int queue = -1;
        public bool lit;       // a Standard material: else the pipeline's Lit (Standard draws pink there)
        public string[] litKeywords;
        public int litQueue = -1;
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
        const string SpritesMarker = "ProjectSettings/UniViewSpritesApplied.txt";
        const string PipelineMarker = "ProjectSettings/UniViewPipelineApplied.txt";
        const string AddressablesMarker = "ProjectSettings/UniViewAddressablesApplied.txt";
        const string ScriptsParked = "Assets/UniView/GameScripts~";   // ignored by Unity
        const string ScriptsActive = "Assets/GameScripts";
        const string RebuildMarker = "ProjectSettings/UniViewRebuildAfterScripts.txt";

        static void BuildMissingIfAny()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            if (File.Exists(RebuildMarker) && (Directory.Exists(ScriptsActive) || Directory.Exists(CodeActive)))
            {
                // The game's code was just swapped and compiles: build again so the script components get attached.
                File.Delete(RebuildMarker);
                Debug.Log("UniView: the game's scripts compiled - rebuilding prefabs and scenes with their script components");
                RebuildAll();
                return;
            }
            if (!File.Exists(SettingsMarker) && File.Exists(BuildDir + "/ProjectSettings.json")) { BuildMissing(); return; }
            if (!File.Exists(SpritesMarker) && File.Exists(BuildDir + "/Sprites.json")) { BuildMissing(); return; }
            foreach (string file in Descriptions())
            {
                Description d = Load(file);
                if (d != null && (d.kind == "pipeline" ? !PipelineApplied(d)
                                  : d.kind == "addressables" ? !Stamped(AddressablesMarker, d) : !File.Exists(d.target)))
                { BuildMissing(); return; }
            }
        }

        [MenuItem("UniView/Add the game's scripts (decompiled)")]
        public static void AddGameScripts()
        {
            if (!Directory.Exists(ScriptsParked))
            {
                Debug.LogWarning("UniView: no decompiled scripts in this project (" + ScriptsParked + ")");
                return;
            }
            if (Directory.Exists(ScriptsActive))
            {
                Debug.LogWarning("UniView: " + ScriptsActive + " already exists");
                return;
            }
            if (!Application.isBatchMode && !EditorUtility.DisplayDialog("UniView",
                "Swap the game's compiled code for its decompiled source (moved into " + ScriptsActive + ")?\n\n"
                + "Only needed to read or change the code in the editor: the compiled code already gives the prefabs "
                + "their script components. Decompiled code often needs fixes before it compiles. If there are "
                + "errors, fix them, or use UniView > Remove the game's scripts to go back. Once it compiles, the "
                + "prefabs and scenes are rebuilt with the source's script components.", "Swap in source", "Cancel")) return;
            // The source replaces the compiled assemblies it came from (same names would clash); it can use the others.
            if (Directory.Exists(CodeActive))
            {
                Directory.CreateDirectory(CodeParked);
                foreach (string dir in Directory.GetDirectories(ScriptsParked))
                    MoveFileWithMeta(CodeActive + "/" + Path.GetFileName(dir) + ".dll", CodeParked);
                SetExplicitlyReferenced(false);
            }
            Directory.Move(ScriptsParked, ScriptsActive);
            if (File.Exists(ScriptsParked + ".meta")) File.Delete(ScriptsParked + ".meta");
            File.WriteAllText(RebuildMarker, "UniView: rebuild prefabs and scenes once the game's scripts compile.\n");
            AssetDatabase.Refresh();
        }

        const string CodeActive = "Assets/UniView/GameCode";   // the game's compiled assemblies
        const string CodeParked = "Assets/UniView/GameCode~";  // the ones swapped out for source

        static void MoveFileWithMeta(string file, string folder)
        {
            if (!File.Exists(file)) return;
            string target = folder + "/" + Path.GetFileName(file);
            if (File.Exists(target)) File.Delete(target);
            File.Move(file, target);
            if (File.Exists(file + ".meta"))
            {
                if (File.Exists(target + ".meta")) File.Delete(target + ".meta");
                File.Move(file + ".meta", target + ".meta");
            }
        }

        static void SetExplicitlyReferenced(bool on)
        {
            // Decompiled source compiles into Assembly-CSharp, which only sees auto-referenced plugins.
            if (!Directory.Exists(CodeActive)) return;
            foreach (string meta in Directory.GetFiles(CodeActive, "*.dll.meta"))
            {
                string text = File.ReadAllText(meta);
                File.WriteAllText(meta, text.Replace("isExplicitlyReferenced: " + (on ? 0 : 1),
                                                     "isExplicitlyReferenced: " + (on ? 1 : 0)));
            }
        }

        [MenuItem("UniView/Add the game's scripts (decompiled)", true)]
        static bool CanAddGameScripts() { return Directory.Exists(ScriptsParked) && !Directory.Exists(ScriptsActive); }

        [MenuItem("UniView/Remove the game's scripts")]
        public static void RemoveGameScripts()
        {
            if (!Directory.Exists(ScriptsActive)) return;
            Directory.Move(ScriptsActive, ScriptsParked);
            if (File.Exists(ScriptsActive + ".meta")) File.Delete(ScriptsActive + ".meta");
            if (Directory.Exists(CodeParked))
            {
                foreach (string file in Directory.GetFiles(CodeParked, "*.dll")) MoveFileWithMeta(file, CodeActive);
                SetExplicitlyReferenced(true);
                File.WriteAllText(RebuildMarker, "UniView: rebuild prefabs and scenes with the compiled code.\n");
            }
            else if (File.Exists(RebuildMarker)) File.Delete(RebuildMarker);
            AssetDatabase.Refresh();
            Debug.Log("UniView: the game's scripts were moved back to " + ScriptsParked + " (the compiled code is used again)");
        }

        [MenuItem("UniView/Remove the game's scripts", true)]
        static bool CanRemoveGameScripts() { return Directory.Exists(ScriptsActive); }

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

        const long MaxDescriptionBytes = 300L * 1024 * 1024;  // bigger ones make JsonUtility abort the editor

        static Description Load(string file)
        {
            try
            {
                long size = new FileInfo(file).Length;
                if (size > MaxDescriptionBytes)
                {
                    Debug.LogWarning("UniView: skipped " + file + " (" + (size >> 20) + " MB is too big for Unity to read)");
                    return null;
                }
                return JsonUtility.FromJson<Description>(File.ReadAllText(file));
            }
            catch (Exception e) { Debug.LogWarning("UniView: can't read " + file + ": " + e.Message); return null; }
        }

        static void Build(bool onlyMissing)
        {
            var todo = new List<Description>();
            var data = new List<Description>();
            var clips = new List<Description>();
            var controllers = new List<Description>();
            var terrains = new List<Description>();
            string addressables = null;
            foreach (string file in Descriptions())
            {
                Description d = Load(file);
                if (d != null && d.kind == "sprites")
                {
                    // Textures become sprites / sprite sheets before anything links to their sprites.
                    if (!onlyMissing || !File.Exists(SpritesMarker)) ApplySprites(d);
                    continue;
                }
                if (d != null && d.kind == "controller")
                {
                    if (!onlyMissing || !File.Exists(d.target)) controllers.Add(d);
                    continue;
                }
                if (d != null && d.kind == "clip")
                {
                    if (!onlyMissing || !File.Exists(d.target)) clips.Add(d);
                    continue;
                }
                if (d != null && d.kind == "terrain")
                {
                    if (!onlyMissing || !File.Exists(d.target)) terrains.Add(d);
                    continue;
                }
                if (d != null && d.kind == "data")
                {
                    if (!onlyMissing || !File.Exists(d.target)) data.Add(d);
                    continue;
                }
                if (d != null && d.kind == "settings")
                {
                    // First: tags and layers must exist before objects can use them.
                    if (!onlyMissing || !File.Exists(SettingsMarker)) ApplySettings(d);
                    continue;
                }
                if (d != null && d.kind == "pipeline")
                {
                    // After the settings (their quality levels get the pipeline asset), before the scenes.
                    if (!onlyMissing || !PipelineApplied(d)) ApplyPipeline(d);
                    continue;
                }
                if (d != null && d.kind == "addressables")
                {
                    // Last: the groups point at the prefabs and scenes this build makes.
                    if (!onlyMissing || !Stamped(AddressablesMarker, d)) addressables = file;
                    continue;
                }
                if (d == null || d.nodes == null || d.nodes.Length == 0) continue;
                if (onlyMissing && File.Exists(d.target)) continue;
                todo.Add(d);
            }
            int clipsMade = BuildClips(clips);  // before prefabs: Animation components point at clips
            int controllersMade = BuildControllers(controllers);  // Animators point at controllers
            int dataMade = CreateDataAssets(data);
            int terrainsMade = 0;
            bool terrainsBuilt = false;
            if (todo.Count == 0)
            {
                terrainsMade = BuildTerrains(terrains);
                FillDataAssets(data);
                AssetDatabase.SaveAssets();
                if (data.Count > 0 || clips.Count > 0 || terrains.Count > 0)
                    Debug.Log("UniView: " + dataMade + " data asset(s), " + clipsMade + " animation clip(s), "
                              + terrainsMade + " terrain(s) built");
                if (addressables != null) ApplyAddressables(addressables);
                return;
            }
            // Prefabs first, then scenes (building a scene replaces the open one).
            todo.Sort(delegate(Description a, Description b) { return (a.kind == "scene").CompareTo(b.kind == "scene"); });
            bool anyScene = todo.Exists(delegate(Description d) { return d.kind == "scene"; });
            string openScene = EditorSceneManager.GetActiveScene().path;
            if (anyScene && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo()) return;

            var parts = new Dictionary<string, Renderer>();
            int prefabs = 0, scenes = 0, failed = 0;
            valuesSet = valuesSkipped = componentsAdded = componentsFailed = scriptsAdded = scriptsMissing = 0;
            missingScripts.Clear();
            scriptTypes = null;  // scripts may have been added or fixed since the last build
            try
            {
                for (int i = 0; i < todo.Count; i++)
                {
                    Description d = todo[i];
                    if (EditorUtility.DisplayCancelableProgressBar("UniView", "Building " + d.target, (float)i / todo.Count)) break;
                    if (d.kind == "scene" && !terrainsBuilt)
                    {
                        // After the prefabs (trees and grass are prefabs), before the scenes that use them.
                        terrainsBuilt = true;
                        terrainsMade = BuildTerrains(terrains);
                    }
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
            if (!terrainsBuilt) terrainsMade = BuildTerrains(terrains);
            FillDataAssets(data);  // after the prefabs exist, so data can point at them
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
            if (addressables != null) ApplyAddressables(addressables);
            if (anyScene)
            {
                if (!string.IsNullOrEmpty(openScene) && File.Exists(openScene)) EditorSceneManager.OpenScene(openScene);
                else EditorSceneManager.NewScene(NewSceneSetup.DefaultGameObjects, NewSceneMode.Single);
            }
            Debug.Log("UniView: built " + prefabs + " prefab(s) and " + scenes + " scene(s)"
                      + (failed > 0 ? ", " + failed + " failed (see warnings)" : "")
                      + "; components: " + componentsAdded + " added" + (componentsFailed > 0 ? ", " + componentsFailed + " not available" : "")
                      + ", " + valuesSet + " values set, " + valuesSkipped + " not applicable"
                      + "; data assets: " + dataMade + " of " + data.Count
                      + "; animation clips: " + clipsMade + " of " + clips.Count
                      + "; animator controllers: " + controllersMade + " of " + controllers.Count
                      + "; terrains: " + terrainsMade + " of " + terrains.Count
                      + "; scripts: " + scriptsAdded + " attached"
                      + (scriptsMissing > 0 ? ", " + scriptsMissing + " script classes not in the project (UniView > Add the game's scripts)" : ""));
        }

        static Vector3 V(float[] a, Vector3 fallback)
        {
            return a != null && a.Length >= 3 ? new Vector3(a[0], a[1], a[2]) : fallback;
        }

        static Quaternion Q(float[] a)
        {
            return a != null && a.Length >= 4 ? new Quaternion(a[0], a[1], a[2], a[3]) : Quaternion.identity;
        }

        static Vector2 V2(float[] a, Vector2 fallback)
        {
            return a != null && a.Length >= 2 ? new Vector2(a[0], a[1]) : fallback;
        }

        static void SetRect(RectTransform rt, RectInfo r, float z)
        {
            // Anchors, pivot and size first: anchoredPosition is measured from them.
            rt.anchorMin = V2(r.anchorMin, new Vector2(0.5f, 0.5f));
            rt.anchorMax = V2(r.anchorMax, new Vector2(0.5f, 0.5f));
            rt.pivot = V2(r.pivot, new Vector2(0.5f, 0.5f));
            rt.sizeDelta = V2(r.sizeDelta, new Vector2(100f, 100f));
            Vector2 p = V2(r.anchoredPosition, Vector2.zero);
            rt.anchoredPosition3D = new Vector3(p.x, p.y, z);
        }

        static GameObject[] BuildObjects(Description d, Dictionary<string, Renderer> parts, bool rootsCanBeInactive)
        {
            var objects = new GameObject[d.nodes.Length];
            for (int i = 0; i < d.nodes.Length; i++)
            {
                Node n = d.nodes[i];
                var go = new GameObject(string.IsNullOrEmpty(n.name) ? "GameObject" : n.name);
                bool ui = n.rect != null && n.rect.present;
                if (ui) go.AddComponent<RectTransform>();  // replaces the Transform
                if (n.parent >= 0 && n.parent < i) go.transform.SetParent(objects[n.parent].transform, false);
                go.transform.localPosition = V(n.pos, Vector3.zero);
                go.transform.localRotation = Q(n.rot);
                go.transform.localScale = V(n.scale, Vector3.one);
                if (ui) SetRect((RectTransform)go.transform, n.rect, V(n.pos, Vector3.zero).z);
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
            foreach (GameObject go in objects)
            {
                // A terrain material the export mapped to a non-terrain shader would hide the terrain layers.
                var terrain = go.GetComponent<Terrain>();
                if (terrain != null && terrain.materialTemplate != null && !terrain.materialTemplate.shader.name.Contains("Terrain"))
                    terrain.materialTemplate = null;
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
            GameObject[] objects = BuildObjects(d, parts, true);
            ApplySceneSettings(d, objects, parts);
            var lit = new List<Renderer>();
            var litIndex = new List<int>();
            var litScaleOffset = new List<Vector4>();
            for (int i = 0; i < d.nodes.Length; i++)
            {
                float[] lm = d.nodes[i].lightmap;
                var r = lm != null && lm.Length >= 5 ? objects[i].GetComponent<MeshRenderer>() : null;
                if (r == null) continue;
                lit.Add(r);
                litIndex.Add((int)lm[0]);
                litScaleOffset.Add(new Vector4(lm[1], lm[2], lm[3], lm[4]));
            }
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
                    var mr = go.AddComponent<MeshRenderer>();
                    mr.sharedMaterials = Materials(src, mesh, b.materials);
                    go.isStatic = true;
                    if (b.lightmap >= 0)
                    {
                        lit.Add(mr);
                        litIndex.Add(b.lightmap);
                        litScaleOffset.Add(new Vector4(1f, 1f, 0f, 0f));  // already in the batch's UVs
                    }
                }
            }
            AddLightmaps(d, lit, litIndex, litScaleOffset);
            Directory.CreateDirectory(Path.GetDirectoryName(d.target));
            EditorSceneManager.SaveScene(scene, d.target);
        }

        static void AddLightmaps(Description d, List<Renderer> lit, List<int> index, List<Vector4> scaleOffset)
        {
            // The game's baked lightmaps, put back by a UniView.SceneLightmaps component (UniView/Runtime).
            if (d.lightmaps == null || d.lightmaps.Length == 0 || lit.Count == 0) return;
            Type type = ScriptType("UniView.SceneLightmaps");
            if (type == null)
            {
                Debug.LogWarning("UniView: UniView.SceneLightmaps isn't compiled, so " + d.target + " has no lightmaps");
                return;
            }
            var textures = new Texture2D[d.lightmaps.Length];
            for (int i = 0; i < textures.Length; i++) textures[i] = Load<Texture2D>(d.lightmaps[i]);
            var comp = new GameObject("Lightmaps (UniView)").AddComponent(type);
            type.GetField("lightmaps").SetValue(comp, textures);
            type.GetField("renderers").SetValue(comp, lit.ToArray());
            type.GetField("indices").SetValue(comp, index.ToArray());
            type.GetField("scaleOffsets").SetValue(comp, scaleOffset.ToArray());
            type.GetMethod("Apply").Invoke(comp, null);
        }

        static void ApplySceneSettings(Description d, GameObject[] objects, Dictionary<string, Renderer> parts)
        {
            // The scene's RenderSettings (skybox, ambient light, fog, reflections, sun), field by field.
            if (d.managers == null) return;
            foreach (Manager m in d.managers)
            {
                if (m.type != "RenderSettings" || m.props == null) continue;
                var get = typeof(RenderSettings).GetMethod("GetRenderSettings",
                    System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Public);
                var target = get != null ? get.Invoke(null, null) as UnityEngine.Object : null;
                if (target == null) { Debug.LogWarning("UniView: can't reach the RenderSettings of " + d.target); continue; }
                var so = new SerializedObject(target);
                foreach (Prop pr in m.props) SetProp(so, pr, objects, null, parts);
                so.ApplyModifiedPropertiesWithoutUndo();
                DynamicGI.UpdateEnvironment();  // ambient light from the new skybox
            }
        }

        // ---- terrains
        static int BuildTerrains(List<Description> list)
        {
            int made = 0;
            var layers = new Dictionary<string, UnityEngine.Object>();
            foreach (Description d in list)
            {
                try { BuildTerrain(d, layers); made++; }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't build terrain " + d.target + ": " + e.Message); }
            }
            if (made > 0) AssetDatabase.SaveAssets();
            return made;
        }

        static Color C(float[] a, Color fallback)
        {
            return a != null && a.Length >= 4 ? new Color(a[0], a[1], a[2], a[3]) : fallback;
        }

        static Vector4 V4(float[] a, Vector4 fallback)
        {
            return a != null && a.Length >= 4 ? new Vector4(a[0], a[1], a[2], a[3]) : fallback;
        }

        static T Load<T>(string path) where T : UnityEngine.Object
        {
            return string.IsNullOrEmpty(path) ? null : AssetDatabase.LoadAssetAtPath<T>(path);
        }

        static void SetMember(object target, string name, object value)
        {
            // Values only some Unity versions have, set by name so this script compiles in all of them.
            var prop = target.GetType().GetProperty(name);
            try
            {
                if (prop != null && prop.CanWrite) prop.SetValue(target, Convert.ChangeType(value, prop.PropertyType), null);
            }
            catch (Exception) { }
        }

        static void BuildTerrain(Description d, Dictionary<string, UnityEngine.Object> layerAssets)
        {
            TerrainInfo t = d.terrain;
            if (t == null || t.resolution < 2 || !File.Exists(t.heights)) throw new Exception("no heightmap");
            Directory.CreateDirectory(Path.GetDirectoryName(d.target));
            int res = t.resolution;
            var td = new TerrainData();
            td.heightmapResolution = res;
            td.size = V(t.size, new Vector3(500f, 600f, 500f));  // after the resolution, which resets it
            AssetDatabase.CreateAsset(td, d.target);

            byte[] raw = File.ReadAllBytes(t.heights);
            var heights = new float[res, res];
            for (int z = 0, i = 0; z < res; z++)
                for (int x = 0; x < res; x++, i += 2)
                    heights[z, x] = Math.Min(1f, BitConverter.ToUInt16(raw, i) / 32766f);
            td.SetHeights(0, 0, heights);
#if UNITY_2019_3_OR_NEWER
            if (!string.IsNullOrEmpty(t.holes) && File.Exists(t.holes))
            {
                raw = File.ReadAllBytes(t.holes);
                var solid = new bool[res - 1, res - 1];
                for (int z = 0, i = 0; z < res - 1; z++)
                    for (int x = 0; x < res - 1; x++, i++)
                        solid[z, x] = raw[i] != 0;
                td.SetHoles(0, 0, solid);
            }
#endif
            int count = t.layers != null ? t.layers.Length : 0;
            if (count > 0)
            {
                if (t.alphamapResolution >= 16) td.alphamapResolution = t.alphamapResolution;
                if (t.baseMapResolution >= 16) td.baseMapResolution = t.baseMapResolution;
#if UNITY_2018_3_OR_NEWER
                var terrainLayers = new TerrainLayer[count];
                for (int l = 0; l < count; l++)
                {
                    TLayer src = t.layers[l];
                    UnityEngine.Object made;
                    if (!layerAssets.TryGetValue(src.target, out made))
                    {
                        var layer = new TerrainLayer();
                        layer.name = src.name;
                        layer.diffuseTexture = Load<Texture2D>(src.diffuse);
                        layer.normalMapTexture = Load<Texture2D>(src.normal);
                        layer.maskMapTexture = Load<Texture2D>(src.mask);
                        layer.tileSize = V2(src.tileSize, new Vector2(15f, 15f));
                        layer.tileOffset = V2(src.tileOffset, Vector2.zero);
                        layer.specular = C(src.specular, Color.black);
                        layer.metallic = src.metallic;
                        layer.smoothness = src.smoothness;
                        layer.normalScale = src.normalScale;
                        layer.diffuseRemapMin = V4(src.diffuseRemapMin, Vector4.zero);
                        layer.diffuseRemapMax = V4(src.diffuseRemapMax, Vector4.one);
                        layer.maskMapRemapMin = V4(src.maskRemapMin, Vector4.zero);
                        layer.maskMapRemapMax = V4(src.maskRemapMax, Vector4.one);
                        Directory.CreateDirectory(Path.GetDirectoryName(src.target));
                        AssetDatabase.CreateAsset(layer, src.target);
                        made = layer;
                        layerAssets[src.target] = made;
                    }
                    terrainLayers[l] = made as TerrainLayer;
                }
                td.terrainLayers = terrainLayers;
#else
                var splats = new SplatPrototype[count];
                for (int l = 0; l < count; l++)
                {
                    TLayer src = t.layers[l];
                    var sp = new SplatPrototype();
                    sp.texture = Load<Texture2D>(src.diffuse);
                    sp.normalMap = Load<Texture2D>(src.normal);
                    sp.tileSize = V2(src.tileSize, new Vector2(15f, 15f));
                    sp.tileOffset = V2(src.tileOffset, Vector2.zero);
                    SetMember(sp, "specular", C(src.specular, Color.black));
                    SetMember(sp, "metallic", src.metallic);
                    SetMember(sp, "smoothness", src.smoothness);
                    splats[l] = sp;
                }
                td.splatPrototypes = splats;
#endif
                if (!string.IsNullOrEmpty(t.alphamaps) && File.Exists(t.alphamaps))
                {
                    raw = File.ReadAllBytes(t.alphamaps);
                    int ares = t.alphamapResolution;
                    var maps = new float[ares, ares, count];
                    for (int l = 0, i = 0; l < count; l++)
                        for (int z = 0; z < ares; z++)
                            for (int x = 0; x < ares; x++, i++)
                                maps[z, x, l] = raw[i] / 255f;
                    if (td.alphamapResolution != ares) Debug.LogWarning("UniView: " + d.target + ": Unity changed the splat map size");
                    else td.SetAlphamaps(0, 0, maps);
                }
            }

            // Trees: prototypes whose prefab is not in the project are left out.
            var treeIndex = new List<int>();
            var protos = new List<TreePrototype>();
            foreach (TTree src in t.trees ?? new TTree[0])
            {
                var prefab = Load<GameObject>(src.prefab);
                treeIndex.Add(prefab != null ? protos.Count : -1);
                if (prefab == null) continue;
                var p = new TreePrototype();
                p.prefab = prefab;
                p.bendFactor = src.bendFactor;
                SetMember(p, "navMeshLod", src.navMeshLod);
                protos.Add(p);
            }
            td.treePrototypes = protos.ToArray();
            if (t.treeCount > 0 && File.Exists(t.treeInstances) && protos.Count > 0)
            {
                raw = File.ReadAllBytes(t.treeInstances);
                var trees = new List<TreeInstance>();
                for (int i = 0; i + 36 <= raw.Length; i += 36)
                {
                    int index = BitConverter.ToInt32(raw, i + 32);
                    if (index < 0 || index >= treeIndex.Count || treeIndex[index] < 0) continue;
                    var tree = new TreeInstance();
                    tree.position = new Vector3(BitConverter.ToSingle(raw, i), BitConverter.ToSingle(raw, i + 4),
                                                BitConverter.ToSingle(raw, i + 8));
                    tree.widthScale = BitConverter.ToSingle(raw, i + 12);
                    tree.heightScale = BitConverter.ToSingle(raw, i + 16);
                    tree.rotation = BitConverter.ToSingle(raw, i + 20);
                    tree.color = new Color32(raw[i + 24], raw[i + 25], raw[i + 26], raw[i + 27]);
                    tree.lightmapColor = new Color32(raw[i + 28], raw[i + 29], raw[i + 30], raw[i + 31]);
                    tree.prototypeIndex = treeIndex[index];
                    trees.Add(tree);
                }
                td.treeInstances = trees.ToArray();
            }

            // Grass and detail meshes.
            var detailIndex = new List<int>();
            var details = new List<DetailPrototype>();
            foreach (TDetail src in t.details ?? new TDetail[0])
            {
                var prefab = src.usePrototypeMesh ? Load<GameObject>(src.prefab) : null;
                var texture = src.usePrototypeMesh ? null : Load<Texture2D>(src.texture);
                detailIndex.Add(prefab != null || texture != null ? details.Count : -1);
                if (prefab == null && texture == null) continue;
                var p = new DetailPrototype();
                p.usePrototypeMesh = prefab != null;
                p.prototype = prefab;
                p.prototypeTexture = texture;
                p.minWidth = src.minWidth;
                p.maxWidth = src.maxWidth;
                p.minHeight = src.minHeight;
                p.maxHeight = src.maxHeight;
                p.noiseSpread = src.noiseSpread;
                p.healthyColor = C(src.healthyColor, Color.white);
                p.dryColor = C(src.dryColor, Color.white);
                p.renderMode = (DetailRenderMode)src.renderMode;
                SetMember(p, "noiseSeed", src.noiseSeed);
                SetMember(p, "holeEdgePadding", src.holeEdgePadding);
                SetMember(p, "density", src.density);
                SetMember(p, "useInstancing", src.useInstancing);
                SetMember(p, "useDensityScaling", src.useDensityScaling);
                SetMember(p, "alignToGround", src.alignToGround);
                SetMember(p, "positionJitter", src.positionJitter);
                SetMember(p, "targetCoverage", src.targetCoverage);
                details.Add(p);
            }
            if (details.Count > 0 && t.detailResolution > 0 && File.Exists(t.detailMap))
            {
                td.SetDetailResolution(t.detailResolution, Math.Max(8, t.detailPatch));
                var scatter = typeof(TerrainData).GetMethod("SetDetailScatterMode");
                if (scatter != null && t.coverage)  // Unity 2022.2+: the values are coverage, not counts
                    scatter.Invoke(td, new object[] { Enum.ToObject(scatter.GetParameters()[0].ParameterType, 1) });
                td.detailPrototypes = details.ToArray();
                raw = File.ReadAllBytes(t.detailMap);
                int dres = t.detailResolution;
                for (int l = 0; l < detailIndex.Count; l++)
                {
                    if (detailIndex[l] < 0 || (l + 1) * dres * dres > raw.Length) continue;
                    var map = new int[dres, dres];
                    for (int z = 0, i = l * dres * dres; z < dres; z++)
                        for (int x = 0; x < dres; x++, i++)
                            map[z, x] = raw[i];
                    td.SetDetailLayer(0, 0, detailIndex[l], map);
                }
            }
            td.wavingGrassTint = C(t.grassTint, td.wavingGrassTint);
            if (t.grass != null && t.grass.Length >= 3)
            {
                td.wavingGrassStrength = t.grass[0];
                td.wavingGrassAmount = t.grass[1];
                td.wavingGrassSpeed = t.grass[2];
            }
            EditorUtility.SetDirty(td);
        }

        // ---- animation clips
        static Type CurveType(ClipCurve c)
        {
            if (c.type == "Transform") return typeof(Transform);
            if (c.type == "GameObject") return typeof(GameObject);
            if (c.type == "Animator") return typeof(Animator);
            return c.script ? ScriptType(c.type) : ComponentType(c.type);
        }

        static int BuildClips(List<Description> clips)
        {
            int made = 0, curvesSet = 0, curvesSkipped = 0;
            foreach (Description d in clips)
            {
                ClipInfo info = d.clip;
                if (info == null) continue;
                try
                {
                    var clip = new AnimationClip { frameRate = info.rate > 0 ? info.rate : 30f, legacy = info.legacy };
                    if (info.curves != null)
                    {
                        foreach (ClipCurve c in info.curves)
                        {
                            Type type = CurveType(c);
                            if (type == null || c.keys == null || c.keys.Length < 2) { curvesSkipped++; continue; }
                            var keys = new Keyframe[c.keys.Length / 2];
                            for (int k = 0; k < keys.Length; k++) keys[k] = new Keyframe(c.keys[2 * k], c.keys[2 * k + 1]);
                            var curve = new AnimationCurve(keys);  // keys at the same time are merged
                            for (int k = 0; k < curve.length; k++)
                            {
                                AnimationUtility.SetKeyLeftTangentMode(curve, k, AnimationUtility.TangentMode.ClampedAuto);
                                AnimationUtility.SetKeyRightTangentMode(curve, k, AnimationUtility.TangentMode.ClampedAuto);
                            }
                            AnimationUtility.SetEditorCurve(clip, EditorCurveBinding.FloatCurve(c.path ?? "", type, c.prop), curve);
                            curvesSet++;
                        }
                    }
                    if (info.objectCurves != null)
                    {
                        foreach (ClipObjectCurve c in info.objectCurves)
                        {
                            Type type = ComponentType(c.type);
                            if (type == null || c.times == null) continue;
                            var frames = new List<ObjectReferenceKeyframe>();
                            for (int k = 0; k < c.times.Length; k++)
                                frames.Add(new ObjectReferenceKeyframe { time = c.times[k], value = SpriteAt(c.textures[k], c.sprites[k]) });
                            AnimationUtility.SetObjectReferenceCurve(clip, EditorCurveBinding.PPtrCurve(c.path ?? "", type, c.prop), frames.ToArray());
                        }
                    }
                    if (info.events != null && info.events.Length > 0)
                    {
                        var events = new List<AnimationEvent>();
                        foreach (ClipEvent e in info.events)
                            events.Add(new AnimationEvent { time = e.time, functionName = e.function, stringParameter = e.data,
                                                            floatParameter = e.floatValue, intParameter = e.intValue });
                        AnimationUtility.SetAnimationEvents(clip, events.ToArray());
                    }
                    if (!info.legacy)
                    {
                        AnimationClipSettings settings = AnimationUtility.GetAnimationClipSettings(clip);
                        settings.loopTime = info.loop;
                        AnimationUtility.SetAnimationClipSettings(clip, settings);
                    }
                    else if (info.loop) clip.wrapMode = WrapMode.Loop;
                    Directory.CreateDirectory(Path.GetDirectoryName(d.target));
                    if (File.Exists(d.target)) AssetDatabase.DeleteAsset(d.target);
                    AssetDatabase.CreateAsset(clip, d.target);
                    made++;
                }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't build " + d.target + ": " + e.Message); }
            }
            if (clips.Count > 0) Debug.Log("UniView: animation curves: " + curvesSet + " set, " + curvesSkipped + " skipped (their component or script isn't in the project)");
            return made;
        }

        // ---- animator controllers
        static int BuildControllers(List<Description> list)
        {
            int made = 0;
            foreach (Description d in list)
            {
                CtrlInfo info = d.controller;
                if (info == null) continue;
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(d.target));
                    if (File.Exists(d.target)) AssetDatabase.DeleteAsset(d.target);
                    AnimatorController ctrl = AnimatorController.CreateAnimatorControllerAtPath(d.target);
                    if (info.parameters != null)
                    {
                        foreach (CtrlParam p in info.parameters)
                        {
                            var type = p.type == "Int" ? AnimatorControllerParameterType.Int
                                     : p.type == "Bool" ? AnimatorControllerParameterType.Bool
                                     : p.type == "Trigger" ? AnimatorControllerParameterType.Trigger
                                     : AnimatorControllerParameterType.Float;
                            ctrl.AddParameter(p.name, type);
                        }
                        AnimatorControllerParameter[] ps = ctrl.parameters;
                        for (int i = 0; i < ps.Length && i < info.parameters.Length; i++)
                        {
                            ps[i].defaultFloat = info.parameters[i].defaultValue;
                            ps[i].defaultInt = (int)info.parameters[i].defaultValue;
                            ps[i].defaultBool = info.parameters[i].defaultValue != 0;
                        }
                        ctrl.parameters = ps;
                    }
                    if (info.layers != null)
                    {
                        for (int li = 0; li < info.layers.Length; li++)
                        {
                            CtrlLayer L = info.layers[li];
                            if (li > 0) ctrl.AddLayer(L.name);
                            AnimatorControllerLayer[] layers = ctrl.layers;
                            AnimatorStateMachine sm = layers[li].stateMachine;
                            var states = new AnimatorState[L.states != null ? L.states.Length : 0];
                            for (int i = 0; i < states.Length; i++)
                            {
                                CtrlState s = L.states[i];
                                AnimatorState st = sm.AddState(string.IsNullOrEmpty(s.name) ? "State" : s.name,
                                                               new Vector3(300 + (i % 4) * 240, 80 + (i / 4) * 80, 0));
                                st.tag = s.tag ?? "";
                                st.speed = s.speed;
                                st.cycleOffset = s.cycleOffset;
                                st.mirror = s.mirror;
                                st.iKOnFeet = s.ikOnFeet;
                                st.writeDefaultValues = s.writeDefaults;
                                if (!string.IsNullOrEmpty(s.speedParam)) { st.speedParameterActive = true; st.speedParameter = s.speedParam; }
                                if (s.treeNodes != null && s.treeNodes.Length > 0) st.motion = BuildTree(ctrl, s.treeNodes, 0, s.name);
                                else if (!string.IsNullOrEmpty(s.clip)) st.motion = AssetDatabase.LoadAssetAtPath<AnimationClip>(s.clip);
                                states[i] = st;
                            }
                            if (states.Length > 0 && L.defaultState >= 0 && L.defaultState < states.Length) sm.defaultState = states[L.defaultState];
                            for (int i = 0; i < states.Length; i++)
                                if (L.states[i].transitions != null)
                                    foreach (CtrlTransition t in L.states[i].transitions)
                                        SetTransition(t.dest >= 0 && t.dest < states.Length ? states[i].AddTransition(states[t.dest])
                                                                                            : states[i].AddExitTransition(), t);
                            if (L.anyTransitions != null)
                                foreach (CtrlTransition t in L.anyTransitions)
                                    if (t.dest >= 0 && t.dest < states.Length) SetTransition(sm.AddAnyStateTransition(states[t.dest]), t);
                            layers = ctrl.layers;
                            layers[li].name = L.name;
                            layers[li].defaultWeight = li == 0 ? 1f : L.weight;  // the base layer always counts fully
                            layers[li].blendingMode = (AnimatorLayerBlendingMode)L.blending;
                            layers[li].iKPass = L.ik;
                            ctrl.layers = layers;
                        }
                    }
                    EditorUtility.SetDirty(ctrl);
                    made++;
                }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't build " + d.target + ": " + e.Message); }
            }
            if (list.Count > 0) AssetDatabase.SaveAssets();
            return made;
        }

        static void SetTransition(AnimatorStateTransition tr, CtrlTransition t)
        {
            tr.duration = t.duration;
            tr.offset = t.offset;
            tr.exitTime = t.exitTime;
            tr.hasExitTime = t.hasExitTime;
            tr.hasFixedDuration = t.fixedDuration;
            tr.interruptionSource = (TransitionInterruptionSource)t.interruption;
            tr.orderedInterruption = t.ordered;
            tr.canTransitionToSelf = t.toSelf;
            if (t.conditions != null)
                foreach (CtrlCondition c in t.conditions) tr.AddCondition((AnimatorConditionMode)c.mode, c.threshold, c.param);
        }

        static Motion BuildTree(AnimatorController ctrl, TreeNode[] nodes, int index, string name)
        {
            TreeNode n = nodes[index];
            if (n.children == null || n.children.Length == 0)
                return string.IsNullOrEmpty(n.clip) ? null : AssetDatabase.LoadAssetAtPath<AnimationClip>(n.clip);
            var tree = new BlendTree { name = name, hideFlags = HideFlags.HideInHierarchy, useAutomaticThresholds = false };
            try { tree.blendType = (BlendTreeType)Enum.Parse(typeof(BlendTreeType), n.type); } catch (Exception) { }
            if (!string.IsNullOrEmpty(n.param)) tree.blendParameter = n.param;
            if (!string.IsNullOrEmpty(n.paramY)) tree.blendParameterY = n.paramY;
            AssetDatabase.AddObjectToAsset(tree, ctrl);
            for (int k = 0; k < n.children.Length; k++)
            {
                int c = n.children[k];
                if (c < 0 || c >= nodes.Length) continue;
                Motion m = BuildTree(ctrl, nodes, c, name + "." + k);
                if (tree.blendType == BlendTreeType.Simple1D)
                    tree.AddChild(m, n.thresholds != null && k < n.thresholds.Length ? n.thresholds[k] : k);
                else if (tree.blendType == BlendTreeType.Direct)
                    tree.AddChild(m);
                else
                    tree.AddChild(m, n.positions != null && 2 * k + 1 < n.positions.Length
                                     ? new Vector2(n.positions[2 * k], n.positions[2 * k + 1]) : Vector2.zero);
            }
            return tree;
        }

        // ---- data assets (ScriptableObjects: item stats, loot tables...)
        static int CreateDataAssets(List<Description> data)
        {
            int made = 0;
            foreach (Description d in data)
            {
                Type type = string.IsNullOrEmpty(d.script) ? null : ScriptType(d.script);
                if (type == null || !typeof(ScriptableObject).IsAssignableFrom(type))
                {
                    if (!string.IsNullOrEmpty(d.script) && missingScripts.Add(d.script)) scriptsMissing++;
                    continue;
                }
                if (AssetDatabase.LoadAssetAtPath<ScriptableObject>(d.target) != null) { made++; continue; }
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(d.target));
                    ScriptableObject obj = ScriptableObject.CreateInstance(type);
                    obj.name = Path.GetFileNameWithoutExtension(d.target);
                    AssetDatabase.CreateAsset(obj, d.target);
                    made++;
                }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't create " + d.target + ": " + e.Message); }
            }
            return made;
        }

        static void FillDataAssets(List<Description> data)
        {
            foreach (Description d in data)
            {
                var obj = AssetDatabase.LoadAssetAtPath<ScriptableObject>(d.target);
                if (obj == null || d.props == null) continue;
                var so = new SerializedObject(obj);
                foreach (Prop pr in d.props) SetProp(so, pr, null, null, new Dictionary<string, Renderer>());
                so.ApplyModifiedPropertiesWithoutUndo();
                EditorUtility.SetDirty(obj);
            }
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

        // ---- render pipeline (URP / HDRP): a pipeline asset with the game's settings, and the materials back on its shaders.
        // All through reflection: the builder compiles without the URP package (and in Unity versions without SRP).
        const System.Reflection.BindingFlags AnyStatic = System.Reflection.BindingFlags.Static
            | System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.NonPublic;

        static bool PipelineApplied(Description d)
        {
            return Stamped(PipelineMarker, d);
        }

        static bool Stamped(string marker, Description d)
        {
            return File.Exists(marker) && File.ReadAllText(marker).Contains(d.stamp ?? "");
        }

        static void ApplyAddressables(string file)
        {
            Description d = Load(file);
            if (d == null) return;
            File.WriteAllText(AddressablesMarker, "UniView added the game's Addressables entries (delete this file to add them again).\n" + d.stamp + "\n");
            Type builder = AnyType("UniView.AddressablesBuilder");
            var apply = builder == null ? null : builder.GetMethod("Apply", AnyStatic);
            if (apply == null)
            {
                Debug.LogWarning("UniView: the game uses Addressables, but the Addressables package isn't in the project - its groups weren't made");
                return;
            }
            try { Debug.Log("UniView: Addressables: " + apply.Invoke(null, new object[] { File.ReadAllText(file) })); }
            catch (Exception e) { Debug.LogWarning("UniView: couldn't make the Addressables groups: " + (e.InnerException ?? e).Message); }
        }

        static object[] Arguments(System.Reflection.MethodInfo m, object first)
        {
            // first: the first parameter; enums: the URP renderer; the rest: their defaults.
            var ps = m.GetParameters();
            var args = new object[ps.Length];
            for (int i = 0; i < ps.Length; i++)
            {
                Type t = ps[i].ParameterType;
                if (i == 0 && first != null) args[i] = first;
                else if (t.IsEnum)
                {
                    string[] names = Enum.GetNames(t);
                    string pick = Array.IndexOf(names, "UniversalRenderer") >= 0 ? "UniversalRenderer"
                        : Array.IndexOf(names, "ForwardRenderer") >= 0 ? "ForwardRenderer" : names[0];
                    args[i] = Enum.Parse(t, pick);
                }
                else if (ps[i].IsOptional) args[i] = ps[i].DefaultValue;
                else args[i] = t.IsValueType ? Activator.CreateInstance(t) : null;
            }
            return args;
        }

        static void EnsureFolder(string assetPath)
        {
            string folder = Path.GetDirectoryName(assetPath).Replace('\\', '/');
            if (!AssetDatabase.IsValidFolder(folder))
                AssetDatabase.CreateFolder(Path.GetDirectoryName(folder).Replace('\\', '/'), Path.GetFileName(folder));
        }

        static ScriptableObject CreateUrpAsset(Type assetType, string path)
        {
            EnsureFolder(path);
            object data = null;
            try
            {
                // URP's own "Create > Rendering > URP Asset (with Universal Renderer)": the renderer asset, then the pipeline asset.
                var makeData = assetType.GetMethod("CreateRendererAsset", AnyStatic);
                if (makeData != null) data = makeData.Invoke(null, Arguments(makeData, path));
            }
            catch (Exception e) { Debug.LogWarning("UniView: couldn't make the URP renderer asset: " + (e.InnerException ?? e).Message); }
            System.Reflection.MethodInfo create = null;
            foreach (var m in assetType.GetMethods(AnyStatic))
                if (m.Name == "Create" && m.GetParameters().Length == 1) create = m;
            if (create == null) { Debug.LogWarning("UniView: this URP version has no UniversalRenderPipelineAsset.Create"); return null; }
            ScriptableObject asset;
            try { asset = create.Invoke(null, new object[] { data }) as ScriptableObject; }
            catch (Exception e) { Debug.LogWarning("UniView: couldn't make the URP asset: " + (e.InnerException ?? e).Message); return null; }
            if (asset == null) return null;
            AssetDatabase.CreateAsset(asset, path);
            if (data == null)
            {
                // No renderer asset yet: URP's public way to make the default one.
                var load = assetType.GetMethod("LoadBuiltinRendererData");
                try { if (load != null) load.Invoke(asset, Arguments(load, null)); }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't make the URP renderer asset: " + (e.InnerException ?? e).Message); }
            }
            return asset;
        }

        static Type AnyType(string fullName)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = asm.GetType(fullName, false);
                if (t != null) return t;
            }
            return null;
        }

        static ScriptableObject CreateHdrpAsset(Type assetType, string path)
        {
            // HDRP's "Create > Rendering > HDRP Asset": a plain new asset; HDRP makes its global settings asset
            // itself once the pipeline is in use.
            EnsureFolder(path);
            ScriptableObject asset;
            try { asset = ScriptableObject.CreateInstance(assetType); }
            catch (Exception e) { Debug.LogWarning("UniView: couldn't make the HDRP asset: " + e.Message); return null; }
            if (asset == null) return null;
            asset.name = Path.GetFileNameWithoutExtension(path);
            AssetDatabase.CreateAsset(asset, path);
            return asset;
        }

        static bool SetStatic(Type type, object value, params string[] names)
        {
            foreach (string name in names)
            {
                var p = type.GetProperty(name, AnyStatic);
                if (p == null || !p.CanWrite) continue;
                try { p.SetValue(null, value, null); return true; }
                catch (Exception e) { Debug.LogWarning("UniView: couldn't set " + type.Name + "." + name + ": " + (e.InnerException ?? e).Message); }
            }
            return false;
        }

        static void ApplyPipeline(Description d)
        {
            File.WriteAllText(PipelineMarker, "UniView applied the game's render pipeline (delete this file to apply it again).\n" + d.stamp + "\n");
            bool hdrp = d.pipeline == "hdrp";
            if (d.pipeline != "urp" && !hdrp) return;
            Type assetType = hdrp
                ? ScriptType("UnityEngine.Rendering.HighDefinition.HDRenderPipelineAsset")
                  ?? ScriptType("UnityEngine.Experimental.Rendering.HDPipeline.HDRenderPipelineAsset")
                : ScriptType("UnityEngine.Rendering.Universal.UniversalRenderPipelineAsset");
            if (assetType == null)
            {
                Debug.LogWarning("UniView: the game uses " + (hdrp ? "the High Definition Render Pipeline (HDRP)" : "the Universal Render Pipeline (URP)")
                                 + ", but its package isn't in the project");
                return;
            }
            var asset = AssetDatabase.LoadAssetAtPath(d.target, assetType) as ScriptableObject;
            if (asset == null) asset = hdrp ? CreateHdrpAsset(assetType, d.target) : CreateUrpAsset(assetType, d.target);
            if (asset == null) return;
            int before = valuesSet, beforeSkip = valuesSkipped;
            if (d.props != null && d.props.Length > 0)
            {
                var so = new SerializedObject(asset);
                foreach (Prop pr in d.props) SetProp(so, pr, null, null, null);
                so.ApplyModifiedPropertiesWithoutUndo();
            }
            // Graphics settings ("defaultRenderPipeline" since 2021.2) and every quality level.
            SetStatic(typeof(UnityEngine.Rendering.GraphicsSettings), asset, "defaultRenderPipeline", "renderPipelineAsset");
            if (typeof(QualitySettings).GetProperty("renderPipeline", AnyStatic) != null)
            {
                int current = QualitySettings.GetQualityLevel();
                for (int i = 0; i < QualitySettings.names.Length; i++)
                {
                    QualitySettings.SetQualityLevel(i, false);
                    SetStatic(typeof(QualitySettings), asset, "renderPipeline");
                }
                QualitySettings.SetQualityLevel(current, false);
            }
            int game = 0, lit = 0, kept = 0;
            Shader litShader = Shader.Find(hdrp ? "HDRP/Lit" : "Universal Render Pipeline/Lit");
            // HDRP materials need its own keyword / pass / stencil setup (what its material inspector does).
            System.Reflection.MethodInfo resetKeywords = null;
            if (hdrp)
            {
                foreach (string name in new[] { "UnityEditor.Rendering.HighDefinition.HDShaderUtils",
                                                "UnityEditor.Rendering.HighDefinition.HDEditorUtils" })
                {
                    Type t = AnyType(name);
                    var method = t == null ? null : t.GetMethod("ResetMaterialKeywords", AnyStatic, null, new[] { typeof(Material) }, null);
                    if (method != null) { resetKeywords = method; break; }
                }
            }
            foreach (MatSwap m in d.materials ?? new MatSwap[0])
            {
                var mat = AssetDatabase.LoadAssetAtPath<Material>(m.path);
                if (mat == null) continue;
                Shader s = string.IsNullOrEmpty(m.shader) ? null : Shader.Find(m.shader);
                string[] keywords = m.keywords;
                int queue = m.queue;
                if (s != null) game++;
                else if (m.lit && litShader != null && mat.shader != null && mat.shader.name.StartsWith("Standard"))
                {
                    s = litShader;
                    keywords = m.litKeywords;
                    queue = m.litQueue;
                    lit++;
                }
                else { kept++; continue; }
                if (mat.shader == s) continue;
                mat.shader = s;  // the .mat keeps every property, so the game's values come back with the shader
                mat.shaderKeywords = keywords ?? new string[0];
                mat.renderQueue = queue;
                if (resetKeywords != null)
                {
                    try { resetKeywords.Invoke(null, new object[] { mat }); }
                    catch (Exception e) { Debug.LogWarning("UniView: HDRP setup of " + m.path + ": " + (e.InnerException ?? e).Message); }
                }
                EditorUtility.SetDirty(mat);
            }
            AssetDatabase.SaveAssets();
            Debug.Log("UniView: render pipeline " + d.target + " (" + (valuesSet - before) + " of the game's settings, "
                      + (valuesSkipped - beforeSkip) + " not applicable); materials: " + game + " on the game's " + d.pipeline.ToUpper()
                      + " shaders, " + lit + " on " + (litShader != null ? litShader.name : "Lit") + ", " + kept + " kept"
                      + (hdrp ? " (HDRP draws no RenderSettings skybox or fog: add a Volume for those)" : ""));
        }

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
            if (d.colorSpace == 1 || d.colorSpace == 2)
                PlayerSettings.colorSpace = d.colorSpace == 2 ? ColorSpace.Linear : ColorSpace.Gamma;
            AssetDatabase.SaveAssets();
            File.WriteAllText(SettingsMarker, "UniView applied the game's project settings (delete this file to apply them again).\n");
            Debug.Log("UniView: project settings applied (" + set + " values set, " + skipped + " not applicable)");
        }

        // ---- built-in components (colliders, rigidbodies, audio sources, cameras...), set field by field
        static Dictionary<string, Type> componentTypes;
        static int valuesSet, valuesSkipped, componentsAdded, componentsFailed;
        static readonly HashSet<string> unknownTypes = new HashSet<string>();

        static Dictionary<string, Type> scriptTypes;
        static int scriptsAdded, scriptsMissing;
        static readonly HashSet<string> missingScripts = new HashSet<string>();

        static Type[] LoadableTypes(System.Reflection.Assembly asm)
        {
            // A game assembly built for an old Unity has a few types that use removed APIs; keep the rest.
            try { return asm.GetTypes(); }
            catch (System.Reflection.ReflectionTypeLoadException e) { return Array.FindAll(e.Types, t => t != null); }
            catch (Exception) { return new Type[0]; }
        }

        static Type ScriptType(string fullName)
        {
            if (scriptTypes == null)
            {
                scriptTypes = new Dictionary<string, Type>();
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    foreach (Type t in LoadableTypes(asm))
                        if ((typeof(MonoBehaviour).IsAssignableFrom(t) || typeof(ScriptableObject).IsAssignableFrom(t))
                            && !t.IsAbstract && !t.IsGenericTypeDefinition && !scriptTypes.ContainsKey(t.FullName))
                            scriptTypes[t.FullName] = t;
                }
            }
            Type found;
            return scriptTypes.TryGetValue(fullName, out found) ? found : null;
        }

        static Type ComponentType(string name)
        {
            if (componentTypes == null)
            {
                componentTypes = new Dictionary<string, Type>();
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    foreach (Type t in LoadableTypes(asm))
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
                    bool isScript = comps[j].type == "MonoBehaviour";
                    Type type = isScript ? ScriptType(comps[j].script) : ComponentType(comps[j].type);
                    if (type == null && isScript)
                    {
                        // The game's scripts aren't in the project (or don't compile yet): UniView > Add the game's scripts.
                        if (missingScripts.Add(comps[j].script)) scriptsMissing++;
                        continue;
                    }
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
                    if (isScript) scriptsAdded++;
                    string key = i + "|" + (isScript ? comps[j].script : comps[j].type);
                    if (!byNode.ContainsKey(key)) byNode[key] = comp;
                }
            }
            for (int i = 0; i < d.nodes.Length; i++)
            {
                if (made[i] == null) continue;
                Comp[] comps = d.nodes[i].components;
                for (int j = 0; j < comps.Length; j++)
                {
                    Prop[] props = comps[j].props;
                    if (comps[j].shared >= 0 && d.shared != null && comps[j].shared < d.shared.Length)
                        props = d.shared[comps[j].shared].props;
                    if (made[i][j] == null || props == null) continue;
                    var so = new SerializedObject(made[i][j]);
                    foreach (Prop pr in props) SetProp(so, pr, objects, byNode, parts);
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
                    if (pr.t == "n" && objects != null && pr.n >= 0 && pr.n < objects.Length)
                    {
                        if (pr.s == "GameObject") target = objects[pr.n];
                        else if (pr.s == "Transform") target = objects[pr.n].transform;
                        else
                        {
                            Component c;
                            if (byNode != null && byNode.TryGetValue(pr.n + "|" + pr.s, out c)) target = c;
                            else if (byNode == null) target = objects[pr.n].GetComponent(pr.s);  // scene settings -> a Light
                        }
                    }
                    else if (pr.t == "m")
                    {
                        Renderer r = ModelRenderer(pr.s, parts);
                        var skinned = r as SkinnedMeshRenderer;
                        if (skinned != null) target = skinned.sharedMesh;
                        else if (r != null && r.GetComponent<MeshFilter>() != null) target = r.GetComponent<MeshFilter>().sharedMesh;
                    }
                    else if (pr.c == "Sprite") target = SpriteAt(pr.s, pr.q);
                    else if (!string.IsNullOrEmpty(pr.c)) target = PrefabObject(pr.s, pr.q, pr.c);
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

        static UnityEngine.Object SpriteAt(string texturePath, string name)
        {
            Sprite first = null;
            foreach (UnityEngine.Object o in AssetDatabase.LoadAllAssetsAtPath(texturePath))
            {
                var sprite = o as Sprite;
                if (sprite == null) continue;
                if (sprite.name == name) return sprite;
                if (first == null) first = sprite;
            }
            return first;
        }

        static void ApplySprites(Description d)
        {
            int textures = 0, sprites = 0;
            if (d.sheets != null)
            {
                foreach (Sheet sh in d.sheets)
                {
                    var ti = AssetImporter.GetAtPath(sh.texture) as TextureImporter;
                    if (ti == null || sh.sprites == null || sh.sprites.Length == 0) continue;
                    try
                    {
                        ti.textureType = TextureImporterType.Sprite;
                        ti.spriteImportMode = SpriteImportMode.Multiple;
                        ti.spritePixelsPerUnit = sh.ppu > 0 ? sh.ppu : 100f;
                        ti.alphaIsTransparency = true;
#if UNITY_2021_2_OR_NEWER
                        var factory = new SpriteDataProviderFactories();
                        factory.Init();
                        ISpriteEditorDataProvider dp = factory.GetSpriteEditorDataProviderFromObject(ti);
                        dp.InitSpriteEditorDataProvider();
                        var rects = new List<SpriteRect>();
                        foreach (SpriteInfo s in sh.sprites)
                            rects.Add(new SpriteRect
                            {
                                name = s.name, rect = R(s.rect), pivot = P(s.pivot), alignment = SpriteAlignment.Custom,
                                border = B(s.border), spriteID = GUID.Generate()
                            });
                        dp.SetSpriteRects(rects.ToArray());
#if UNITY_2022_2_OR_NEWER
                        var ids = dp.GetDataProvider<ISpriteNameFileIdDataProvider>();
                        if (ids != null)
                        {
                            var pairs = new List<SpriteNameFileIdPair>();
                            foreach (SpriteRect r in rects) pairs.Add(new SpriteNameFileIdPair(r.name, r.spriteID));
                            ids.SetNameFileIdPairs(pairs);
                        }
#endif
                        dp.Apply();
#else
                        var metas = new List<SpriteMetaData>();
                        foreach (SpriteInfo s in sh.sprites)
                            metas.Add(new SpriteMetaData { name = s.name, rect = R(s.rect), pivot = P(s.pivot), alignment = 9, border = B(s.border) });
                        ti.spritesheet = metas.ToArray();
#endif
                        ti.SaveAndReimport();
                        textures++;
                        sprites += sh.sprites.Length;
                    }
                    catch (Exception e) { Debug.LogWarning("UniView: couldn't set up sprites of " + sh.texture + ": " + e.Message); }
                }
            }
            File.WriteAllText(SpritesMarker, "UniView set up the game's sprites (delete this file to do it again).\n");
            Debug.Log("UniView: " + sprites + " sprite(s) in " + textures + " texture(s)");
        }

        static Rect R(float[] a) { return a != null && a.Length >= 4 ? new Rect(a[0], a[1], a[2], a[3]) : new Rect(); }
        static Vector2 P(float[] a) { return a != null && a.Length >= 2 ? new Vector2(a[0], a[1]) : new Vector2(0.5f, 0.5f); }
        static Vector4 B(float[] a) { return a != null && a.Length >= 4 ? new Vector4(a[0], a[1], a[2], a[3]) : Vector4.zero; }

        static UnityEngine.Object PrefabObject(string path, string childPath, string cls)
        {
            var root = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (root == null) return null;
            Transform t = string.IsNullOrEmpty(childPath) ? root.transform : root.transform.Find(childPath);
            if (t == null) return null;
            if (cls == "GameObject") return t.gameObject;
            if (cls == "Transform") return t;
            Type type = ScriptType(cls) ?? ComponentType(cls);
            return type != null ? t.GetComponent(type) : null;
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

# A runtime script (in its own assembly, so the game's decompiled scripts can't stop it compiling) that puts
# a scene's baked lightmaps back: the editor drops renderers' lightmap indices when a scene has no baked
# lighting data of its own, so they're applied again whenever the scene is loaded, in the editor too.
LIGHTMAPS_CS = r'''// Written by UniView (Game Asset Viewer): the game's baked lightmaps for this scene.
using UnityEngine;

namespace UniView
{
#if UNITY_2018_3_OR_NEWER
    [ExecuteAlways]
#else
    [ExecuteInEditMode]
#endif
    public class SceneLightmaps : MonoBehaviour
    {
        public Texture2D[] lightmaps = new Texture2D[0];
        public Renderer[] renderers = new Renderer[0];
        public int[] indices = new int[0];
        public Vector4[] scaleOffsets = new Vector4[0];

        void OnEnable() { Apply(); }

        public void Apply()
        {
            var data = new LightmapData[lightmaps.Length];
            for (int i = 0; i < lightmaps.Length; i++)
            {
                data[i] = new LightmapData();
                data[i].lightmapColor = lightmaps[i];
            }
            LightmapSettings.lightmaps = data;
            for (int i = 0; i < renderers.Length && i < indices.Length && i < scaleOffsets.Length; i++)
            {
                if (renderers[i] == null) continue;
                renderers[i].lightmapIndex = indices[i];
                renderers[i].lightmapScaleOffset = scaleOffsets[i];
            }
        }
    }
}
'''

LIGHTMAPS_ASMDEF = '{\n    "name": "UniView.Runtime"\n}\n'

# The game's Addressables groups (Assets/UniView/Addressables/): its own editor assembly, compiled only when the
# project has the Addressables package (versionDefines + defineConstraints); UniViewBuilder.cs calls it by reflection.
ADDRESSABLES_CS = r'''// Written by UniView (Game Asset Viewer): puts the game's assets back in Addressables groups with their
// addresses and labels, so Addressables.LoadAssetAsync("address") finds them. Run from UniViewBuilder.cs.
#if UNIVIEW_ADDRESSABLES
using System;
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.AddressableAssets;
using UnityEditor.AddressableAssets.Settings;
using UnityEditor.AddressableAssets.Settings.GroupSchemas;
using UnityEngine;

namespace UniView
{
    [Serializable]
    public class AddressableEntry
    {
        public string asset;    // the asset in this project
        public string address;
        public string[] labels;
        public string group;    // from the bundle it was built into
    }

    [Serializable]
    public class AddressablesList
    {
        public AddressableEntry[] entries;
    }

    public static class AddressablesBuilder
    {
        public static string Apply(string json)
        {
            var list = JsonUtility.FromJson<AddressablesList>(json);
            if (list == null || list.entries == null || list.entries.Length == 0) return "no entries";
            var settings = AddressableAssetSettingsDefaultObject.GetSettings(true);
            if (settings == null) return "couldn't make the Addressables settings";
            var groups = new Dictionary<string, AddressableAssetGroup>();
            var changed = new List<AddressableAssetEntry>();
            int missing = 0;
            foreach (AddressableEntry e in list.entries)
            {
                string guid = AssetDatabase.AssetPathToGUID(e.asset);
                if (string.IsNullOrEmpty(guid) || AssetDatabase.GUIDToAssetPath(guid) != e.asset) { missing++; continue; }
                string name = string.IsNullOrEmpty(e.group) ? "Game Assets" : e.group;
                AddressableAssetGroup group;
                if (!groups.TryGetValue(name, out group))
                {
                    group = settings.FindGroup(name);
                    if (group == null)
                        group = settings.CreateGroup(name, false, false, false, null,
                                                     typeof(BundledAssetGroupSchema), typeof(ContentUpdateGroupSchema));
                    groups[name] = group;
                }
                var entry = settings.CreateOrMoveEntry(guid, group, false, false);
                if (entry == null) { missing++; continue; }
                if (!string.IsNullOrEmpty(e.address)) entry.SetAddress(e.address, false);
                if (e.labels != null)
                    foreach (string label in e.labels)
                    {
                        if (string.IsNullOrEmpty(label)) continue;
                        settings.AddLabel(label, false);
                        entry.SetLabel(label, true, true, false);
                    }
                changed.Add(entry);
            }
            settings.SetDirty(AddressableAssetSettings.ModificationEvent.EntryMoved, changed, true, true);
            AssetDatabase.SaveAssets();
            return changed.Count + " entries in " + groups.Count + " group(s)"
                + (missing > 0 ? ", " + missing + " not in the project" : "");
        }
    }
}
#endif
'''

ADDRESSABLES_ASMDEF = '''{
    "name": "UniView.Addressables.Editor",
    "references": ["Unity.Addressables", "Unity.Addressables.Editor"],
    "includePlatforms": ["Editor"],
    "defineConstraints": ["UNIVIEW_ADDRESSABLES"],
    "versionDefines": [
        {"name": "com.unity.addressables", "expression": "1.0.0", "define": "UNIVIEW_ADDRESSABLES"}
    ]
}
'''
