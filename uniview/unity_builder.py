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
        public Sheet[] sheets;  // kind "sprites"
        public ClipInfo clip;   // kind "clip"
        public CtrlInfo controller;  // kind "controller"
        public string script;  // kind "data": the ScriptableObject class
        public Prop[] props;   // kind "data": its values
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
        const string ScriptsParked = "Assets/UniView/GameScripts~";   // ignored by Unity
        const string ScriptsActive = "Assets/GameScripts";
        const string RebuildMarker = "ProjectSettings/UniViewRebuildAfterScripts.txt";

        static void BuildMissingIfAny()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            if (File.Exists(RebuildMarker) && Directory.Exists(ScriptsActive))
            {
                // The game's scripts were just added and compile: build again so the script components get attached.
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
                if (d != null && !File.Exists(d.target)) { BuildMissing(); return; }
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
                "Move the game's decompiled code into " + ScriptsActive + " so Unity compiles it?\n\n"
                + "Decompiled code often needs fixes before it compiles. If there are errors, fix them, or use "
                + "UniView > Remove the game's scripts to go back. Once it compiles, the prefabs and scenes are "
                + "rebuilt with their script components.", "Add scripts", "Cancel")) return;
            Directory.Move(ScriptsParked, ScriptsActive);
            if (File.Exists(ScriptsParked + ".meta")) File.Delete(ScriptsParked + ".meta");
            File.WriteAllText(RebuildMarker, "UniView: rebuild prefabs and scenes once the game's scripts compile.\n");
            AssetDatabase.Refresh();
        }

        [MenuItem("UniView/Add the game's scripts (decompiled)", true)]
        static bool CanAddGameScripts() { return Directory.Exists(ScriptsParked) && !Directory.Exists(ScriptsActive); }

        [MenuItem("UniView/Remove the game's scripts")]
        public static void RemoveGameScripts()
        {
            if (!Directory.Exists(ScriptsActive)) return;
            Directory.Move(ScriptsActive, ScriptsParked);
            if (File.Exists(ScriptsActive + ".meta")) File.Delete(ScriptsActive + ".meta");
            if (File.Exists(RebuildMarker)) File.Delete(RebuildMarker);
            AssetDatabase.Refresh();
            Debug.Log("UniView: the game's scripts were moved back to " + ScriptsParked + " (not compiled)");
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

        static Description Load(string file)
        {
            try { return JsonUtility.FromJson<Description>(File.ReadAllText(file)); }
            catch (Exception e) { Debug.LogWarning("UniView: can't read " + file + ": " + e.Message); return null; }
        }

        static void Build(bool onlyMissing)
        {
            var todo = new List<Description>();
            var data = new List<Description>();
            var clips = new List<Description>();
            var controllers = new List<Description>();
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
                if (d == null || d.nodes == null || d.nodes.Length == 0) continue;
                if (onlyMissing && File.Exists(d.target)) continue;
                todo.Add(d);
            }
            int clipsMade = BuildClips(clips);  // before prefabs: Animation components point at clips
            int controllersMade = BuildControllers(controllers);  // Animators point at controllers
            int dataMade = CreateDataAssets(data);
            if (todo.Count == 0)
            {
                FillDataAssets(data);
                AssetDatabase.SaveAssets();
                if (data.Count > 0 || clips.Count > 0)
                    Debug.Log("UniView: " + dataMade + " data asset(s), " + clipsMade + " animation clip(s) built");
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
            FillDataAssets(data);  // after the prefabs exist, so data can point at them
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
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

        static Dictionary<string, Type> scriptTypes;
        static int scriptsAdded, scriptsMissing;
        static readonly HashSet<string> missingScripts = new HashSet<string>();

        static Type ScriptType(string fullName)
        {
            if (scriptTypes == null)
            {
                scriptTypes = new Dictionary<string, Type>();
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type[] types;
                    try { types = asm.GetTypes(); } catch (Exception) { continue; }
                    foreach (Type t in types)
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
                    if (pr.t == "n" && objects != null && pr.n >= 0 && pr.n < objects.Length)
                    {
                        if (pr.s == "GameObject") target = objects[pr.n];
                        else if (pr.s == "Transform") target = objects[pr.n].transform;
                        else { Component c; if (byNode != null && byNode.TryGetValue(pr.n + "|" + pr.s, out c)) target = c; }
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
