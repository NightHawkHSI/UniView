"""Export as Unity project: GUIDs, .meta files, versions, package manifest, models referencing textures."""

import json
import os
import struct
import threading

from PIL import Image

from engines.sdk import ALBEDO, NORMAL, Asset, Material, MeshData, TextureRef
from uniview import unity_project as up


class Session:
    def __init__(self, assets, materials=None):
        self.lock = threading.RLock()
        self.assets = assets
        self._materials = materials or {}

    def image(self, a):
        if a.name == "empty":
            raise ValueError("Empty texture")
        return Image.new("RGB", (2, 2))

    def audio(self, a):
        return b"RIFF", "wav"

    def raw(self, a):
        return b"data"

    def mesh(self, a):
        if a.name == "lines":
            raise ValueError("Mesh only has lines/points")
        return MeshData([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[[0, 1, 2]]], uvs={"UV0": [[0, 0], [1, 0], [0, 1]]})

    def materials(self, a):
        return self._materials.get(a.key, [])

    def skeleton(self, a, clip=None):
        raise NotImplementedError

    def stats(self, a):
        if a.name == "empty":
            return {"size": 0, "w": 0, "h": 0}
        if a.name == "lines":
            return {"size": 10, "tris": 0}
        return {"size": 10}


def glb_json(path):
    data = open(path, "rb").read()
    n = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + n])


def fake_editor(tmp_path, modules=("audio", "ui", "video")):
    root = tmp_path / "Hub" / "6000.5.4f1" / "Editor"
    pkgs = root / "Data" / "Resources" / "PackageManager" / "BuiltInPackages"
    for m in modules:
        (pkgs / f"com.unity.modules.{m}").mkdir(parents=True)
    (pkgs / "com.unity.modules.audio.sha1").write_text("x")  # not a package folder name
    (pkgs / "com.unity.collections").mkdir()                # not a built-in module
    (root / "Unity.exe").write_text("")
    return str(root / "Unity.exe")


def test_guid_is_stable_and_unity_shaped():
    g = up.asset_guid("sharedassets0.assets:42")
    assert g == up.asset_guid("sharedassets0.assets:42") != up.asset_guid("sharedassets0.assets:43")
    assert len(g) == 32 and int(g, 16) >= 0


def test_unity_version_helpers():
    assert up.unity_version("", "2019.4.19f1") == "2019.4.19f1"
    assert up.unity_version("2022.3.62f2", "2019.4.11f1") == "2022.3.62f2"
    assert up.unity_version("5.6.7f1", "Unity 2019", None) == ""
    assert up.version_tuple("2019.4.19f1") == (2019, 4, 19) and up.version_tuple("") == ()
    assert not up.uses_gltf("2019.4.40f1") and up.uses_gltf("2020.3.0f1") and up.uses_gltf("6000.5.4f1")
    assert up.gltfast_version("6000.0.1f1") == "6.14.1" and up.gltfast_version("2021.3.1f1") == "6.0.1"


def test_target_version():
    installed = {"2021.3.5f1": "a", "6000.5.4f1": "b", "6000.0.30f1": "c"}
    assert up.target_version("2021.3.5f1", installed) == "2021.3.5f1"   # the game's own, when installed
    assert up.target_version("2017.4.9f1", installed) == "6000.5.4f1"   # else the newest
    assert up.target_version("2017.4.9f1", {}) == "2017.4.9f1"          # nothing installed


def test_installed_editors(tmp_path):
    exe = fake_editor(tmp_path)
    (tmp_path / "Hub" / "junk" / "Editor").mkdir(parents=True)
    assert up.installed_editors([str(tmp_path / "Hub")]) == {"6000.5.4f1": exe}


def test_meta_text_textures_have_serialized_version():
    assert up.meta_text("ab") == "fileFormatVersion: 2\nguid: ab\n"
    assert up.meta_text("ab", 1).endswith("TextureImporter:\n  serializedVersion: 4\n  textureType: 1\n")


def test_write_manifest(tmp_path):
    exe = fake_editor(tmp_path)
    assert up.write_manifest(str(tmp_path / "proj"), exe, "6.14.1")
    deps = json.load(open(tmp_path / "proj" / "Packages" / "manifest.json"))["dependencies"]
    assert deps == {"com.unity.cloud.gltfast": "6.14.1", "com.unity.modules.audio": "1.0.0",
                    "com.unity.modules.ui": "1.0.0", "com.unity.modules.video": "1.0.0"}
    # a package the user added later survives a re-export
    json.dump({"dependencies": {**deps, "com.unity.textmeshpro": "3.0.6"}},
              open(tmp_path / "proj" / "Packages" / "manifest.json", "w"))
    up.write_manifest(str(tmp_path / "proj"), exe, "6.14.1")
    assert "com.unity.textmeshpro" in json.load(open(tmp_path / "proj" / "Packages" / "manifest.json"))["dependencies"]
    assert not up.write_manifest(str(tmp_path / "p2"), None, "6.14.1")


def test_plan_skips_builtins_and_unhandled_kinds(tmp_path):
    assets = [Asset("texture", "Soft", 1, source="unity default resources"),
              Asset("texture", "wall", 2, source="sharedassets0.assets"),
              Asset("texture", "wall", 3, source="sharedassets0.assets"),
              Asset("model", "crate", 4, source="sharedassets0.assets"),
              Asset("scene", "Scene: Main", 6, source="level0"),
              Asset("audio", "boom", 5, source="resources.assets", ext="wav")]
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in up.plan(assets, str(tmp_path))]
    assert rel == ["sharedassets0.assets/Textures/wall.png", "sharedassets0.assets/Textures/wall_2.png",
                   "sharedassets0.assets/Models/crate.glb", "resources.assets/Audio/boom.wav"]
    assert up.plan(assets, str(tmp_path), "obj")[2][1].endswith("crate.obj")


def test_export_writes_project_with_models(tmp_path):
    wall = Asset("texture", "wall", "w", uid="s:1", source="sharedassets0.assets")
    bump = Asset("texture", "wall_n", "n", uid="s:5", source="sharedassets0.assets")
    crate = Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")
    assets = [wall, bump, crate,
              Asset("texture", "empty", 2, uid="s:2", source="sharedassets0.assets"),
              Asset("model", "lines", "l", uid="s:7", source="sharedassets1.assets"),
              Asset("audio", "boom", 3, uid="r:3", source="resources.assets", ext="wav"),
              Asset("text", "readme", 4, uid="r:4", source="resources.assets", ext="txt")]
    mats = {"c": [Material("crate_mat", [TextureRef("_MainTex", "wall", wall, ALBEDO),
                                         TextureRef("_BumpMap", "wall_n", bump, NORMAL)])]}
    exe = fake_editor(tmp_path)
    calls = []
    root = tmp_path / "proj"
    written, failed, skipped = up.export_unity_project(Session(assets, mats), str(root), "6000.5.4f1",
                                                       progress=lambda d, t, n: calls.append((d, t)), editor_exe=exe)
    assert (written, failed, skipped) == (5, 0, 2)
    assert calls[-1] == (7, 7)
    assert open(root / "ProjectSettings" / "ProjectVersion.txt").read() == "m_EditorVersion: 6000.5.4f1\n"
    assert "com.unity.cloud.gltfast" in open(root / "Packages" / "manifest.json").read()
    tex = root / "Assets" / "sharedassets0.assets" / "Textures"
    assert open(str(tex / "wall.png") + ".meta").read() == up.meta_text(up.asset_guid("s:1"), 0)
    assert open(str(tex / "wall_n.png") + ".meta").read() == up.meta_text(up.asset_guid("s:5"), 1)  # normal map
    glb = root / "Assets" / "sharedassets1.assets" / "Models" / "crate.glb"
    assert open(str(glb) + ".meta").read() == up.meta_text(up.asset_guid("s:6"))
    gltf = glb_json(glb)
    uris = [img["uri"] for img in gltf["images"]]
    assert uris == ["../../sharedassets0.assets/Textures/wall.png", "../../sharedassets0.assets/Textures/wall_n.png"]
    assert "normalTexture" in gltf["materials"][0]
    assert "bufferView" not in gltf["images"][0]  # referenced, not embedded
    assert "folderAsset: yes" in open(root / "Assets" / "sharedassets0.assets" / "Textures.meta").read()
    assert not (root / "Assets.meta").exists()


def test_export_old_unity_uses_obj_without_manifest(tmp_path):
    assets = [Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")]
    written, failed, _ = up.export_unity_project(Session(assets), str(tmp_path), "2019.4.19f1",
                                                 editor_exe=fake_editor(tmp_path))
    models = tmp_path / "Assets" / "sharedassets1.assets" / "Models"
    assert failed == 0 and (models / "crate.obj").exists()
    assert (models / "crate.obj.meta").exists()
    assert not (tmp_path / "Packages").exists()


def test_export_cancel(tmp_path):
    assets = [Asset("text", f"t{i}", i, uid=f"t{i}", source="x", ext="txt") for i in range(30)]
    written, _, _ = up.export_unity_project(Session(assets), str(tmp_path), cancelled=lambda: True)
    assert written == 0
    assert not (tmp_path / "ProjectSettings" / "ProjectVersion.txt").exists()  # no version given


# ---------------------------------------------------------------------------- prefabs

class PrefabSession(Session):
    def hierarchy(self, asset):
        return [
            {"name": "Crate", "parent": -1, "active": True, "pos": [1, 2, 3], "rot": [0, 0, 0, 1],
             "scale": [1, 1, 1], "mesh": None, "skinned": False, "renderer_enabled": True},
            {"name": "Body", "parent": 0, "active": False, "pos": [0, 0, 0], "rot": [0, 0, 0, 1],
             "scale": [2, 2, 2], "mesh": "s:6", "skinned": False, "renderer_enabled": False},
            {"name": "Ball", "parent": 0, "active": True, "pos": [0, 1, 0], "rot": [0, 0, 0, 1],
             "scale": [1, 1, 1], "mesh": "builtin:1", "skinned": False, "renderer_enabled": True},
            {"name": "Ghost", "parent": 0, "active": True, "pos": [0, 1, 0], "rot": [0, 0, 0, 1],
             "scale": [1, 1, 1], "mesh": "missing:9", "skinned": True, "renderer_enabled": True},
        ]


def test_plan_prefabs(tmp_path):
    assets = [Asset("scene", "Prefab: prefabs/cubes/corner", ("prefab", "c"), uid="prefab:c",
                    path="assets/prefabs/cubes/corner.prefab", source="shared"),
              Asset("scene", "Prefab: Boat", ("root", 1, 2), uid="prefab:b1", source="sharedassets1.assets"),
              Asset("scene", "Prefab: Boat", ("root", 3, 2), uid="prefab:b2", source="sharedassets1.assets"),
              Asset("scene", "Scene: Main", ("scene", 5), uid="scene:level0", source="level0")]
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in up.plan_prefabs(assets, str(tmp_path))]
    assert rel == ["assets/prefabs/cubes/corner.prefab", "sharedassets1.assets/Prefabs/Boat.prefab",
                   "sharedassets1.assets/Prefabs/Boat_2.prefab"]


def test_export_writes_prefab_descriptions_and_builder(tmp_path):
    crate = Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")
    sphere = Asset("model", "Sphere", "b", uid="builtin:1", source="unity default resources")
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    root = tmp_path / "proj"
    written, failed, _ = up.export_unity_project(PrefabSession([crate, sphere, prefab]), str(root), "6000.5.4f1",
                                                 editor_exe=fake_editor(tmp_path))
    assert (written, failed) == (2, 0)  # the GLB + one prefab description
    desc_path = root / "Assets" / "UniView" / "Build" / "sharedassets1.assets" / "Prefabs" / "Crate.prefab.json"
    desc = json.load(open(desc_path))
    assert desc["kind"] == "prefab" and desc["target"] == "Assets/sharedassets1.assets/Prefabs/Crate.prefab"
    nodes = desc["nodes"]
    assert [n["name"] for n in nodes] == ["Crate", "Body", "Ball", "Ghost"]
    assert nodes[1]["model"] == "Assets/sharedassets1.assets/Models/crate.glb" and not nodes[1]["active"]
    assert nodes[1]["rendererEnabled"] is False and nodes[1]["scale"] == [2.0, 2.0, 2.0]
    assert nodes[2]["model"] == "" and nodes[2]["builtin"] == "Sphere"
    assert nodes[3]["model"] == "" and nodes[3]["builtin"] == "" and nodes[3]["skinned"] is True
    builder = root / "Assets" / "UniView" / "Editor" / "UniViewBuilder.cs"
    assert "PrefabUtility.SaveAsPrefabAsset" in builder.read_text() and (root / "Assets" / "UniView" / "Editor" / "UniViewBuilder.cs.meta").exists()
    assert (root / "Assets" / "UniView.meta").exists() and str(desc_path) + ".meta"


def test_no_builder_without_prefabs(tmp_path):
    up.export_unity_project(PrefabSession([Asset("text", "t", 1, uid="t", source="x", ext="txt")]), str(tmp_path))
    assert not (tmp_path / "Assets" / "UniView").exists()


def test_builder_json_fields_match_the_cs_classes():
    """JsonUtility silently ignores unknown fields: every key we write must exist in the C# Node class."""
    from uniview.unity_builder import BUILDER_CS
    import re
    cs_node = re.search(r"public class Node\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    fields = set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_node))
    desc = up.prefab_description(PrefabSession([]).hierarchy(None), "Assets/x.prefab", {}, "")
    assert set(desc["nodes"][0]) <= fields
    cs_desc = re.search(r"public class Description\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(desc) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_desc))
    cs_batch = re.search(r"public class BatchRef\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert {"model", "materials"} <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_batch))
    cs_light = re.search(r"public class LightInfo\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    light = up.prefab_description([{**PrefabSession([]).hierarchy(None)[0],
                                    "light": {"type": 1, "color": [1, 1, 1, 1], "intensity": 1, "range": 1,
                                              "spot_angle": 1}}], "Assets/x.unity", {}, "")["nodes"][0]["light"]
    assert set(light) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_light))


# ---------------------------------------------------------------------------- scenes

class SceneSession(Session):
    """A scene: a lit, statically batched 2-object level, one object hidden, plus a terrain."""

    def __init__(self, assets):
        super().__init__(assets)
        self.red, self.blue = Material("red", color=(1, 0, 0, 1)), Material("blue", color=(0, 0, 1, 1))

    def mesh(self, a):
        if a.uid == "combined":  # 3 submeshes: one per batched renderer
            pts = [[i, 0, 0] for i in range(9)]
            return MeshData(pts, [[[0, 1, 2]], [[3, 4, 5]], [[6, 7, 8]]])
        return super().mesh(a)

    def hierarchy(self, asset):
        base = {"rot": [0, 0, 0, 1], "scale": [1, 1, 1], "mesh": None, "skinned": False, "renderer_enabled": True,
                "pos": [0, 0, 0]}
        return [
            {**base, "name": "Level", "parent": -1, "active": True},
            {**base, "name": "Wall", "parent": 0, "active": True,
             "batch": {"mesh": "combined", "first": 0, "count": 1, "materials": [self.red]}},
            {**base, "name": "Floor", "parent": 0, "active": True,
             "batch": {"mesh": "combined", "first": 1, "count": 1, "materials": [self.blue]}},
            {**base, "name": "Hidden", "parent": 0, "active": False,
             "batch": {"mesh": "combined", "first": 2, "count": 1, "materials": [self.red]}},
            {**base, "name": "Sun", "parent": -1, "active": True,
             "light": {"type": 1, "color": [1, 0.9, 0.8, 1], "intensity": 1.2, "range": 10, "spot_angle": 30}},
            {**base, "name": "Ground", "parent": -1, "active": True, "terrain": "terrain:level0:5"},
        ]


def test_visible_nodes():
    nodes = [{"parent": -1, "active": True}, {"parent": 0, "active": False}, {"parent": 1, "active": True},
             {"parent": 0, "active": True}]
    assert up.visible_nodes(nodes) == [True, False, False, True]


def test_plan_scenes(tmp_path):
    assets = [Asset("scene", "Scene: Main", ("scene", 1), source="level0"),
              Asset("scene", "Scene: Main", ("scene", 2), source="level1"),
              Asset("scene", "Prefab: Boat", ("root", 1, 2), source="x")]
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in up.plan_scenes(assets, str(tmp_path))]
    assert rel == ["Scenes/Main.unity", "Scenes/Main_2.unity"]


def test_export_scene_with_static_batches_lights_and_terrain(tmp_path):
    combined = Asset("model", "Combined Mesh (root: scene) 1", "cm", uid="combined", source="level0")
    terrain = Asset("model", "Terrain: Ground", "tr", uid="terrain:level0:5", source="level0")
    scene = Asset("scene", "Scene: Main", ("scene", 1), uid="scene:level0", source="level0")
    root = tmp_path / "proj"
    written, failed, _ = up.export_unity_project(SceneSession([combined, terrain, scene]), str(root), "6000.5.4f1",
                                                 editor_exe=fake_editor(tmp_path))
    assert failed == 0
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Scenes" / "Main.unity.json"))
    assert desc["kind"] == "scene" and desc["target"] == "Assets/Scenes/Main.unity"
    nodes = {n["name"]: n for n in desc["nodes"]}
    assert nodes["Wall"]["model"] == "" and nodes["Floor"]["model"] == ""  # drawn by the batch instead
    assert nodes["Ground"]["model"] == "Assets/level0/Models/Terrain_ Ground.glb"
    assert nodes["Sun"]["light"] == {"present": True, "type": 1, "color": [1.0, 0.9, 0.8, 1.0], "intensity": 1.2,
                                     "range": 10, "spotAngle": 30, "enabled": True}
    assert "light" not in nodes["Wall"]
    assert len(desc["batches"]) == 1 and desc["batches"][0]["model"].startswith("Assets/Scenes/Main_StaticBatches/")
    glb = root / desc["batches"][0]["model"]
    gltf = glb_json(glb)
    prims = gltf["meshes"][0]["primitives"]
    assert len(prims) == 2  # the hidden object's submesh is left out
    colors = [gltf["materials"][p["material"]]["pbrMetallicRoughness"]["baseColorFactor"] for p in prims]
    assert colors == [[1, 0, 0, 1], [0, 0, 1, 1]]
    assert os.path.exists(str(glb) + ".meta")


# ---------------------------------------------------------------------------- .mat files

class MaterialSession(PrefabSession):
    def hierarchy(self, asset):
        nodes = super().hierarchy(asset)
        nodes[1]["materials"] = ["material:sharedassets1.assets:77", None]
        return nodes

    def material_details(self, uid):
        wall = next(a for a in self.assets if a.name == "wall")
        return {"name": "Shield", "shader": "Custom/Translucent", "textures": [("_MainTex", wall, (1, 1), (0, 0))],
                "colors": {"_Color": (0, 0.5, 1, 0.3)}, "floats": {}, "keywords": [], "queue": -1, "tags": {}}


def test_export_writes_mat_files_for_renderers(tmp_path):
    wall = Asset("texture", "wall", "w", uid="s:1", source="sharedassets0.assets")
    crate = Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    root = tmp_path / "proj"
    written, failed, _ = up.export_unity_project(MaterialSession([wall, crate, prefab]), str(root), "6000.5.4f1",
                                                 editor_exe=fake_editor(tmp_path))
    assert failed == 0
    mat = root / "Assets" / "sharedassets1.assets" / "Materials" / "Shield.mat"
    text = mat.read_text()
    assert "m_CustomRenderQueue: 3000" in text and up.asset_guid("s:1") in text  # transparent, texture by GUID
    assert open(str(mat) + ".meta").read() == up.meta_text(up.asset_guid("material:sharedassets1.assets:77"))
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "sharedassets1.assets" / "Prefabs" / "Crate.prefab.json"))
    body = desc["nodes"][1]
    assert body["materials"] == ["Assets/sharedassets1.assets/Materials/Shield.mat", ""]
    assert desc["nodes"][0]["materials"] == []  # no renderer, no materials
    assert body["noMaterials"] is False and desc["nodes"][0]["noMaterials"] is False


def test_renderer_without_materials_is_marked():
    nodes = PrefabSession([]).hierarchy(None)
    nodes[1]["materials"] = []           # the game's renderer has an empty material list
    desc = up.prefab_description(nodes, "Assets/x.prefab", {"s:6": "/p/Assets/m.glb"}, "/p")
    assert desc["nodes"][1]["noMaterials"] is True
    assert desc["nodes"][2]["noMaterials"] is False  # no "materials" key: unknown, keep the fallback
