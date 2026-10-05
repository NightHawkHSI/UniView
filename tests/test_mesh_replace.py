"""Mod Maker mesh replacement: reading OBJ/glTF, and writing a model into a Unity Mesh's vertex layout."""

import numpy as np

from engines.sdk import MeshData
from uniview import meshimport
from uniview.export import write_glb
from uniview.unity_mesh_write import _decode, _layout, _rows, compute_tangents, replace_mesh

QUAD = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.float32)


def test_obj_polygons_negative_indices_and_materials(tmp_path):
    path = tmp_path / "m.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nvn 0 0 1\n"
                    "usemtl A\nf 1/1/1 2/2/1 3/3/1 4/4/1\nusemtl B\nf -4/-4/-1 -2/-2/-1 -1/-1/-1\n")
    m = meshimport.load(str(path))
    assert m.submesh_names == ["A", "B"]
    assert [len(s) for s in m.submeshes] == [2, 1]   # the quad is fanned into two triangles
    assert len(m.points) == 4                          # B reuses A's corners (same v/vt/vn)
    assert m.uvs["UV0"][2].tolist() == [1, 1]
    assert m.normals is not None


def test_glb_from_our_export_reads_back_the_same(tmp_path):
    md = MeshData(QUAD, [np.array([[0, 1, 2]]), np.array([[0, 2, 3]])], normals=np.tile([0, 0, 1], (4, 1)),
                  uvs={"UV0": np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)})
    path = str(tmp_path / "m.glb")
    write_glb(None, md, [], path)
    m = meshimport.load(path)
    assert np.allclose(m.points, QUAD)
    assert [s.tolist() for s in m.submeshes] == [[[0, 1, 2]], [[0, 2, 3]]]  # both parts share one vertex list
    assert np.allclose(m.uvs["UV0"], md.uvs["UV0"])  # glTF's flipped V is undone


def _mesh_tree(points, weights):
    """A Unity 2019-style Mesh typetree: stream 0 position+normal (float), stream 1 UV0 (half) + UV1,
    stream 2 bone weights (float x4) + indices (uint32 x4)."""
    n = len(points)
    s0 = np.hstack([points, np.tile([0, 0, 1], (n, 1))]).astype("<f4").tobytes()
    s1 = np.hstack([np.zeros((n, 2), "<f2").view("u1"),  # UV0 half x2 = 4 bytes
                    np.full((n, 2), 0.5, "<f4").view("u1")]).tobytes()  # UV1 float x2 (lightmap)
    s2 = np.hstack([weights.astype("<f4").view("u1"), np.tile(np.arange(4, dtype="<u4"), (n, 1)).view("u1")]).tobytes()

    def pad(b):
        return b + bytes(-len(b) % 16)
    channels = [{"stream": 0, "offset": 0, "format": 0, "dimension": 3},
                {"stream": 0, "offset": 12, "format": 0, "dimension": 3}] + \
               [{"stream": 0, "offset": 0, "format": 0, "dimension": 0}] * 2 + \
               [{"stream": 1, "offset": 0, "format": 1, "dimension": 2},
                {"stream": 1, "offset": 4, "format": 0, "dimension": 2}] + \
               [{"stream": 0, "offset": 0, "format": 0, "dimension": 0}] * 6 + \
               [{"stream": 2, "offset": 0, "format": 0, "dimension": 4},
                {"stream": 2, "offset": 16, "format": 10, "dimension": 4}]
    sub = {"firstByte": 0, "indexCount": 3, "topology": 0, "baseVertex": 0, "firstVertex": 0, "vertexCount": n,
           "localAABB": {}}
    return {"m_Name": "m", "m_SubMeshes": [sub, dict(sub)], "m_Shapes": {"shapes": []}, "m_BindPose": [],
            "m_MeshCompression": 0, "m_IndexFormat": 0, "m_IndexBuffer": b"",
            "m_VertexData": {"m_VertexCount": n, "m_Channels": channels, "m_DataSize": pad(s0) + pad(s1) + s2},
            "m_LocalAABB": {}, "m_StreamData": {"offset": 0, "size": 0, "path": ""}}


def _read(tree, meaning):
    vd = tree["m_VertexData"]
    chans, streams, _ = _layout(vd["m_Channels"], (2019, 4), vd["m_VertexCount"])
    ch = next(c for c in chans if c[1] == meaning)
    return _decode(_rows(vd["m_DataSize"], vd["m_VertexCount"], streams[ch[5]], ch), ch)


def test_model_goes_into_the_games_vertex_layout():
    original = np.array([[0, 0, 0], [-1, 0, 0], [-1, 1, 0]], np.float64)  # Unity space
    weights = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], np.float64)
    tree = _mesh_tree(original, weights)
    model = meshimport.ImportedMesh(QUAD * 1.01, [[[0, 1, 2], [0, 2, 3]]],
                                    uvs={"UV0": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    notes = replace_mesh(tree, (2019, 4), model, tree["m_VertexData"]["m_DataSize"])
    vd = tree["m_VertexData"]
    assert vd["m_VertexCount"] == 4
    pos = _read(tree, "position")
    assert np.allclose(pos, QUAD * 1.01 * [-1, 1, 1], atol=1e-6)        # x mirrored into Unity space
    assert np.allclose(_read(tree, "normal"), [[0, 0, 1]] * 4)         # computed (file +z); mirroring x keeps it
    assert np.allclose(_read(tree, "uv0"), model.uvs["UV0"])
    assert np.allclose(_read(tree, "uv1"), 0.5)                        # lightmap UVs copied over
    assert np.allclose(_read(tree, "weights")[:3], weights)            # each corner takes its nearest weights
    assert _read(tree, "weights")[3].tolist() == [0, 0, 1, 0]          # (-0, 1.01, 0) is nearest to vertex 2
    indices = np.frombuffer(tree["m_IndexBuffer"], "<u2").reshape(-1, 3)
    assert indices.tolist() == [[2, 1, 0], [3, 2, 0]]                  # winding flipped
    assert [s["indexCount"] for s in tree["m_SubMeshes"]] == [6, 0]    # second material left empty
    assert tree["m_LocalAABB"]["m_Extent"]["x"] == np.float32(0.505)
    assert any("empty" in n for n in notes) and any("weights" in n for n in notes)


def test_tangents_follow_uv_u_direction():
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float64)
    normals = np.tile([0.0, 0.0, 1.0], (3, 1))
    uv = np.array([[0, 0], [1, 0], [0, 1]], np.float64)
    t = compute_tangents(pts, normals, uv, [np.array([[0, 1, 2]])])
    assert np.allclose(t[:, :3], [[1, 0, 0]] * 3) and np.all(t[:, 3] == 1)
    t = compute_tangents(pts, normals, uv * [1, -1], [np.array([[0, 1, 2]])])  # mirrored V: handedness flips
    assert np.all(t[:, 3] == -1)


def test_obj_groups_split_parts_that_share_a_material(tmp_path):
    path = tmp_path / "m.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\ng part0\nusemtl M\nf 1 2 3\ng part1\nusemtl M\nf 1 3 4\n")
    assert [len(s) for s in meshimport.load(str(path)).submeshes] == [1, 1]
