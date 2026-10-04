import struct
from types import SimpleNamespace

import numpy as np

from engines.unity import asset_image, float_pixels
from uniview.exr import write_exr
from uniview.unity_project import meta_text


def half_texture(values, w=2, h=2, fmt=17):
    data = np.asarray(values, dtype="<f2").tobytes()
    return SimpleNamespace(m_TextureFormat=fmt, m_Width=w, m_Height=h, image_data=data, m_StreamData=None,
                           get_image_data=lambda: data)


def test_half_texture_keeps_values_and_previews_clamped():
    # bottom row first in Unity: pixel (0,0) = -0.5 red, the top-left pixel after flipping is row 1
    values = [-0.5, 0, 0, 1, 2.0, 0, 0, 1, 0.25, 0.5, 0.75, 1, 1, 1, 1, 1]
    tex = half_texture(values)
    px = float_pixels(tex)
    assert px.shape == (2, 2, 4)
    assert px[1, 0, 0] == -0.5 and px[1, 1, 0] == 2.0 and px[0, 0, 1] == 0.5
    img = asset_image(tex)  # UnityPy fails on these ("bytes must be in range(0, 256)")
    assert img.mode == "RGBA" and img.getpixel((0, 1))[0] == 0 and img.getpixel((1, 1))[0] == 255
    assert float_pixels(SimpleNamespace(m_TextureFormat=4)) is None  # RGBA32: not a float format


def test_single_channel_float():
    tex = half_texture(np.arange(4, dtype=np.float32) - 1, fmt=18)
    tex.image_data = tex.get_image_data = None
    data = np.asarray([-1, 0, 1, 2], dtype="<f4").tobytes()
    tex.get_image_data = lambda: data
    assert float_pixels(tex)[:, :, 0].tolist() == [[1.0, 2.0], [-1.0, 0.0]]


def test_write_exr_layout(tmp_path):
    px = np.array([[[1.0, -2.0]], [[0.5, 3.0]]], dtype=np.float32)  # 2 rows, 1 column, RG
    path = tmp_path / "a.exr"
    write_exr(str(path), px)
    data = path.read_bytes()
    assert struct.unpack("<ii", data[:8]) == (20000630, 2)
    header_end = data.index(b"screenWindowWidth\0float\0") + len("screenWindowWidth\0float\0") + 8 + 1
    offsets = struct.unpack("<2Q", data[header_end:header_end + 16])
    y, size = struct.unpack("<ii", data[offsets[0]:offsets[0] + 8])
    row = np.frombuffer(data[offsets[0] + 8:offsets[0] + 8 + size], dtype="<f2")
    assert y == 0 and row.tolist() == [1.0, 0.0, -2.0, 1.0]  # A, B, G, R of the top row
    write_exr(str(path), px, float32=True)
    assert len(path.read_bytes()) > len(data)


def test_data_texture_meta():
    text = meta_text("ab", 0, {"filter_mode": 0, "wrap_mode": 1, "mipmaps": False})
    assert "sRGBTexture: 0" in text and "textureCompression: 0" in text and "nPOTScale: 0" in text
    assert "filterMode: 0" in text and "wrapU: 1" in text and "enableMipMap: 0" in text
    assert meta_text("ab", 0) == "fileFormatVersion: 2\nguid: ab\nTextureImporter:\n  serializedVersion: 4\n  textureType: 0\n"
