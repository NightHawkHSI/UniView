"""Particle playback: the Unity ParticleSystem simulator and the quads the 3D view draws."""

import numpy as np
import pytest

from engines import unity_particles as up
from engines.sdk import Material, MeshData
from uniview import model_display as md_
from uniview import particle_view as pv_


def const(v):
    return {"minMaxState": 0, "scalar": v}


def system(**kw):
    ps = {"lengthInSec": 5.0, "looping": 1, "prewarm": 0, "startDelay": const(0.0),
          "InitialModule": {"startLifetime": const(1.0), "startSpeed": const(2.0), "startSize": const(0.5),
                            "startColor": {"minMaxState": 0, "maxColor": {"r": 1, "g": 0.5, "b": 0, "a": 1}},
                            "maxNumParticles": 1000, "gravityModifier": const(0.0)},
          "ShapeModule": {"enabled": 0},
          "EmissionModule": {"enabled": 1, "rateOverTime": const(10.0), "m_BurstCount": 0, "m_Bursts": []}}
    ps.update(kw)
    return ps


def emitter(ps, matrix=None, mode=0):
    return up.Emitter(ps, {"m_RenderMode": mode}, np.eye(4) if matrix is None else matrix, seed=7)


def test_rate_fills_up_to_lifetime():
    e = emitter(system())
    assert e.simulate(0.0) is not None and len(e.simulate(0.0)) == 1
    assert len(e.simulate(0.55)) == 6        # 0, 0.1 ... 0.5
    assert len(e.simulate(3.0)) == 10        # steady state: rate x lifetime


def test_motion_color_and_flip():
    m = np.eye(4)
    m[:3, 3] = (1.0, 2.0, 3.0)
    p = emitter(system(), m).simulate(0.25)
    newest, oldest = p.pos[-1], p.pos[0]
    assert np.allclose(newest, [-1.0, 2.0, 3.0 + 2.0 * 0.05], atol=1e-6)  # x flipped, along +z at 2/s
    assert np.allclose(oldest, [-1.0, 2.0, 3.0 + 2.0 * 0.25], atol=1e-6)
    assert np.allclose(p.color[0], [1, 0.5, 0, 1]) and np.allclose(p.size, 0.5)


def test_gravity_pulls_down():
    ps = system()
    ps["InitialModule"]["gravityModifier"] = const(1.0)
    ps["InitialModule"]["startSpeed"] = const(0.0)
    p = emitter(ps).simulate(0.5)
    assert p.pos[0, 1] == pytest.approx(-0.5 * up.GRAVITY * 0.25)


def test_one_shot_burst_repeats_in_preview():
    ps = system(looping=0, lengthInSec=1.0)
    ps["EmissionModule"] = {"enabled": 1, "rateOverTime": const(0.0), "m_BurstCount": 1,
                            "m_Bursts": [{"time": 0.0, "countCurve": const(12), "cycleCount": 1,
                                          "repeatInterval": 0.01, "probability": 1.0}]}
    e = emitter(ps)
    assert len(e.simulate(0.5)) == 12
    assert e.simulate(1.5) is None                      # all dead
    assert len(e.simulate(e.period + 0.5)) == 12        # and again


def test_lifetime_gradient_fades():
    ps = system()
    ps["ColorModule"] = {"enabled": 1, "gradient": {"minMaxState": 1, "maxGradient": {
        "key0": {"r": 1, "g": 1, "b": 1, "a": 1}, "key1": {"r": 1, "g": 1, "b": 1, "a": 0},
        "ctime0": 0, "ctime1": 65535, "atime0": 0, "atime1": 65535, "m_NumColorKeys": 2, "m_NumAlphaKeys": 2}}}
    p = emitter(ps).simulate(3.0)
    alphas = p.color[:, 3]
    assert alphas.max() > 0.9 and alphas.min() < 0.15  # newest opaque, oldest nearly gone


def test_random_between_constants_stays_in_range():
    ps = system()
    ps["InitialModule"]["startSize"] = {"minMaxState": 3, "scalar": 2.0, "minScalar": 1.0}
    sizes = emitter(ps).simulate(3.0).size
    assert sizes.min() >= 1.0 and sizes.max() <= 2.0 and np.ptp(sizes) > 0.1


def test_cone_spreads_by_angle():
    ps = system()
    ps["ShapeModule"] = {"enabled": 1, "type": 4, "angle": 30.0, "radius": {"value": 0.001},
                         "radiusThickness": 1.0}
    p = emitter(ps).simulate(3.0)
    d = p.vel / np.linalg.norm(p.vel, axis=1, keepdims=True)
    assert (d[:, 2] >= np.cos(np.radians(30.0)) - 1e-6).all()


def test_sheet_frames_and_uvs():
    uv = pv_.frame_uvs(np.array([0, 3]), (2, 2))
    assert np.allclose(uv[0], [[0, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]])   # top-left
    assert np.allclose(uv[1], [[0.5, 0], [1, 0], [1, 0.5], [0.5, 0.5]])   # bottom-right


def test_quads_face_the_camera():
    e = emitter(system())
    p = e.simulate(0.0)
    axes = pv_.camera_axes((0, 0, -10), (0, 0, 0), (0, 1, 0))
    pts, uvs, colors, tris = pv_.quads(e, p, axes)
    assert pts.shape == (4, 3) and tris.shape == (2, 3) and colors.shape == (4, 4)
    assert np.ptp(pts[:, 2]) < 1e-6  # flat, facing the view direction (z)
    assert np.ptp(pts[:, 0]) == pytest.approx(0.5) and np.ptp(pts[:, 1]) == pytest.approx(0.5)


def test_frame_geometry_groups_by_look_and_nearest():
    es = [emitter(system()), emitter(system())]
    geo, total = pv_.frame_geometry(es, [0, 1], 0.55, pv_.camera_axes((0, 0, -5), (0, 0, 0), (0, 1, 0)),
                                    lambda i: 0)
    assert total == 12 and list(geo) == [0]
    pts, uvs, colors, tris = geo[0]
    assert len(pts) == 48 and colors.dtype == np.uint8 and tris.max() == 47
    assert pv_.nearest(es * 3, np.zeros(3), k=2) and len(pv_.nearest(es * 3, np.zeros(3), k=2)) == 2


def test_texture_groups_keep_effects_apart():
    mesh = MeshData([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [2, 0, 0], [2, 1, 0]],
                    [[[0, 1, 2], [3, 4, 5]]])
    mesh.effect_mask = np.array([False, False, False, True, True, True])
    groups = md_.texture_groups(mesh, [Material("m")])
    assert sorted((len(g[2]), g[4]) for g in groups) == [(1, False), (1, True)]


# ---------------------------------------------------------------------------- bones overlay (unity bind poses)

def test_bones_from_bind_positions_and_parents():
    from engines.unity import bones_from_bind
    bind = np.stack([np.eye(4)] * 3)
    bind[1][:3, 3] = (-1.0, -2.0, 0.0)   # bone 1 sits at (1, 2, 0) in the mesh
    bind[2][:3, 3] = (0.0, -3.0, 0.0)
    nodes = {"hips": {"name": "Hips", "parent": None}, "spine": {"name": "Spine", "parent": "hips"},
             "twist": {"name": "Twist", "parent": "spine"}, "head": {"name": "Head", "parent": "twist"}}
    b = bones_from_bind(["hips", "spine", "head"], bind, nodes)
    assert b.names == ["Hips", "Spine", "Head"]
    assert list(b.parents) == [-1, 0, 1]          # Head's parent skips the non-bone Twist
    assert np.allclose(b.points[1], [-1.0, 2.0, 0.0])  # x flipped like the mesh


# ---------------------------------------------------------------------------- trails module

def trail_system(mode=0, **trail):
    ps = system()
    ps["TrailModule"] = {"enabled": 1, "mode": mode, "ratio": 1.0, "lifetime": const(0.5),
                         "widthOverTrail": const(1.0), "sizeAffectsWidth": 1, "inheritParticleColor": 1,
                         "colorOverTrail": {"minMaxState": 0, "maxColor": {"r": 1, "g": 1, "b": 1, "a": 1}}, **trail}
    return up.Emitter(ps, {"m_RenderMode": 0}, np.eye(4), seed=7, trail_material=3)


def test_particle_trails_follow_the_path():
    p = trail_system().simulate(0.95)
    pts, widths, colors = p.trails
    assert pts.shape == (len(p), up.TRAIL_POINTS, 3) and widths.shape == pts.shape[:2]
    oldest = 0  # born at t=0: 0.95 s old, its trail covers the last 0.5 s of its 1 s life
    assert np.allclose(pts[oldest, 0], p.pos[oldest])                       # starts at the particle
    assert pts[oldest, -1, 2] == pytest.approx(2.0 * (0.95 - 0.5), abs=1e-6)  # ends where it was 0.5 s ago
    assert np.allclose(widths, 0.5) and np.allclose(colors[oldest, 0], p.color[oldest])


def test_ribbon_trails_join_particles():
    p = trail_system(mode=1).simulate(0.55)
    pts, widths, _c = p.trails
    assert pts.shape == (1, len(p), 3)  # one ribbon through every particle, newest first
    assert np.allclose(pts[0, 0], p.pos[-1]) and np.allclose(pts[0, -1], p.pos[0])


def test_no_trails_without_trail_material():
    ps = trail_system().ps
    assert up.Emitter(ps, {"m_RenderMode": 0}, np.eye(4)).simulate(0.5).trails is None


def test_trail_strips_and_keys():
    e = trail_system()
    axes = pv_.camera_axes((0, -10, 0), (0, 0, 0), (0, 0, 1))
    geo, _n = pv_.frame_geometry([e], [0], 0.95, axes, lambda i: "p", lambda i: "t")
    pts, uvs, colors, tris = geo["t"]
    m = len(e.simulate(0.95))
    assert len(pts) == m * up.TRAIL_POINTS * 2 and len(tris) == m * (up.TRAIL_POINTS - 1) * 2
    assert uvs[:, 0].min() == 0 and uvs[:, 0].max() == 1 and "p" in geo
