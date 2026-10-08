"""Resizing keeps the colour under transparent pixels (Pillow's premultiplied RGBA resize turns it black)."""

import numpy as np
from PIL import Image

from uniview import imaging


def rgba(alpha):
    a = np.zeros((8, 8, 4), np.uint8)
    a[..., :3] = (200, 100, 50)
    a[..., 3] = alpha
    return Image.fromarray(a)


def test_resize_keeps_colour_under_alpha_zero():
    out = np.asarray(imaging.resize(rgba(0), (4, 4)))
    assert tuple(out[0, 0]) == (200, 100, 50, 0)


def test_shrink_keeps_aspect_and_small_images():
    img = rgba(255).resize((80, 40))
    assert imaging.shrink(img, 20).size == (20, 10)
    small = rgba(0)
    assert imaging.shrink(small, 64) is small


def test_resize_other_modes():
    assert imaging.resize(Image.new("RGB", (8, 8), (1, 2, 3)), (2, 2)).getpixel((0, 0)) == (1, 2, 3)
    la = imaging.resize(Image.new("LA", (8, 8), (90, 0)), (2, 2))
    assert la.mode == "LA" and la.getpixel((0, 0)) == (90, 0)
