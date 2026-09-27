"""Quaternion / matrix helpers and skeleton export used by animated GLB export."""

import struct
import zlib

import numpy as np
import pytest

from engines import unity_anim, unity_humanoid as hum, unity_skin as skin

rng = np.random.default_rng(1234)


def random_quats(n):
    q = rng.normal(size=(n, 4))
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def axis_angle(axis, deg):
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    h = np.radians(deg) / 2
    return np.array([*(axis * np.sin(h)), np.cos(h)])


def rot_x(deg):
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(deg):
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_z(deg):
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def same_rotation(a, b):
    """Quaternions equal up to sign."""
    return min(np.abs(a - b).max(), np.abs(a + b).max()) < 1e-6


# ---------------------------------------------------------------------------- quaternions

def test_quat_to_mat_is_rotation():
    m = skin.quat_to_mat(random_quats(50))
    np.testing.assert_allclose(m @ m.transpose(0, 2, 1), np.broadcast_to(np.eye(3), m.shape), atol=1e-9)
    np.testing.assert_allclose(np.linalg.det(m), 1.0, atol=1e-9)


def test_quat_to_mat_known_axes():
    np.testing.assert_allclose(skin.quat_to_mat(axis_angle([0, 0, 1], 90)), rot_z(90), atol=1e-12)
    np.testing.assert_allclose(skin.quat_to_mat(axis_angle([1, 0, 0], 30)), rot_x(30), atol=1e-12)
    np.testing.assert_allclose(skin.quat_to_mat(axis_angle([0, 1, 0], -45)), rot_y(-45), atol=1e-12)


def test_quat_to_mat_normalizes_input():
    q = axis_angle([0, 0, 1], 90)
    np.testing.assert_allclose(skin.quat_to_mat(q * 3.0), skin.quat_to_mat(q), atol=1e-12)


def test_qmul_matches_matrix_product_and_both_impls_agree():
    a, b = random_quats(20), random_quats(20)
    ab = hum.qmul(a, b)
    np.testing.assert_allclose(skin.quat_to_mat(ab), skin.quat_to_mat(a) @ skin.quat_to_mat(b), atol=1e-9)
    for i in range(20):
        np.testing.assert_allclose(skin._qmul(a[i], b[i]), ab[i], atol=1e-12)


def test_qrot_and_qconj():
    q, v = random_quats(20), rng.normal(size=(20, 3))
    expected = np.einsum("nij,nj->ni", skin.quat_to_mat(q), v)
    np.testing.assert_allclose(hum.qrot(q, v), expected, atol=1e-9)
    np.testing.assert_allclose(hum.qrot(hum.qconj(q), hum.qrot(q, v)), v, atol=1e-9)


def test_mat_quat_roundtrip_all_branches():
    qs = list(random_quats(200)) + [axis_angle([1, 0, 0], 179), axis_angle([0, 1, 0], 179),
                                     axis_angle([0, 0, 1], 179), np.array([0, 0, 0, 1.0])]
    for q in qs:  # includes negative-trace matrices (the i/j/k branches)
        back = hum._mat_quat(skin.quat_to_mat(q))
        assert same_rotation(back / np.linalg.norm(back), q)


def test_euler_to_quat_is_unity_zxy_order():
    for deg in rng.uniform(-180, 180, size=(30, 3)):
        m = skin.quat_to_mat(skin.euler_to_quat(deg))
        np.testing.assert_allclose(m, rot_y(deg[1]) @ rot_x(deg[0]) @ rot_z(deg[2]), atol=1e-9)


def test_trs_composition():
    pos, rot, scale = [1, 2, 3], axis_angle([0, 1, 0], 90), [2, 3, 4]
    m = skin.trs(pos, rot, scale)
    p = np.array([1.0, 1.0, 1.0, 1.0])
    expected = rot_y(90) @ (np.array(scale) * p[:3]) + pos
    np.testing.assert_allclose((m @ p)[:3], expected, atol=1e-12)


def test_write_glb_rest_matrix_matches_quat_to_mat():
    """write_glb's _add_skeleton builds rest matrices with its own copy of the formula."""
    import unity_viewer as uv
    q = axis_angle([1, 2, 3], 70)
    rig = {"joints": [{"name": "j", "parent": -1, "translation": [0, 0, 0], "rotation": list(q),
                       "scale": [1, 1, 1]}], "skin_joints": [], "inverse_bind": []}
    captured = {}

    def add_accessor(arr, *a, **k):
        captured["ibm"] = arr
        return 0

    gltf = {"nodes": [{}], "scenes": [{"nodes": [0]}]}
    uv._add_skeleton(gltf, rig, None, add_accessor)
    world = np.linalg.inv(captured["ibm"][0].T)
    np.testing.assert_allclose(world[:3, :3], skin.quat_to_mat(q), atol=1e-6)


# ---------------------------------------------------------------------------- sampling

def test_sample_clamps_and_interpolates():
    keys = np.array([[0.0, 10.0], [1.0, 20.0], [3.0, 0.0]])
    assert skin._sample(keys, -1) == 10.0
    assert skin._sample(keys, 5) == 0.0
    assert skin._sample(keys, 0.5) == pytest.approx(15.0)
    assert skin._sample(keys, 2.0) == pytest.approx(10.0)
    assert skin._sample(None, 1) is None
    assert skin._sample(np.zeros((0, 2)), 1) is None


def test_continuous_keeps_one_hemisphere():
    q = random_quats(40)
    q[1::2] *= -1
    c = hum._continuous(q)
    assert all(np.dot(c[i], c[i - 1]) >= 0 for i in range(1, len(c)))
    for a, b in zip(q, c):
        assert same_rotation(a, b)


def test_sample_rotation_handles_sign_flipped_keys():
    a, b = axis_angle([0, 0, 1], 10), -axis_angle([0, 0, 1], 30)  # b stored as -q
    values = {i: np.array([[0.0, a[i]], [1.0, b[i]]]) for i in range(4)}
    out = hum.sample_rotation(values, 0, np.array([0.0, 0.5, 1.0]))
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-9)
    assert same_rotation(out[1], axis_angle([0, 0, 1], 20))  # halfway, not through the long way round


def test_sample_rotation_missing_curves_default_identity():
    values = {1: np.array([[0.0, 0.0], [1.0, 0.0]])}
    out = hum.sample_rotation(values, 0, np.array([0.0, 1.0]))
    np.testing.assert_allclose(out, [[0, 0, 0, 1]] * 2, atol=1e-12)


# ---------------------------------------------------------------------------- mirroring / rig export

def test_mirror_trs_matches_mirrored_matrix():
    """Converting Unity's left-handed TRS by flipping X must equal M @ trs @ M."""
    for _ in range(20):
        pos, rot, scale = rng.normal(size=3), random_quats(1)[0], rng.uniform(0.5, 2, size=3)
        mirrored = skin._MIRROR @ skin.trs(pos, rot, scale) @ skin._MIRROR
        np.testing.assert_allclose(skin.trs(*skin._mirror_trs(pos, rot, scale)), mirrored, atol=1e-9)


def make_nodes():
    ident = {"rot": [0, 0, 0, 1], "scale": [1, 1, 1]}
    return {
        "char": {"name": "char", "parent": None, "pos": [5, 0, 0], **ident},
        "hips": {"name": "hips", "parent": "char", "pos": [0, 1, 0], **ident},
        "spine": {"name": "spine", "parent": "hips", "pos": [1, 1, 0], **ident},
        "leg": {"name": "leg", "parent": "hips", "pos": [0, -1, 0], **ident},
        "mesh": {"name": "mesh", "parent": "char", "pos": [0, 0, 0], **ident},
    }


def test_export_rig_structure_and_mirroring():
    nodes = make_nodes()
    bones = ["spine", "leg"]
    rig, joint_of = skin.export_rig(nodes, bones, [np.eye(4)] * 2, [[0], [1], [0]], [[1], [1], [1]], "mesh")
    names = [j["name"] for j in rig["joints"]]
    assert names[0] == "char" and set(names) == {"char", "hips", "spine", "leg", "mesh"}
    root = rig["joints"][0]
    assert root["parent"] == -1 and root["translation"] == [0.0, 0.0, 0.0]  # common ancestor at the origin
    for j in rig["joints"][1:]:
        assert j["parent"] >= 0 and rig["joints"][j["parent"]]["name"] == nodes[j["name"]]["parent"]
    spine = rig["joints"][joint_of["spine"]]
    assert spine["translation"] == [-1.0, 1.0, 0.0]  # X mirrored
    assert rig["skin_joints"] == [joint_of["spine"], joint_of["leg"]]


def test_export_rig_weights_padded_and_normalized():
    nodes = make_nodes()
    rig, _ = skin.export_rig(nodes, ["spine", "leg"], [np.eye(4)] * 2,
                             [[0, 1], [1, 0], [0, 0]], [[3, 1], [0, 0], [0.5, 0.5]], "mesh")
    w, j = rig["weights_0"], rig["joints_0"]
    assert w.shape == (3, 4) and j.shape == (3, 4) and w.dtype == np.float32 and j.dtype == np.uint16
    np.testing.assert_allclose(w[0], [0.75, 0.25, 0, 0])
    np.testing.assert_allclose(w[1], [1, 0, 0, 0])  # no weights -> all on the first bone
    np.testing.assert_allclose(w.sum(axis=1), 1.0, atol=1e-6)


def test_export_rig_keeps_four_strongest():
    nodes = make_nodes()
    idx = [[0, 1, 0, 1, 0, 1]]
    wts = [[0.05, 0.4, 0.1, 0.3, 0.02, 0.13]]
    rig, _ = skin.export_rig(nodes, ["spine", "leg"], [np.eye(4)] * 2, idx, wts, "mesh")
    kept = np.array([0.4, 0.3, 0.13, 0.1])
    np.testing.assert_allclose(rig["weights_0"][0], kept / kept.sum(), atol=1e-6)
    np.testing.assert_array_equal(rig["joints_0"][0], [1, 1, 1, 0])


def test_export_rig_clamps_bad_bone_indices():
    rig, _ = skin.export_rig(make_nodes(), ["spine", "leg"], [np.eye(4)] * 2, [[-3], [99]], [[1], [1]], "mesh")
    assert rig["joints_0"].max() <= 1 and rig["joints_0"].min() >= 0


def test_export_rig_mirrors_bind_poses():
    bind = np.eye(4)
    bind[:3, 3] = [2, 0, 0]
    rig, _ = skin.export_rig(make_nodes(), ["spine"], [bind], [[0]], [[1]], "mesh")
    np.testing.assert_allclose(rig["inverse_bind"][0][:3, 3], [-2, 0, 0])


def test_export_rig_without_bones_raises():
    with pytest.raises(ValueError):
        skin.export_rig({}, ["missing"], [np.eye(4)], [[0]], [[1]], "nope")


# ---------------------------------------------------------------------------- clip decoding

def test_path_hash_is_crc32():
    assert unity_anim.path_hash("Hips/Spine") == zlib.crc32(b"Hips/Spine")
    assert unity_anim.path_hash("") == 0


def test_streamed_frames():
    raw = struct.pack("<fi", 0.0, 2) + struct.pack("<i4f", 0, 0, 0, 0, 1.5) + struct.pack("<i4f", 3, 0, 0, 0, -2.0)
    raw += struct.pack("<fi", 0.25, 1) + struct.pack("<i4f", 1, 0, 0, 0, 7.0)
    words = np.frombuffer(raw, np.uint32)
    assert unity_anim._streamed_frames(words) == [(0.0, [(0, 1.5), (3, -2.0)]), (0.25, [(1, 7.0)])]
