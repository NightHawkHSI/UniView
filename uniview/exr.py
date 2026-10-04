"""Minimal OpenEXR writer: uncompressed scanlines, half or float channels. Enough for Unity to import
float textures (vertex animation data, lookup tables) without rounding them to 8 bits. No Qt here."""

import struct

import numpy as np

HALF, FLOAT = 1, 2


def _attr(name, kind, value):
    return name.encode() + b"\0" + kind.encode() + b"\0" + struct.pack("<i", len(value)) + value


def write_exr(path, pixels, float32=False):
    """pixels: H x W x C (C = 1..4, rows top-down) -> R, G, B, A channels (missing ones 0, alpha 1)."""
    pixels = np.asarray(pixels, dtype=np.float32)
    if pixels.ndim == 2:
        pixels = pixels[..., None]
    h, w, c = pixels.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[..., 3] = 1.0
    rgba[..., :c] = pixels[..., :4]
    kind, dtype = (FLOAT, "<f4") if float32 else (HALF, "<f2")
    names = "ABGR"  # EXR stores channels in alphabetical order
    chlist = b"".join(n.encode() + b"\0" + struct.pack("<iB3xii", kind, 0, 1, 1) for n in names) + b"\0"
    box = struct.pack("<iiii", 0, 0, w - 1, h - 1)
    header = (struct.pack("<ii", 20000630, 2)
              + _attr("channels", "chlist", chlist)
              + _attr("compression", "compression", b"\0")
              + _attr("dataWindow", "box2i", box)
              + _attr("displayWindow", "box2i", box)
              + _attr("lineOrder", "lineOrder", b"\0")
              + _attr("pixelAspectRatio", "float", struct.pack("<f", 1.0))
              + _attr("screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
              + _attr("screenWindowWidth", "float", struct.pack("<f", 1.0))
              + b"\0")
    # One scanline per chunk: y, byte count, then each channel's row.
    order = [3, 2, 1, 0]  # A, B, G, R
    planes = rgba[..., order].astype(dtype)  # H x W x 4
    row_bytes = w * 4 * np.dtype(dtype).itemsize
    chunk = 8 + row_bytes
    start = len(header) + 8 * h
    offsets = struct.pack(f"<{h}Q", *(start + y * chunk for y in range(h)))
    with open(path, "wb") as f:
        f.write(header)
        f.write(offsets)
        for y in range(h):
            f.write(struct.pack("<ii", y, row_bytes))
            f.write(np.ascontiguousarray(planes[y].T).tobytes())  # channel-major within the row
