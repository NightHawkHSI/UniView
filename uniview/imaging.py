"""Image resizing that keeps the colour under transparent pixels. No Qt here.

Pillow resizes RGBA images with premultiplied alpha, so every pixel whose alpha is 0 comes out black. Game
textures often keep other data in alpha (smoothness, masks, nothing at all) while the material ignores it, so
their colour must survive a resize: colour and alpha are resized separately here.
"""

from PIL import Image


def resize(img, size, resample=Image.LANCZOS):
    """img resized to `size`, colour and alpha each on their own."""
    if img.mode not in ("RGBA", "LA", "PA", "RGBa", "La"):
        return img.resize(size, resample)
    alpha = img.getchannel("A").resize(size, resample)
    color = img.convert("RGB" if img.mode in ("RGBA", "PA", "RGBa") else "L").resize(size, resample)
    color.putalpha(alpha)
    return color


def shrink(img, max_side, resample=Image.LANCZOS):
    """img scaled down (aspect kept) so its longer side is at most max_side; img itself if it already fits."""
    if max(img.size) <= max_side:
        return img
    scale = max_side / max(img.size)
    return resize(img, (max(1, round(img.width * scale)), max(1, round(img.height * scale))), resample)
