"""Gizmos: wire shapes for colliders, lights, cameras and sound sources in the 3D scene view."""

import numpy as np

from engines.unity_scene import (box_lines, capsule_lines, circle_lines, component_gizmo, cone_lines, frustum_lines,
                                 join, sphere_lines)


def test_box_has_8_corners_and_12_edges_of_the_right_size():
    pts, segs = box_lines((1, 0, 0), (2, 4, 6))
    assert len(pts) == 8 and len(segs) == 12
    assert np.allclose(pts.min(0), [0, -2, -3]) and np.allclose(pts.max(0), [2, 2, 3])
    lengths = sorted(np.linalg.norm(pts[a] - pts[b]) for a, b in segs)
    assert np.allclose(lengths, [2] * 4 + [4] * 4 + [6] * 4)


def test_circles_spheres_and_capsules():
    pts, segs = circle_lines((0, 0, 0), 2.0, axis=1, n=16)
    assert np.allclose(np.linalg.norm(pts, axis=1), 2.0) and np.allclose(pts[:, 1], 0) and len(segs) == 16
    pts, segs = sphere_lines((0, 0, 0), 1.0)
    assert len(segs) == 3 * 32 and segs.max() < len(pts)
    pts, _ = capsule_lines((0, 0, 0), 0.5, 3.0, direction=1)
    assert np.isclose(pts[:, 1].max(), 1.5) and np.isclose(np.abs(pts[:, 0]).max(), 0.5)


def test_cone_and_frustum_extents():
    pts, _ = cone_lines(10.0, 90.0)
    assert np.isclose(pts[:, 2].max(), 10.0) and np.isclose(np.abs(pts[:, 0]).max(), 10.0)
    pts, segs = frustum_lines(90.0, 2.0, 1.0, 3.0)
    assert len(pts) == 8 and len(segs) == 12
    assert np.isclose(pts[:, 1].max(), 3.0) and np.isclose(pts[:, 0].max(), 6.0)
    pts, _ = frustum_lines(60.0, 1.0, 0.0, 5.0, ortho_size=2.0)
    assert np.isclose(pts[:, 1].max(), 2.0)


def test_component_gizmos_by_type():
    kind, pts, _ = component_gizmo("BoxCollider", {"m_Enabled": 1, "m_IsTrigger": 1, "m_Center": {"x": 0, "y": 1, "z": 0},
                                                   "m_Size": {"x": 1, "y": 1, "z": 1}})
    assert kind == "trigger" and np.isclose(pts[:, 1].max(), 1.5)
    assert component_gizmo("SphereCollider", {"m_Enabled": 0, "m_Radius": 1}) is None
    assert component_gizmo("Light", {"m_Type": 2, "m_Range": 4})[0] == "light"
    assert component_gizmo("Camera", {"field of view": 60, "near clip plane": 0.3, "far clip plane": 1000})[0] == "camera"
    assert component_gizmo("AudioSource", {})[0] == "audio"
    assert component_gizmo("Rigidbody", {}) is None
    kind, pts, segs = component_gizmo("CircleCollider2D", {"m_Radius": 2, "m_Offset": {"x": 1, "y": 0}})
    assert kind == "collider" and np.isclose(pts[:, 0].max(), 3.0)


def test_join_offsets_segments():
    pts, segs = join(box_lines((0, 0, 0), (1, 1, 1)), box_lines((5, 0, 0), (1, 1, 1)))
    assert len(pts) == 16 and segs.min() == 0 and segs.max() == 15


def test_lod_hidden_keeps_renderers_shared_with_the_shown_level():
    from engines.unity_scene import lod_hidden
    levels = [{"a", "shared"}, {"b", "shared"}, {"c"}]
    assert lod_hidden(levels, 0) == {"b", "c"}
    assert lod_hidden(levels, 1) == {"a", "c"}
    assert lod_hidden(levels, 2) == {"a", "b", "shared"}
    assert lod_hidden([{"only"}], 0) == set()
