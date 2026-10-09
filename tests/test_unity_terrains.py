import re
import struct

import numpy as np

from engines.unity_terrain import _blank_layer, _details
from uniview.unity_builder import BUILDER_CS
from uniview.unity_terrains import data_key, terrain_description


def cs_fields(cls):
    body = re.search(r"public class " + cls + r"\s*\{(.*?)\}", BUILDER_CS, re.S).group(1)
    return set(re.findall(r"public \w+(?:\[\])? (\w+)", body))


def test_details_patch_layout():
    # 2 x 2 patches of 2 x 2 samples; patch 1 (x = 1, z = 0) uses layers 1 then 0.
    patches = [{"layerIndices": [], "numberOfObjects": []} for _ in range(4)]
    patches[1] = {"layerIndices": [1, 0], "numberOfObjects": [1, 2, 3, 4, 5, 6, 7, 8]}
    out = _details({"m_PatchCount": 2, "m_PatchSamples": 2, "m_Patches": patches}, 2)
    assert out.shape == (2, 4, 4)
    assert out[1, 0:2, 2:4].tolist() == [[1, 2], [3, 4]]  # row = z, columns = x
    assert out[0, 0:2, 2:4].tolist() == [[5, 6], [7, 8]]
    assert out[:, 2:, :].sum() == 0 and out[:, :, :2].sum() == 0
    assert _details({"m_PatchCount": 2, "m_PatchSamples": 2, "m_Patches": patches[:3]}, 2) is None


def terrain_data():
    layer = {**_blank_layer("terrainlayer:a:1", "Grass"), "diffuse": "tex:grass", "normal": "tex:missing"}
    return {
        "name": "Main", "resolution": 3, "size": [10.0, 5.0, 10.0],
        "heights": np.arange(9, dtype=np.uint16).reshape(3, 3), "holes": np.array([[1, 0], [1, 1]], bool),
        "alphamaps": np.full((1, 4, 4), 255, np.uint8), "baseMapResolution": 512, "layers": [layer],
        "trees": [{"prefab": "go:a:5", "bendFactor": 0.0, "navMeshLod": 0},
                  {"prefab": "go:a:6", "bendFactor": 0.0, "navMeshLod": 0}],
        "tree_instances": [(0.5, 0.1, 0.25, 1.0, 1.0, 3.0, 0xFF0000FF, 0xFFFFFFFF, 1)],
        "details": [{"prefab": "", "texture": "tex:grass", "renderMode": 2, "usePrototypeMesh": False}],
        "detail_map": np.ones((1, 8, 8), np.uint8), "detailPatch": 8, "coverage": True,
        "grassTint": [1.0] * 4, "grass": [0.5, 0.5, 0.5]}


def test_terrain_description(tmp_path):
    layer_paths = {}
    desc, files = terrain_description(
        terrain_data(), str(tmp_path / "Assets" / "Main.asset"), layer_paths,
        {"tex:grass": str(tmp_path / "Assets" / "grass.png")},
        {"go:a:5": ("Assets/Tree.prefab", ""), "go:a:6": ("Assets/Big.prefab", "Branch")},
        str(tmp_path / "Assets" / "raw" / "Main"), lambda p: "U:" + p.replace("\\", "/").split("/Assets/")[-1])
    t = desc["terrain"]
    assert desc["kind"] == "terrain" and desc["target"] == "U:Main.asset"
    assert set(desc) <= cs_fields("Description")
    assert set(t) <= cs_fields("TerrainInfo")
    assert set(t["layers"][0]) <= cs_fields("TLayer")
    assert set(t["trees"][0]) <= cs_fields("TTree")
    assert set(t["details"][0]) <= cs_fields("TDetail")
    assert t["layers"][0]["diffuse"] == "U:grass.png" and t["layers"][0]["normal"] == ""
    assert t["layers"][0]["target"] == "U:Terrain Layers/Grass.terrainlayer"
    assert [x["prefab"] for x in t["trees"]] == ["Assets/Tree.prefab", ""]  # only a prefab root can be a tree
    assert t["details"][0]["texture"] == "U:grass.png"
    assert t["alphamapResolution"] == 4 and t["detailResolution"] == 8 and t["treeCount"] == 1
    blobs = {p.replace("\\", "/").rsplit(".", 2)[-2]: b for p, b in files.items()}
    assert np.frombuffer(blobs["heights"], "<u2").tolist() == list(range(9))
    assert list(blobs["holes"]) == [1, 0, 1, 1]
    assert len(blobs["trees"]) == 36 and struct.unpack("<6fIIi", blobs["trees"])[-1] == 1
    assert t["heights"].endswith("Main.heights.bytes") and t["treeInstances"].endswith("Main.trees.bytes")
    # A second terrain using the same layer shares its .terrainlayer.
    desc2, _ = terrain_description(terrain_data(), str(tmp_path / "Assets" / "Other" / "B.asset"), layer_paths,
                                   {}, {}, str(tmp_path / "raw" / "B"), lambda p: p)
    assert desc2["terrain"]["layers"][0]["target"].replace("\\", "/").endswith("/Assets/Terrain Layers/Grass.terrainlayer")


def test_data_key():
    assert data_key("terrain:sharedassets1.assets:42") == "terraindata:sharedassets1.assets:42"
    assert data_key("mesh:x") == ""
