"""write_glb: parse the file back byte by byte and check the layout against the glTF 2.0 spec."""

import json
import logging
import struct
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import unity_viewer as uv
from engines.sdk import ALBEDO, NORMAL, Material, MeshData, TextureRef
from engines.unity_skin import quat_to_mat

COMPONENTS = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def parse_glb(path):
    """(gltf dict, BIN chunk bytes), asserting the container is well formed."""
    data = open(path, "rb").read()
    magic, version, length = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67 and version == 2
    assert length == len(data)
    js_len, js_type = struct.unpack_from("<II", data, 12)
    assert js_type == 0x4E4F534A and js_len % 4 == 0
    js = data[20:20 + js_len]
    assert js.rstrip(b" ") == js.rstrip()  # padded with spaces only
    gltf = json.loads(js)
    bin_off = 20 + js_len
    bin_len, bin_type = struct.unpack_from("<II", data, bin_off)
    assert bin_type == 0x004E4942 and bin_len % 4 == 0
    buf = data[bin_off + 8:bin_off + 8 + bin_len]
    assert bin_off + 8 + bin_len == len(data)
    assert gltf["buffers"] == [{"byteLength": bin_len}]
    for view in gltf["bufferViews"]:
        assert view["byteOffset"] % 4 == 0
        assert view["byteOffset"] + view["byteLength"] <= bin_len
    return gltf, buf


def accessor(gltf, buf, index):
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    dtype = COMPONENTS[acc["componentType"]]
    width = WIDTH[acc["type"]]
    n = acc["count"] * width
    assert n * np.dtype(dtype).itemsize <= view["byteLength"]
    arr = np.frombuffer(buf, dtype, n, view["byteOffset"])
    return arr.reshape(acc["count"], width) if width > 1 else arr


class FakeSession:
    def __init__(self, fail=()):
        self.lock = threading.Lock()
        self.fail = set(fail)
        self.calls = []

    def image(self, asset):
        self.calls.append(asset.key)
        if asset.key in self.fail:
            raise RuntimeError("can't decode")
        return Image.new("RGB", (4, 4), (255, 0, 0))


def tex(key, role=ALBEDO):
    return TextureRef("_MainTex", f"tex_{key}", SimpleNamespace(key=key, name=f"tex_{key}"), role)


def quad(**kw):
    points = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    return MeshData(points, [[[0, 1, 2]], [[0, 2, 3]]], **kw)


def test_positions_and_bounds(tmp_path):
    md = quad()
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, [], path)
    gltf, buf = parse_glb(path)
    prim = gltf["meshes"][0]["primitives"][0]
    pos_acc = prim["attributes"]["POSITION"]
    np.testing.assert_array_equal(accessor(gltf, buf, pos_acc), md.points)
    assert gltf["accessors"][pos_acc]["min"] == [0, 0, 0]
    assert gltf["accessors"][pos_acc]["max"] == [1, 1, 0]
    assert gltf["bufferViews"][gltf["accessors"][pos_acc]["bufferView"]]["target"] == 34962


def test_one_primitive_per_submesh_with_indices(tmp_path):
    md = quad()
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, [], path)
    gltf, buf = parse_glb(path)
    prims = gltf["meshes"][0]["primitives"]
    assert len(prims) == 2
    for prim, tris in zip(prims, md.submeshes):
        assert prim["mode"] == 4
        acc = gltf["accessors"][prim["indices"]]
        assert acc["componentType"] == 5125
        assert gltf["bufferViews"][acc["bufferView"]]["target"] == 34963
        np.testing.assert_array_equal(accessor(gltf, buf, prim["indices"]), tris.ravel())


def test_empty_submesh_skipped(tmp_path):
    md = quad()
    md.submeshes.append(np.zeros((0, 3), np.int64))
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, [], path)
    gltf, _ = parse_glb(path)
    assert len(gltf["meshes"][0]["primitives"]) == 2


def test_no_triangles_raises(tmp_path):
    md = quad()
    md.submeshes = [np.zeros((0, 3), np.int64)]
    with pytest.raises(ValueError):
        uv.write_glb(FakeSession(), md, [], str(tmp_path / "m.glb"))


def test_normals_uvs_colors(tmp_path):
    normals = [[0, 0, 2], [0, 0, 0], [0, 0, 1], [3, 0, 0]]  # unnormalized + one zero-length
    uvs = {"UV0": [[0, 0], [1, 0], [1, 1], [0.25, 0.75]], "UV1": [[9, 9]] * 4}
    colors = [[2, -1, 0.5, 1]] * 4
    md = quad(normals=normals, uvs=uvs, colors=colors)
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, [], path)
    gltf, buf = parse_glb(path)
    attrs = gltf["meshes"][0]["primitives"][0]["attributes"]
    n = accessor(gltf, buf, attrs["NORMAL"])
    np.testing.assert_allclose(n, [[0, 0, 1], [0, 1, 0], [0, 0, 1], [1, 0, 0]])
    t = accessor(gltf, buf, attrs["TEXCOORD_0"])
    np.testing.assert_allclose(t, [[0, 1], [1, 1], [1, 0], [0.25, 0.25]])  # V flipped, first set only
    assert "TEXCOORD_1" not in attrs
    np.testing.assert_allclose(accessor(gltf, buf, attrs["COLOR_0"]), [[1, 0, 0.5, 1]] * 4)
    np.testing.assert_allclose(md.uvs["UV0"][3], [0.25, 0.75])  # the source mesh isn't modified


def test_primitives_share_attributes(tmp_path):
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(), [], path)
    gltf, _ = parse_glb(path)
    a, b = gltf["meshes"][0]["primitives"]
    assert a["attributes"] == b["attributes"]


def test_materials_slots_and_color(tmp_path):
    mats = [Material("A", color=(0.5, 2.0, -1.0)), Material("B")]
    md = quad(material_slots=[1, 0])
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, mats, path)
    gltf, _ = parse_glb(path)
    assert [m["name"] for m in gltf["materials"]] == ["A", "B"]
    assert gltf["materials"][0]["pbrMetallicRoughness"]["baseColorFactor"] == [0.5, 1.0, 0.0, 1.0]
    assert "baseColorFactor" not in gltf["materials"][1]["pbrMetallicRoughness"]
    assert [p["material"] for p in gltf["meshes"][0]["primitives"]] == [1, 0]


def test_material_slot_out_of_range_uses_last(tmp_path):
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(material_slots=[0, 7]), [Material("A"), Material("B")], path)
    gltf, _ = parse_glb(path)
    assert [p["material"] for p in gltf["meshes"][0]["primitives"]] == [0, 1]


def test_texture_embedded_once_and_png(tmp_path):
    shared = tex("t1")
    mats = [Material("A", [tex("n", NORMAL), shared]), Material("B", [tex("t1")])]
    session = FakeSession()
    path = str(tmp_path / "m.glb")
    uv.write_glb(session, quad(), mats, path)
    gltf, buf = parse_glb(path)
    assert session.calls == ["t1"]  # deduplicated by asset key, normal map not used
    assert len(gltf["images"]) == 1 and len(gltf["textures"]) == 1
    for m in gltf["materials"]:
        assert m["pbrMetallicRoughness"]["baseColorTexture"] == {"index": 0}
    view = gltf["bufferViews"][gltf["images"][0]["bufferView"]]
    assert "target" not in view
    png = buf[view["byteOffset"]:view["byteOffset"] + view["byteLength"]]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert gltf["samplers"] and gltf["textures"][0]["sampler"] == 0


def test_texture_failure_still_writes_valid_file(tmp_path, caplog):
    mats = [Material("A", [tex("bad")]), Material("B", [tex("good")])]
    path = str(tmp_path / "m.glb")
    with caplog.at_level(logging.WARNING, logger="viewer"):
        uv.write_glb(FakeSession(fail={"bad"}), quad(), mats, path)
    gltf, _ = parse_glb(path)
    assert "baseColorTexture" not in gltf["materials"][0]["pbrMetallicRoughness"]
    assert gltf["materials"][1]["pbrMetallicRoughness"]["baseColorTexture"] == {"index": 0}
    assert "Could not embed a texture" in caplog.text


def test_trimesh_reads_it(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    md = quad(normals=[[0, 0, 1]] * 4, uvs={"UV0": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), md, [Material("A", [tex("t")])], path)
    scene = trimesh.load(path, force="scene")
    mesh = trimesh.util.concatenate(list(scene.geometry.values()))
    assert len(mesh.faces) == 2
    np.testing.assert_allclose(mesh.bounds, [[0, 0, 0], [1, 1, 0]])


# ---------------------------------------------------------------------------- skeleton / animation

def two_bone_rig():
    """Root at origin, child 1 unit up, rotated 90 degrees about Z."""
    s = np.sqrt(0.5)
    joints = [
        {"name": "root", "parent": -1, "translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0],
         "scale": [1.0, 1.0, 1.0]},
        {"name": "child", "parent": 0, "translation": [0.0, 1.0, 0.0], "rotation": [0.0, 0.0, s, s],
         "scale": [1.0, 1.0, 1.0]},
    ]
    return {"joints": joints, "skin_joints": [1], "inverse_bind": np.zeros((0, 4, 4)),
            "joints_0": np.zeros((4, 4), np.uint16),
            "weights_0": np.tile(np.array([1, 0, 0, 0], np.float32), (4, 1))}


def test_skeleton_nodes_skin_and_ibm(tmp_path):
    rig = two_bone_rig()
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(), [], path, rig=rig)
    gltf, buf = parse_glb(path)
    nodes = gltf["nodes"]
    assert nodes[0]["skin"] == 0 and nodes[1]["name"] == "root" and nodes[2]["name"] == "child"
    assert nodes[1]["children"] == [2]
    assert gltf["scenes"][0]["nodes"] == [0, 1]
    skin = gltf["skins"][0]
    assert skin["joints"] == [1, 2] and skin["skeleton"] == 1
    attrs = gltf["meshes"][0]["primitives"][0]["attributes"]
    assert gltf["accessors"][attrs["JOINTS_0"]]["componentType"] == 5123
    np.testing.assert_array_equal(accessor(gltf, buf, attrs["JOINTS_0"]), [[1, 1, 1, 1]] * 4)  # bone 0 -> joint 1
    ibm = accessor(gltf, buf, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)  # column-major
    world_child = np.eye(4)
    world_child[:3, :3] = quat_to_mat(np.array(rig["joints"][1]["rotation"]))
    world_child[:3, 3] = [0, 1, 0]
    np.testing.assert_allclose(ibm[0], np.eye(4), atol=1e-6)
    np.testing.assert_allclose(ibm[1] @ world_child, np.eye(4), atol=1e-6)


def test_skin_bone_inverse_bind_wins(tmp_path):
    rig = two_bone_rig()
    custom = np.diag([2.0, 2.0, 2.0, 1.0])
    custom[:3, 3] = [1, 2, 3]
    rig["inverse_bind"] = np.array([custom])
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(), [], path, rig=rig)
    gltf, buf = parse_glb(path)
    ibm = accessor(gltf, buf, gltf["skins"][0]["inverseBindMatrices"]).reshape(-1, 4, 4)
    np.testing.assert_allclose(ibm[1], custom.T)  # stored column-major: translation in the last row


def test_animation_channels(tmp_path):
    rig = two_bone_rig()
    s = np.sqrt(0.5)
    anim = {"name": "", "times": np.array([0.0, 0.5, 1.0], np.float32), "channels": [
        {"joint": 1, "path": "rotation", "values": np.array([[0, 0, 0, 1], [0, 0, s, s], [0, 0, 1, 0]], np.float32)},
        {"joint": 0, "path": "translation", "values": np.zeros((3, 3), np.float32)},
    ]}
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(), [], path, rig=rig, animation=anim)
    gltf, buf = parse_glb(path)
    a = gltf["animations"][0]
    assert a["name"] == "animation"
    assert [c["target"] for c in a["channels"]] == [{"node": 2, "path": "rotation"}, {"node": 1, "path": "translation"}]
    times_acc = gltf["accessors"][a["samplers"][0]["input"]]
    assert times_acc["min"] == [0.0] and times_acc["max"] == [1.0]
    assert gltf["accessors"][a["samplers"][0]["output"]]["type"] == "VEC4"
    assert gltf["accessors"][a["samplers"][1]["output"]]["type"] == "VEC3"
    np.testing.assert_allclose(accessor(gltf, buf, a["samplers"][0]["output"]), anim["channels"][0]["values"])


def test_no_animation_when_no_channels(tmp_path):
    path = str(tmp_path / "m.glb")
    uv.write_glb(FakeSession(), quad(), [], path, rig=two_bone_rig(),
                 animation={"name": "x", "times": [0.0], "channels": []})
    gltf, _ = parse_glb(path)
    assert "animations" not in gltf
