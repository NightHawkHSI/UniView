"""Scene lighting: lightmap decoding, sky colors, lightmap groups."""

from types import SimpleNamespace

import numpy as np
from PIL import Image

from engines.sdk import ALBEDO, Material, MeshData, TextureRef
from uniview import model_display as md_


def solid(rgba):
    return Image.fromarray(np.full((2, 2, 4), rgba, np.uint8))


def test_lightmap_decoding():
    # RGBM: rgb x 5 x alpha (0.2 alpha = x1)
    assert md_.lightmap_image(solid((100, 50, 0, 51)), "rgbm").getpixel((0, 0)) == (100, 50, 0)
    assert md_.lightmap_image(solid((100, 200, 0, 255)), "rgbm").getpixel((0, 0)) == (255, 255, 0)  # clipped
    assert md_.lightmap_image(solid((100, 50, 0, 255)), "dldr").getpixel((0, 0)) == (200, 100, 0)
    # HDR: linear light -> display (gamma 2.2)
    r, g, _b = md_.lightmap_image(solid((64, 255, 0, 255)), "hdr").getpixel((0, 0))
    assert 135 <= r <= 137 and g == 255


def test_lightmapped_parts_kept_apart():
    mesh = MeshData([[0, 0, 0], [1, 0, 0], [0, 1, 0]] * 2, [[[0, 1, 2]], [[3, 4, 5]]])
    tex = TextureRef("_MainTex", "t", SimpleNamespace(key="t", name="t"), ALBEDO)
    baked = Material("baked", [tex])
    baked.lightmap, baked.lightmap_mode = SimpleNamespace(key="lm0", name="Lightmap-0"), "hdr"
    groups = md_.texture_groups(mesh, [baked, Material("live", [tex])])
    assert sorted((g[6] is not None) for g in groups) == [False, True]
    assert next(g for g in groups if g[6])[6][1] == "hdr"


def test_sky_colors_procedural():
    from engines.unity_scene import sky_colors

    def color(r, g, b):
        return SimpleNamespace(r=r, g=g, b=b, a=1.0)
    mat = SimpleNamespace(m_SavedProperties=SimpleNamespace(
        m_Colors=[("_SkyTint", color(0.5, 0.5, 0.5)), ("_GroundColor", color(0.37, 0.35, 0.34))],
        m_Floats=[("_AtmosphereThickness", 1.0), ("_Exposure", 1.3)]))
    ptr = SimpleNamespace(path_id=1, deref=lambda: SimpleNamespace(read=lambda: mat))
    render = {"_reader": SimpleNamespace(read=lambda: SimpleNamespace(m_SkyboxMaterial=ptr))}
    top, horizon, bottom = sky_colors(None, render)
    assert top[2] > top[0] and horizon[2] >= horizon[0]  # blue sky, pale horizon
    assert bottom == (0.37, 0.35, 0.34)


# ---------------------------------------------------------------------------- skyboxes

def faces_coded(n=8):
    """Unity-order cube faces whose red = face index x 40 and green / blue = column / row."""
    col, row = np.meshgrid(np.arange(n), np.arange(n))
    return [Image.fromarray(np.stack([np.full((n, n), f * 40), col * 30, row * 30], -1).astype(np.uint8))
            for f in range(6)]


def test_cube_sky_unchanged_and_full_turn():
    faces = faces_coded()
    same = md_.sky_faces("cube", faces)
    assert all(np.array_equal(a, np.asarray(b)) for a, b in zip(same, faces))
    turned = md_.sky_faces("cube", faces, rotation=360.0)
    assert all(np.array_equal(a, np.asarray(b)) for a, b in zip(turned, faces))


def test_quarter_turn_moves_faces_around():
    turned = md_.sky_faces("cube", faces_coded(), rotation=90.0)
    assert {int(f[4, 4, 0]) // 40 for f in turned[:2] + turned[4:]} == {0, 1, 4, 5}  # sides swap among sides
    assert int(turned[2][4, 4, 0]) // 40 == 2 and int(turned[3][4, 4, 0]) // 40 == 3  # up / down stay


def test_panorama_like_unity():
    # columns: left quarter red ... Unity's Panoramic shows the middle column straight along +X
    w, h = 64, 32
    pano = np.zeros((h, w, 3), np.uint8)
    pano[:, w // 2 - 2:w // 2 + 2] = (255, 0, 0)   # u = 0.5 -> +X
    pano[:, w // 4 - 2:w // 4 + 2] = (0, 255, 0)   # u = 0.25 -> +Z
    pano[:4] = (0, 0, 255)                         # top rows -> +Y
    faces = md_.sky_faces("pano", [Image.fromarray(pano)], size=16)
    assert tuple(faces[0][8, 8]) == (255, 0, 0) and tuple(faces[4][8, 8]) == (0, 255, 0)
    assert tuple(faces[2][8, 8]) == (0, 0, 255)


def test_vtk_slots_half_turn():
    faces = [np.asarray(f) for f in faces_coded()]
    slots = md_.vtk_cube_faces(faces)
    assert [int(s[0, 0, 0]) // 40 for s in slots] == [1, 0, 2, 3, 5, 4]
    assert np.array_equal(slots[0], faces[1]) and np.array_equal(slots[2], faces[2][::-1, ::-1])


def test_cubemap_cross_layout():
    from engines.unity import cubemap_cross
    cross = np.asarray(cubemap_cross(faces_coded()))
    n = 8
    assert cross.shape == (3 * n, 4 * n, 3)
    at = {f: (c, r) for f, (c, r) in enumerate(((2, 1), (0, 1), (1, 0), (1, 2), (1, 1), (3, 1)))}
    for f, (c, r) in at.items():
        assert cross[r * n + 1, c * n + 1, 0] == f * 40
