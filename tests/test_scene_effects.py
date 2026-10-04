"""Particle systems and line renderers as still geometry in the 3D scene view."""

import numpy as np
import pytest

from engines.unity_scene import MAX_PUFFS, crossed_quads, curve_value, gradient_color, particle_puffs, ribbon


def const(v):
    return {"minMaxState": 0, "scalar": v}


def test_curve_and_gradient_values():
    assert curve_value(const(2.0)) == 2.0
    assert curve_value({"minMaxState": 3, "scalar": 4.0, "minScalar": 2.0}) == 3.0
    assert curve_value({"minMaxState": 1, "scalar": 2.0, "maxCurve": {"m_Curve": [{"value": 0.0}, {"value": 1.0}]}}) == 1.0
    assert curve_value(None, 7.0) == 7.0
    red = {"r": 1.0, "g": 0.0, "b": 0.0, "a": 0.5}
    assert gradient_color({"minMaxState": 0, "maxColor": red}) == (1.0, 0.0, 0.0, 0.5)
    assert gradient_color({"minMaxState": 1, "maxGradient": {"key0": red}}) == (1.0, 0.0, 0.0, 0.5)


def test_cone_emitter_puffs_go_along_z_within_the_cone():
    ps = {"InitialModule": {"startSize": const(0.5), "startLifetime": const(2.0), "startSpeed": const(4.0)},
          "EmissionModule": {"enabled": True, "rateOverTime": const(10.0)},
          "ShapeModule": {"enabled": True, "type": 4, "radius": {"value": 0.1}, "angle": 10.0}}
    centres, size = particle_puffs(ps, seed=1)
    assert size == 0.5 and len(centres) == MAX_PUFFS
    assert np.all(centres[:, 2] > 0) and centres[:, 2].max() <= 4.0  # half of speed x lifetime
    radial = np.hypot(centres[:, 0], centres[:, 1])
    assert np.all(radial <= 0.1 + centres[:, 2] * np.tan(np.radians(10.0)) + 1e-9)
    again, _ = particle_puffs(ps, seed=1)
    assert np.allclose(centres, again)  # the same every time


def test_single_burst_without_shape_is_one_puff_at_the_emitter():
    ps = {"InitialModule": {"startSize": const(1.0), "startLifetime": const(1.0), "startSpeed": const(0.0)},
          "EmissionModule": {"enabled": True, "rateOverTime": const(0.0)},
          "ShapeModule": {"enabled": False}}
    centres, _ = particle_puffs(ps)
    assert np.allclose(centres, [[0, 0, 0]])


def test_crossed_quads_and_ribbon_shapes():
    pts, uvs, tris = crossed_quads(np.zeros((2, 3)), 2.0, (0, 0.5, 0.5, 1))
    assert pts.shape == (24, 3) and tris.shape == (24, 3)
    assert np.allclose(np.abs(pts).max(), 1.0) and uvs.min() == 0 and uvs.max() == 1 and 0.5 in uvs
    pts, _, tris = ribbon([(0, 0, 0), (0, 0, 2), (0, 0, 2), (1, 0, 2)], 0.2)
    assert len(tris) == 2 * 2 * 4  # 2 segments (the repeated point is skipped), 2 strips, 4 triangles each
    assert pytest.approx(float(np.abs(pts[:, 0]).max()), abs=1e-6) == 1.0
    assert ribbon([(0, 0, 0)], 1.0) is None
    loop = ribbon([(0, 0, 0), (1, 0, 0), (1, 1, 0)], 0.1, loop=True)
    assert len(loop[2]) == 3 * 2 * 4
