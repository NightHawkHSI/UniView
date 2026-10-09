"""Export as Unity project: GUIDs, .meta files, versions, package manifest, models referencing textures."""

import json
import os
import struct
import threading

import numpy as np
from PIL import Image

from engines.sdk import ALBEDO, NORMAL, Asset, Material, MeshData, TextureRef
from tests.test_glb import accessor, parse_glb
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


def test_write_manifest_for_another_editor_drops_old_packages(tmp_path):
    proj = tmp_path / "proj"
    manifest, lock = proj / "Packages" / "manifest.json", proj / "Packages" / "packages-lock.json"
    up.write_manifest(str(proj), fake_editor(tmp_path, ("accessibility", "audio")), "6.14.1",
                      {"com.unity.collections": "6.5.0"}, "6000.5.4f1")
    deps = json.load(open(manifest))["dependencies"]
    json.dump({"dependencies": {**deps, "com.unity.textmeshpro": "3.0.6"}}, open(manifest, "w"))
    lock.write_text("{}")
    old_editor = tmp_path / "old"
    old_editor.mkdir()
    exe = fake_editor(old_editor, ("audio",))
    up.write_manifest(str(proj), exe, "6.0.1", {"com.unity.collections": "1.2.3"}, "2021.3.5f1")
    assert json.load(open(manifest))["dependencies"] == {
        "com.unity.cloud.gltfast": "6.0.1", "com.unity.collections": "1.2.3", "com.unity.modules.audio": "1.0.0",
        "com.unity.textmeshpro": "3.0.6"}  # the user's package stays, Unity 6's module and versions go
    assert not lock.exists()


def test_write_manifest_without_record_drops_export_packages(tmp_path):
    """Projects exported before Packages/uniview-packages.json existed."""
    manifest = tmp_path / "proj" / "Packages" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    json.dump({"dependencies": {"com.unity.modules.amd": "1.0.0", "com.unity.visualeffectgraph": "17.5.0",
                                "com.example.tool": "1.0.0"}}, open(manifest, "w"))
    up.write_manifest(str(tmp_path / "proj"), fake_editor(tmp_path, ("audio",)), "6.0.1", version="2021.3.5f1")
    assert json.load(open(manifest))["dependencies"] == {
        "com.example.tool": "1.0.0", "com.unity.cloud.gltfast": "6.0.1", "com.unity.modules.audio": "1.0.0"}


def test_plan_skips_builtins_and_unhandled_kinds(tmp_path):
    assets = [Asset("texture", "Soft", 1, source="unity default resources"),
              Asset("texture", "wall", 2, source="sharedassets0.assets"),
              Asset("texture", "wall", 3, source="sharedassets0.assets"),
              Asset("model", "crate", 4, source="sharedassets0.assets"),
              Asset("scene", "Scene: Main", 6, source="level0"),
              Asset("audio", "boom", 5, source="resources.assets", ext="wav")]
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in up.plan(assets, str(tmp_path))]
    assert rel == ["Textures/wall.png", "Textures/wall_2.png", "Models/crate.glb", "Audio/boom.wav"]
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
    tex = root / "Assets" / "Textures"
    assert open(str(tex / "wall.png") + ".meta").read() == up.meta_text(up.asset_guid("s:1"), 0)
    assert open(str(tex / "wall_n.png") + ".meta").read() == up.meta_text(up.asset_guid("s:5"), 1)  # normal map
    glb = root / "Assets" / "Models" / "crate.glb"
    assert open(str(glb) + ".meta").read() == up.meta_text(up.asset_guid("s:6"))
    gltf = glb_json(glb)
    uris = [img["uri"] for img in gltf["images"]]
    assert uris == ["../Textures/wall.png", "../Textures/wall_n.png"]
    assert "normalTexture" in gltf["materials"][0]
    assert "bufferView" not in gltf["images"][0]  # referenced, not embedded
    assert "folderAsset: yes" in open(root / "Assets" / "Textures.meta").read()
    assert not (root / "Assets.meta").exists()


def test_export_old_unity_uses_obj_without_manifest(tmp_path):
    assets = [Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")]
    written, failed, _ = up.export_unity_project(Session(assets), str(tmp_path), "2019.4.19f1",
                                                 editor_exe=fake_editor(tmp_path))
    models = tmp_path / "Assets" / "Models"
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
    assert rel == ["prefabs/cubes/corner.prefab", "Prefabs/Boat.prefab", "Prefabs/Boat_2.prefab"]


def test_export_writes_prefab_descriptions_and_builder(tmp_path):
    crate = Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")
    sphere = Asset("model", "Sphere", "b", uid="builtin:1", source="unity default resources")
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    root = tmp_path / "proj"
    written, failed, _ = up.export_unity_project(PrefabSession([crate, sphere, prefab]), str(root), "6000.5.4f1",
                                                 editor_exe=fake_editor(tmp_path))
    assert (written, failed) == (2, 0)  # the GLB + one prefab description
    desc_path = root / "Assets" / "UniView" / "Build" / "Prefabs" / "Crate.prefab.json"
    desc = json.load(open(desc_path))
    assert desc["kind"] == "prefab" and desc["target"] == "Assets/Prefabs/Crate.prefab"
    nodes = desc["nodes"]
    assert [n["name"] for n in nodes] == ["Crate", "Body", "Ball", "Ghost"]
    assert nodes[1]["model"] == "Assets/Models/crate.glb" and not nodes[1]["active"]
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
    cs_prop = re.search(r"public class Prop\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert {"p", "t", "v", "s", "n"} <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_prop))
    cs_comp = re.search(r"public class Comp\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert {"type", "props"} <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_comp))
    cs_light = re.search(r"public class LightInfo\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    light = up.prefab_description([{**PrefabSession([]).hierarchy(None)[0],
                                    "light": {"type": 1, "color": [1, 1, 1, 1], "intensity": 1, "range": 1,
                                              "spot_angle": 1}}], "Assets/x.unity", {}, "")["nodes"][0]["light"]
    assert set(light) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_light))
    cs_rect = re.search(r"public class RectInfo\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    ui = {"anchor_min": [0, 0], "anchor_max": [1, 1], "pos": [3, 4], "size": [-10, 20], "pivot": [0.5, 1]}
    rect = up.prefab_description([{**PrefabSession([]).hierarchy(None)[0], "rect": ui}], "Assets/x.prefab", {},
                                 "")["nodes"][0]["rect"]
    assert set(rect) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_rect))
    assert rect == {"present": True, "anchorMin": [0, 0], "anchorMax": [1, 1], "anchoredPosition": [3, 4],
                    "sizeDelta": [-10, 20], "pivot": [0.5, 1]}
    assert "rect" not in desc["nodes"][0]


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
    assert nodes["Ground"]["model"] == "Assets/Models/Terrain_ Ground.glb"
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
    mat = root / "Assets" / "Materials" / "Shield.mat"
    text = mat.read_text()
    assert "m_CustomRenderQueue: 3000" in text and up.asset_guid("s:1") in text  # transparent, texture by GUID
    assert open(str(mat) + ".meta").read() == up.meta_text(up.asset_guid("material:sharedassets1.assets:77"))
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Prefabs" / "Crate.prefab.json"))
    body = desc["nodes"][1]
    assert body["materials"] == ["Assets/Materials/Shield.mat", ""]
    assert desc["nodes"][0]["materials"] == []  # no renderer, no materials
    assert body["noMaterials"] is False and desc["nodes"][0]["noMaterials"] is False


def test_renderer_without_materials_is_marked():
    nodes = PrefabSession([]).hierarchy(None)
    nodes[1]["materials"] = []           # the game's renderer has an empty material list
    desc = up.prefab_description(nodes, "Assets/x.prefab", {"s:6": "/p/Assets/m.glb"}, "/p")
    assert desc["nodes"][1]["noMaterials"] is True
    assert desc["nodes"][2]["noMaterials"] is False  # no "materials" key: unknown, keep the fallback


# ---------------------------------------------------------------------------- project settings

class SettingsSession(PrefabSession):
    def project_settings(self):
        return {"managers": [{"type": "TagManager", "props": [{"p": "tags.Array.size", "t": "i", "v": 1},
                                                             {"p": "tags.Array.data[0]", "t": "s", "s": "Enemy"}]}],
                "tags": ["Enemy"], "scene_order": ["Assets/Menu.unity", "Assets/Game.unity"],
                "product": "Muck", "company": "Dani"}

    def hierarchy(self, asset):
        nodes = super().hierarchy(asset)
        nodes[1].update(layer=9, tag="Enemy")
        return nodes


def test_settings_and_layers_tags(tmp_path):
    crate = Asset("model", "crate", "c", uid="s:6", source="sharedassets1.assets")
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    game = Asset("scene", "Scene: Game", ("scene", 2), uid="scene:level1", source="level1")
    menu = Asset("scene", "Scene: Menu", ("scene", 1), uid="scene:level0", source="level0")
    root = tmp_path / "proj"
    up.export_unity_project(SettingsSession([crate, prefab, game, menu]), str(root), "6000.5.4f1",
                            editor_exe=fake_editor(tmp_path))
    settings = json.load(open(root / "Assets" / "UniView" / "Build" / "ProjectSettings.json"))
    assert settings["kind"] == "settings" and settings["product"] == "Muck" and settings["company"] == "Dani"
    assert settings["scenes"] == ["Assets/Scenes/Menu.unity", "Assets/Scenes/Game.unity"]  # build order, by level
    assert settings["managers"][0]["type"] == "TagManager"
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Prefabs" / "Crate.prefab.json"))
    assert desc["nodes"][1]["layer"] == 9 and desc["nodes"][1]["tag"] == "Enemy"
    from uniview.unity_builder import BUILDER_CS
    import re
    cs_desc = re.search(r"public class Description\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(settings) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_desc))


# ---------------------------------------------------------------------------- data assets, links between prefabs

class DataSession(PrefabSession):
    def data_asset(self, asset):
        return "InventoryItem", [{"p": "name", "t": "s", "s": "Dark Oak Wood"},
                                 {"p": "dropPrefab", "t": "ref", "asset": "go:shared:7", "kind": "gameobject", "cls": "GameObject"},
                                 {"p": "arm", "t": "ref", "asset": "go:shared:9", "kind": "gameobject", "cls": "Rigidbody"},
                                 {"p": "next", "t": "ref", "asset": "d:2", "kind": "data"}]

    def object_paths(self, asset):
        return {"go:shared:7": "", "go:shared:9": "Body/Arm"}


def test_data_assets_and_prefab_links(tmp_path):
    from types import SimpleNamespace
    mb = SimpleNamespace(type=SimpleNamespace(name="MonoBehaviour"))
    item = Asset("data", "Wood_DarkOak", "d1", uid="d:1", source="sharedassets0.assets", ref=mb)
    other = Asset("data", "Wood_Oak", "d2", uid="d:2", source="sharedassets0.assets", ref=mb)
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    root = tmp_path / "proj"
    written, failed, _ = up.export_unity_project(DataSession([item, other, prefab]), str(root), "6000.5.4f1",
                                                 editor_exe=fake_editor(tmp_path))
    assert failed == 0
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Data" / "Wood_DarkOak.asset.json"))
    assert desc["kind"] == "data" and desc["script"] == "InventoryItem"
    assert desc["target"] == "Assets/Data/Wood_DarkOak.asset"
    props = {e["p"]: e for e in desc["props"]}
    assert props["name"] == {"p": "name", "t": "s", "s": "Dark Oak Wood"}
    crate = "Assets/Prefabs/Crate.prefab"
    assert props["dropPrefab"] == {"p": "dropPrefab", "t": "a", "s": crate, "c": "GameObject", "q": ""}
    assert props["arm"] == {"p": "arm", "t": "a", "s": crate, "c": "Rigidbody", "q": "Body/Arm"}
    assert props["next"] == {"p": "next", "t": "a", "s": "Assets/Data/Wood_Oak.asset"}
    from uniview.unity_builder import BUILDER_CS
    import re
    fields = lambda cls: set(re.findall(r"public \w+(?:\[\])? (\w+)",  # noqa: E731
                                        re.search(rf"public class {cls}\s*\{{(.*?)\}}", BUILDER_CS, re.S).group(1)))
    assert set(desc) <= fields("Description")
    assert {"p", "t", "v", "s", "n", "c", "q"} <= fields("Prop")


def test_clip_description_matches_builder_classes(tmp_path):
    from types import SimpleNamespace
    import re
    from uniview.unity_builder import BUILDER_CS

    class ClipSession(PrefabSession):
        def clip_export(self, asset):
            return {"name": "Walk", "length": 1.0, "sample_rate": 30, "loop": True, "legacy": False,
                    "events": [{"time": 0.5, "function": "Step", "data": "", "float": 0.0, "int": 1}],
                    "curves": [{"path": "Body", "type": "Transform", "prop": "m_LocalPosition.x",
                                "keys": [(0.0, 1.0), (1.0, 2.0)]}],
                    "object_curves": [], "skipped": 0}

    clip = Asset("animation", "Walk", "w", uid="a:1", source="sharedassets0.assets",
                 ref=SimpleNamespace(type=SimpleNamespace(name="AnimationClip")))
    prefab = Asset("scene", "Prefab: Crate", ("root", 1, 1), uid="prefab:crate", source="sharedassets1.assets")
    root = tmp_path / "proj"
    up.export_unity_project(ClipSession([clip, prefab]), str(root), "6000.5.4f1", editor_exe=fake_editor(tmp_path))
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Animations" / "Walk.anim.json"))
    assert desc["kind"] == "clip" and desc["target"] == "Assets/Animations/Walk.anim"
    c = desc["clip"]
    assert c["loop"] is True and c["curves"][0]["keys"] == [0.0, 1.0, 1.0, 2.0]
    fields = lambda cls: set(re.findall(r"public \w+(?:\[\])? (\w+)",  # noqa: E731
                                        re.search(rf"public class {cls}\s*\{{(.*?)\}}", BUILDER_CS, re.S).group(1)))
    assert set(c) <= fields("ClipInfo")
    assert set(c["curves"][0]) <= fields("ClipCurve") and set(c["events"][0]) <= fields("ClipEvent")
    assert set(desc) <= fields("Description")


def test_controller_json_matches_builder_classes():
    import re
    from engines.unity_controller import decode_controller
    from uniview.unity_builder import BUILDER_CS
    tree = {"m_Name": "Cow", "m_TOS": [(1, "Speed"), (2, "Base Layer"), (3, "Idle"), (4, "Walk"), (5, "Grounded")],
            "m_Controller": {
                "m_Values": {"data": {"m_ValueArray": [{"m_ID": 1, "m_Type": 1, "m_Index": 0},
                                                       {"m_ID": 5, "m_Type": 4, "m_Index": 0}]}},
                "m_DefaultValues": {"data": {"m_FloatValues": [0.5], "m_BoolValues": [True]}},
                "m_LayerArray": [{"data": {"m_StateMachineIndex": 0, "m_Binding": 2, "m_DefaultWeight": 0.0}}],
                "m_StateMachineArray": [{"data": {"m_DefaultState": 0, "m_AnyStateTransitionConstantArray": [], "m_StateConstantArray": [
                    {"data": {"m_NameID": 3, "m_BlendTreeConstantArray": [{"data": {"m_NodeArray": [{"data": {"m_ClipID": 0}}]}}],
                              "m_TransitionConstantArray": [{"data": {"m_DestinationState": 1, "m_ConditionConstantArray": [
                                  {"data": {"m_ConditionMode": 3, "m_EventID": 1, "m_EventThreshold": 0.1}}]}}]}},
                    {"data": {"m_NameID": 4, "m_BlendTreeConstantArray": [{"data": {"m_NodeArray": [
                        {"data": {"m_BlendType": 0, "m_BlendEventID": 1, "m_ChildIndices": [1, 2],
                                  "m_Blend1dData": {"data": {"m_ChildThresholdArray": [0.0, 1.0]}}, "m_ClipID": 0xFFFFFFFF}},
                        {"data": {"m_ClipID": 1}}, {"data": {"m_ClipID": 0}}]}}],
                              "m_TransitionConstantArray": [{"data": {"m_DestinationState": 99}}]}}]}}]}}
    ctrl = decode_controller(tree, lambda i: f"clip{i}")
    assert ctrl["parameters"] == [{"name": "Speed", "type": "Float", "default": 0.5},
                                  {"name": "Grounded", "type": "Bool", "default": 1.0}]
    layer = ctrl["layers"][0]
    assert layer["name"] == "Base Layer" and [s["name"] for s in layer["states"]] == ["Idle", "Walk"]
    idle, walk = layer["states"]
    assert idle["motion"] == {"clip": "clip0"}
    assert idle["transitions"][0]["dest"] == 1 and idle["transitions"][0]["conditions"] == [
        {"mode": 3, "param": "Speed", "threshold": 0.1}]
    assert walk["transitions"][0]["dest"] == -1  # out of range -> exit
    nodes = walk["motion"]["tree"]["nodes"]
    assert nodes[0]["param"] == "Speed" and nodes[0]["children"] == [1, 2] and nodes[0]["thresholds"] == [0.0, 1.0]
    js = up.controller_json(ctrl, lambda uid: f"Assets/{uid}.anim" if uid else "")
    def names(cls):
        """Field names of a C# class, including several per line ("public float a = 1f, b;")."""
        body = re.search(rf"public class {cls}\s*\{{(.*?)\}}", BUILDER_CS, re.S).group(1)
        out = set()
        for decl in re.findall(r"public \w+(?:\[\])? ([^;]+);", body):
            out.update(part.split("=")[0].strip() for part in decl.split(","))
        return out
    assert set(js) <= names("CtrlInfo") and set(js["parameters"][0]) <= names("CtrlParam")
    assert set(js["layers"][0]) <= names("CtrlLayer") and set(js["layers"][0]["states"][0]) <= names("CtrlState")
    assert set(js["layers"][0]["states"][0]["transitions"][0]) <= names("CtrlTransition")
    assert set(js["layers"][0]["states"][1]["treeNodes"][0]) <= names("TreeNode")


def test_identical_components_share_one_prop_list():
    """Big maps hold thousands of copies of one particle effect: their values are written once."""
    from uniview.unity_builder import BUILDER_CS
    import re
    big = [{"p": f"m_Value{k}", "t": "f", "v": float(k)} for k in range(up.SHARE_MIN_PROPS)]
    base = PrefabSession([]).hierarchy(None)[0]
    nodes = [{**base, "name": f"fx{i}", "parent": -1,
              "components": [{"type": "ParticleSystem", "props": big},
                             {"type": "BoxCollider", "props": [{"p": "m_IsTrigger", "t": "b", "v": i % 2}]}]}
             for i in range(3)]
    desc = up.prefab_description(nodes, "Assets/x.unity", {}, "", kind="scene")
    assert len(desc["shared"]) == 1 and len(desc["shared"][0]["props"]) == up.SHARE_MIN_PROPS
    for n in desc["nodes"]:
        fx, box = n["components"]
        assert fx["shared"] == 0 and fx["props"] == []
        assert box["shared"] == -1 and len(box["props"]) == 1
    cs_comp = re.search(r"public class Comp\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(desc["nodes"][0]["components"][0]) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_comp))
    cs_list = re.search(r"public class PropList\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(desc["shared"][0]) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_list))


def test_copy_streaming_assets(tmp_path):
    sa = tmp_path / "game" / "Game_Data" / "StreamingAssets"
    (sa / "Config").mkdir(parents=True)
    (sa / "aa" / "StandaloneWindows64").mkdir(parents=True)
    (sa / "Config" / "settings.json").write_text("{}")
    (sa / "intro.mp4").write_bytes(b"x" * 10)
    (sa / "aa" / "catalog.json").write_text("{}")
    (sa / "aa" / "StandaloneWindows64" / "a.bundle").write_bytes(b"b")
    assets = tmp_path / "proj" / "Assets"
    assert up.copy_streaming_assets(str(tmp_path / "game"), str(assets)) == (2, 12)
    out = assets / "StreamingAssets"
    assert (out / "Config" / "settings.json").read_text() == "{}"
    assert (out / "intro.mp4").stat().st_size == 10
    assert not (out / "aa").exists()  # Addressables build output stays behind
    assert up.copy_streaming_assets(str(tmp_path / "game"), str(assets)) == (0, 0)  # unchanged: not copied again
    # The game path can be the *_Data folder itself; no StreamingAssets at all copies nothing.
    assert up.streaming_assets_dirs(str(tmp_path / "game" / "Game_Data")) == [str(sa)]
    assert up.copy_streaming_assets(str(tmp_path / "proj"), str(assets)) == (0, 0)


def test_cubemap_meta():
    text = up.meta_text("a" * 32, 0, cube=True)
    assert "textureShape: 2" in text and "generateCubemap: 6" in text and "serializedVersion: 4" in text
    assert "textureShape" not in up.meta_text("a" * 32, 0)


class LitSession(SceneSession):
    """The scene with baked lighting: Wall and Floor in different lightmaps, a skybox and fog."""

    def mesh(self, a):
        md = super().mesh(a)
        if a.uid == "combined":
            md.uvs = {"UV0": np.zeros((9, 2), np.float32), "UV1": np.full((9, 2), 1.0, np.float32)}
        return md

    def hierarchy(self, asset):
        nodes = super().hierarchy(asset)
        nodes[1]["batch"]["lightmap"] = [0, 0.5, 0.5, 0.0, 0.0]
        nodes[2]["batch"]["lightmap"] = [1, 0.25, 0.25, 0.5, 0.5]
        return nodes

    def scene_lightmaps(self, asset):
        return [(Asset("texture", "Lightmap-0_comp_light", "lm0", uid="lm0"), "dldr"), (None, "")]

    def image(self, asset):
        return Image.new("RGBA", (4, 4), (128, 64, 0, 255))

    def color_space(self):
        return "gamma"

    def scene_managers(self, asset):
        return [{"type": "RenderSettings", "props": [{"p": "m_Fog", "t": "b", "v": 1},
                                                     {"p": "m_Sun", "t": "ref", "node": 4, "cls": "Light"}]}]


def test_export_scene_lightmaps_and_render_settings(tmp_path):
    from uniview.unity_builder import BUILDER_CS
    import re
    combined = Asset("model", "Combined Mesh (root: scene) 1", "cm", uid="combined", source="level0")
    scene = Asset("scene", "Scene: Main", ("scene", 1), uid="scene:level0", source="level0")
    root = tmp_path / "proj"
    _w, failed, _s = up.export_unity_project(LitSession([combined, scene]), str(root), "6000.5.4f1",
                                             editor_exe=fake_editor(tmp_path))
    assert failed == 0
    desc = json.load(open(root / "Assets" / "UniView" / "Build" / "Scenes" / "Main.unity.json"))
    assert desc["managers"] == [{"type": "RenderSettings", "props": [{"p": "m_Fog", "t": "b", "v": 1},
                                                                     {"p": "m_Sun", "t": "n", "n": 4, "s": "Light"}]}]
    assert desc["lightmaps"] == ["Assets/Scenes/Main_Lightmaps/Lightmap-0_comp_light.exr", ""]
    exr = root / desc["lightmaps"][0]
    assert exr.exists() and "textureType: 6" in open(str(exr) + ".meta").read()
    by_lightmap = {b["lightmap"]: b for b in desc["batches"]}
    assert set(by_lightmap) == {0, 1}  # one GLB per lightmap the batch's parts are baked into
    for index, expected in ((0, [0.5, 1 - 0.5]), (1, [0.75, 1 - 0.75])):  # uv * tiling + offset, V flipped
        gltf, buf = parse_glb(root / by_lightmap[index]["model"])
        prim = gltf["meshes"][0]["primitives"][0]
        used = sorted(set(np.asarray(accessor(gltf, buf, prim["indices"])).ravel().tolist()))
        np.testing.assert_allclose(accessor(gltf, buf, prim["attributes"]["TEXCOORD_1"])[used], [expected] * len(used))
    # Every key written exists in the C# classes (JsonUtility ignores unknown ones).
    fields = {cls: set(re.findall(r"public \w+(?:\[\])? (\w+)",
                                  re.search(r"public class " + cls + r"\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)))
              for cls in ("Description", "BatchRef", "Manager")}
    assert set(desc) <= fields["Description"] and set(desc["batches"][0]) <= fields["BatchRef"]
    assert set(desc["managers"][0]) <= fields["Manager"]


def test_lightmap_linear():
    px = np.array([[[0.5, 0.25, 0.0, 0.2]]], np.float32)
    np.testing.assert_allclose(up.lightmap_linear(px, "dldr", False), [[[1.0, 0.5 ** 2.2, 0.0]]], rtol=1e-5)
    np.testing.assert_allclose(up.lightmap_linear(px, "rgbm", False), [[[0.5 ** 2.2, 0.25 ** 2.2, 0.0]]], rtol=1e-5)
    np.testing.assert_allclose(up.lightmap_linear(px, "rgbm", True), [[[0.5, 0.25, 0.0]]], rtol=1e-5)
    np.testing.assert_allclose(up.lightmap_linear(px, "hdr", True), [[[0.5, 0.25, 0.0]]])


def test_baked_probe_becomes_custom():
    from engines.unity_components import baked_probe_as_custom
    props = [{"p": "m_Mode", "t": "i", "v": 0}, {"p": "m_BakedTexture", "t": "ref", "asset": "cube", "kind": "file"},
             {"p": "m_Intensity", "t": "f", "v": 1.0}]
    out = baked_probe_as_custom(props)
    assert {"p": "m_Mode", "t": "i", "v": 2} in out and not any(e["p"] == "m_BakedTexture" for e in out)
    assert {"p": "m_CustomBakedTexture", "t": "ref", "asset": "cube", "kind": "file"} in out
    realtime = [{"p": "m_Mode", "t": "i", "v": 1}] + props[1:]
    assert baked_probe_as_custom(realtime) == realtime


# ---------------------------------------------------------------------------- render pipeline

class PipelineSession:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def render_pipeline(self):
        return self.pipeline


URP = {"kind": "urp", "class": "UnityEngine.Rendering.Universal.UniversalRenderPipelineAsset",
       "name": "Sky:High", "props": [{"p": "m_ShadowDistance", "t": "f", "v": 160.0}]}


def test_project_pipeline_needs_urp_and_its_package():
    assert up.project_pipeline(PipelineSession(URP), {up.URP_PACKAGE: "17.0.0"}) is URP
    assert up.project_pipeline(PipelineSession(URP), {}) is None
    assert up.project_pipeline(PipelineSession({**URP, "kind": "hdrp"}), {up.URP_PACKAGE: "1"}) is None
    hdrp = {**URP, "kind": "hdrp"}
    assert up.project_pipeline(PipelineSession(hdrp), {up.PIPELINE_PACKAGES["hdrp"]: "17.0.0"}) is hdrp
    assert up.project_pipeline(PipelineSession(None), {up.URP_PACKAGE: "1"}) is None
    assert up.project_pipeline(object(), {up.URP_PACKAGE: "1"}) is None


def test_pipeline_description(tmp_path):
    import re
    from uniview.unity_builder import BUILDER_CS
    assets = tmp_path / "Assets"
    swaps = [{"path": "Assets/Materials/Rock.mat", "shader": "Universal Render Pipeline/Lit", "keywords": ["_NORMALMAP"],
              "queue": -1, "lit": True, "litKeywords": ["_NORMALMAP"], "litQueue": -1}]
    assert up.write_pipeline_description(str(tmp_path), str(assets), URP, swaps)
    path = assets / "UniView" / "Build" / "RenderPipeline.json"
    desc = json.loads(path.read_text(encoding="utf-8"))
    assert desc["kind"] == "pipeline" and desc["pipeline"] == "urp" and desc["materials"] == swaps
    assert desc["target"].startswith("Assets/Settings/Sky") and desc["target"].endswith(".asset")
    assert ":" not in desc["target"] and desc["props"] == URP["props"] and len(desc["stamp"]) == 40
    assert os.path.isfile(str(path) + ".meta")
    cs_desc = re.search(r"public class Description\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(desc) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_desc))
    cs_swap = re.search(r"public class MatSwap\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    assert set(swaps[0]) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_swap))
    up.write_pipeline_description(str(tmp_path), str(assets), URP, [])
    assert json.loads(path.read_text(encoding="utf-8"))["stamp"] != desc["stamp"]


# ---------------------------------------------------------------------------- Addressables

def test_addressable_entries_pick_the_main_asset():
    from types import SimpleNamespace as NS
    assets = [NS(uid="mesh", name="Ship", kind="model", path="assets/ships/ship.prefab"),
              NS(uid="pf", name="Prefab: Ship", kind="prefab", path="assets/ships/ship.prefab"),
              NS(uid="tex", name="hull", kind="texture", path="assets/art/hull.png"),
              NS(uid="gone", name="x", kind="texture", path="assets/art/x.png")]
    exported = {"mesh": "Assets/Ships/Ship/Ship.glb", "pf": "Assets/Ships/Ship.prefab", "tex": "Assets/Art/hull.png"}
    entries = [{"path": "Assets/Ships/Ship.prefab", "address": "Ship", "labels": ["Map"], "group": "maps"},
               {"path": "Assets/Art/hull.png", "address": "Assets/Art/hull.png", "labels": [], "group": ""},
               {"path": "Assets/Art/x.png", "address": "x", "labels": [], "group": ""}]
    out = up.addressable_entries(entries, assets, exported)
    assert out == [{"asset": "Assets/Ships/Ship.prefab", "address": "Ship", "labels": ["Map"], "group": "maps"},
                   {"asset": "Assets/Art/hull.png", "address": "Assets/Art/hull.png", "labels": [], "group": ""}]


def test_addressables_description(tmp_path):
    import re
    from types import SimpleNamespace as NS
    from uniview.unity_builder import ADDRESSABLES_ASMDEF, ADDRESSABLES_CS
    session = NS(assets=[NS(uid="pf", name="Ship", kind="prefab", path="Assets/Ships/Ship.prefab")],
                 addressables=lambda: [{"path": "Assets/Ships/Ship.prefab", "address": "Ship", "labels": ["Map"],
                                        "group": "maps", "guid": ""}])
    assets = tmp_path / "Assets"
    assert up.write_addressables_description(session, str(tmp_path), str(assets), {"pf": "Assets/Ships/Ship.prefab"}) == 1
    desc = json.loads((assets / "UniView" / "Build" / "Addressables.json").read_text(encoding="utf-8"))
    assert desc["kind"] == "addressables" and len(desc["stamp"]) == 40 and desc["entries"][0]["address"] == "Ship"
    cs_entry = re.search(r"public class AddressableEntry\s*\{(.*?)\}", ADDRESSABLES_CS, re.S).group(1)
    assert set(desc["entries"][0]) <= set(re.findall(r"public \w+(?:\[\])? (\w+)", cs_entry))
    asmdef = json.loads(ADDRESSABLES_ASMDEF)
    assert asmdef["defineConstraints"] == [asmdef["versionDefines"][0]["define"]]
    assert "#if " + asmdef["defineConstraints"][0] in ADDRESSABLES_CS
    folder = assets / "UniView" / "Addressables"
    assert (folder / "UniViewAddressables.cs").is_file() and (folder / "UniView.Addressables.Editor.asmdef.meta").is_file()
    assert up.write_addressables_description(session, str(tmp_path), str(assets), {}) == 0
