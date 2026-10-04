"""SpriteRenderers drawn as quads in the 3D scene view."""

import numpy as np

from engines.unity_scene import sprite_quad


def test_simple_sprite_is_placed_around_its_pivot():
    # A 200x100 px sprite at 100 ppu, centre pivot, using the whole of a 200x100 texture.
    pts, uvs, tris = sprite_quad((200, 100), (0.5, 0.5), 100, (0, 0, 200, 100), (0, 0), (200, 100))
    assert np.allclose(pts.min(0), [-1, -0.5, 0]) and np.allclose(pts.max(0), [1, 0.5, 0])
    assert np.allclose(uvs.min(0), [0, 0]) and np.allclose(uvs.max(0), [1, 1])
    assert len(tris) == 4  # two triangles per side


def test_trimmed_atlas_sprite_uses_its_part_of_the_texture():
    # 100x100 rect, bottom-left pivot; only a 50x40 part at (10, 20) of the rect has pixels, packed at
    # (300, 200) in a 1000x500 atlas.
    pts, uvs, _ = sprite_quad((100, 100), (0, 0), 100, (300, 200, 50, 40), (10, 20), (1000, 500))
    assert np.allclose(pts.min(0)[:2], [0.1, 0.2]) and np.allclose(pts.max(0)[:2], [0.6, 0.6])
    assert np.allclose(uvs.min(0), [0.3, 0.4]) and np.allclose(uvs.max(0), [0.35, 0.48])


def test_flip_and_sliced_size():
    pts, uvs, _ = sprite_quad((100, 100), (0, 0), 100, (0, 0, 100, 100), (0, 0), (100, 100), flip_x=True)
    assert np.allclose(pts.min(0)[:2], [-1, 0]) and np.allclose(pts.max(0)[:2], [0, 1])
    assert uvs[0][0] == 1.0  # the left corner shows the texture's right edge
    pts, _, _ = sprite_quad((100, 100), (0.5, 0.5), 100, (0, 0, 100, 100), (0, 0), (100, 100), draw_mode=1,
                            size=(4, 2))
    assert np.allclose(pts.min(0)[:2], [-2, -1]) and np.allclose(pts.max(0)[:2], [2, 1])
