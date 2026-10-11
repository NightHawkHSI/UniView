"""Saving assets without the UI: file names, bulk export plans, write_asset per kind."""

import logging
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from engines.sdk import Asset, Material, MeshData
from uniview import export as ex


class Session:
    """Fake GameSession: each hook can be a value, or an exception to raise."""

    def __init__(self, **hooks):
        self.lock = threading.RLock()
        self.hooks = hooks

    def _call(self, name, *args):
        v = self.hooks.get(name, NotImplementedError(name))
        if isinstance(v, Exception):
            raise v
        return v(*args) if callable(v) else v

    def mesh(self, a): return self._call("mesh", a)
    def materials(self, a): return self._call("materials", a)
    def image(self, a): return self._call("image", a)
    def audio(self, a): return self._call("audio", a)
    def raw(self, a): return self._call("raw", a)
    def text(self, a): return self._call("text", a)
    def skeleton(self, a, clip=None): return self._call("skeleton", a)
    def sprite_frames(self, a): return self._call("sprite_frames", a)


def tri():
    return MeshData([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[[0, 1, 2]]])


@pytest.mark.parametrize("kind, ext, fmt, expected", [
    ("model", "", "glb", "glb"), ("scene", "", "obj", "obj"), ("texture", "dds", "obj", "png"),
    ("sprite", "", "obj", "png"), ("audio", "vsnd_c", "obj", "wav"), ("audio", "", "obj", "wav"),
    ("audio", "ogg", "obj", "ogg"), ("text", "", "obj", "txt"), ("file", "", "obj", "bin"),
    ("font", "ttf", "obj", "ttf"),
])
def test_export_ext(kind, ext, fmt, expected):
    assert ex.export_ext(Asset(kind, "x", "x", ext=ext), fmt) == expected


def test_export_stem_strips_duplicate_extension_only_for_raw_kinds():
    assert ex.export_stem(Asset("text", "scripts/readme.txt", 1), "txt") == "readme"
    assert ex.export_stem(Asset("audio", "sfx\\boom.ogg", 1), "ogg") == "boom"
    assert ex.export_stem(Asset("model", "props/crate.obj", 1), "obj") == "crate.obj"
    assert ex.export_stem(Asset("text", "a<b>.txt", 1), "txt") == ex.export_stem(Asset("text", "a<b>", 1))


def test_plan_export_unique_names_flat(tmp_path):
    items = [Asset("texture", "a/wall", 1), Asset("texture", "b/wall", 2), Asset("texture", "c/WALL", 3),
             Asset("model", "wall", 4)]
    plan = ex.plan_export(items, str(tmp_path), "glb")
    names = [os.path.basename(p) for _a, p in plan]
    assert names == ["wall.png", "wall_2.png", "WALL_3.png", "wall_4.glb"]
    assert [a for a, _p in plan] == items


def test_plan_export_keeps_structure(tmp_path):
    items = [Asset("texture", "wall", 1, path="textures/walls/wall.png"),
             Asset("texture", "wall", 2, path="textures/floors/wall.png")]
    plan = ex.plan_export(items, str(tmp_path), keep_structure=True)
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in plan]
    assert rel == ["textures/walls/wall.png", "textures/floors/wall.png"]  # different folders: no suffix


def test_write_asset_image(tmp_path):
    s = Session(image=Image.new("RGB", (3, 2)))
    path = str(tmp_path / "t.png")
    assert ex.write_asset(s, Asset("texture", "t", 1), path) == [path]
    assert Image.open(path).size == (3, 2)


def test_write_asset_audio_uses_decoded_extension(tmp_path):
    s = Session(audio=(b"RIFFdata", "wav"))
    out = ex.write_asset(s, Asset("audio", "s", 1, ext="vsnd_c"), str(tmp_path / "s.vsnd_c"))
    assert out == [str(tmp_path / "s.wav")] and open(out[0], "rb").read() == b"RIFFdata"


def test_write_asset_audio_falls_back_to_raw(tmp_path):
    s = Session(raw=b"bank")
    out = ex.write_asset(s, Asset("audio", "s", 1, ext="bnk"), str(tmp_path / "s.bnk"))
    assert open(out[0], "rb").read() == b"bank"


def test_write_asset_text_fallback_when_no_raw(tmp_path):
    s = Session(text="héllo")
    out = ex.write_asset(s, Asset("text", "t", 1), str(tmp_path / "t.txt"))
    assert open(out[0], "rb").read() == "héllo".encode("utf-8")


def test_write_asset_model_glb_and_obj(tmp_path):
    s = Session(mesh=tri(), materials=[Material("m")])
    glb = ex.write_asset(s, Asset("model", "m", 1), str(tmp_path / "m.glb"))
    obj = ex.write_asset(s, Asset("model", "m", 1), str(tmp_path / "m.obj"))
    assert open(glb[0], "rb").read(4) == b"glTF"
    assert any(p.endswith(".obj") for p in obj) and any(p.endswith(".mtl") for p in obj)


def test_session_materials_failure_is_logged(caplog):
    s = Session(materials=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR, logger="viewer"):
        assert ex.session_materials(s, Asset("model", "m", 1)) == []
    assert "Finding the materials of 'm' failed" in caplog.text


def test_rig_for_export():
    md = tri()
    good = {"joints_0": np.zeros((3, 4))}
    assert ex.rig_for_export(Session(skeleton=(good, None)), Asset("model", "m", 1), md) is good
    assert ex.rig_for_export(Session(skeleton=({"joints_0": np.zeros((2, 4))}, None)),
                             Asset("model", "m", 1), md) is None  # vertex count mismatch
    assert ex.rig_for_export(Session(), Asset("model", "m", 1), md) is None  # not supported
    assert ex.rig_for_export(Session(skeleton=(good, None)), Asset("scene", "m", 1), md) is None


def test_write_animated_glb(tmp_path):
    rig = {"joints": [{"name": "r", "parent": -1, "translation": [0, 0, 0], "rotation": [0, 0, 0, 1],
                       "scale": [1, 1, 1]}], "skin_joints": [0], "inverse_bind": np.eye(4)[None],
           "joints_0": np.zeros((3, 4), np.uint16), "weights_0": np.tile([1, 0, 0, 0], (3, 1)).astype(np.float32)}
    anim = {"name": "clip", "times": np.array([0.0, 1.0], np.float32),
            "channels": [{"joint": 0, "path": "translation", "values": np.zeros((2, 3), np.float32)}]}
    s = Session(mesh=tri(), materials=[], skeleton=lambda a: (rig, anim))
    written, got_rig = ex.write_animated_glb(s, Asset("model", "m", 1), SimpleNamespace(), str(tmp_path / "a.glb"))
    assert got_rig is rig and open(written[0], "rb").read(4) == b"glTF"


def _flipbook_session():
    """Two frames of different sizes, 0.1 s and 0.3 s."""
    red, blue = Image.new("RGBA", (4, 2), (255, 0, 0, 255)), Image.new("RGBA", (2, 6), (0, 0, 255, 255))
    frames = [(0.0, Asset("sprite", "f0", "f0")), (0.1, Asset("sprite", "f1", "f1"))]
    return Session(sprite_frames=(frames, 0.4), image=lambda a: red if a.key == "f0" else blue)


def test_flipbook_frames_share_one_canvas():
    frames = ex.flipbook(_flipbook_session(), Asset("animation", "walk", 1))
    assert [img.size for img, _d in frames] == [(4, 6), (4, 6)]
    assert [round(d, 3) for _i, d in frames] == [0.1, 0.3]
    assert frames[0][0].getpixel((0, 5))[3] == 255 and frames[0][0].getpixel((0, 0))[3] == 0  # stands on the bottom


def test_write_flipbook_gif_sheet_and_frames(tmp_path):
    import json
    s, clip = _flipbook_session(), Asset("animation", "walk", 1)
    gif = ex.write_asset(s, clip, str(tmp_path / "walk.gif"))
    assert open(gif[0], "rb").read(6) == b"GIF89a"
    with Image.open(gif[0]) as im:
        assert im.n_frames == 2
    sheet = ex.write_asset(s, clip, str(tmp_path / "sheet.png"), "sheet")
    assert [os.path.basename(p) for p in sheet] == ["sheet.png", "sheet.json"]
    meta = json.load(open(sheet[1]))
    assert meta["meta"]["image"] == "sheet.png" and [f["duration"] for f in meta["frames"]] == [100, 300]
    with Image.open(sheet[0]) as im:
        f1 = meta["frames"][1]["frame"]
        assert im.getpixel((f1["x"] + 1, f1["y"] + 5))[2] == 255  # the blue frame sits in its cell
    files = ex.write_asset(s, clip, str(tmp_path / "run.png"), "frames")
    assert [os.path.relpath(p, tmp_path).replace(os.sep, "/") for p in files] == [
        "run/run_000.png", "run/run_001.png", "run/run.json"]


def test_non_flipbook_animation_is_saved_as_stored(tmp_path):
    s = Session(sprite_frames=([], 0.0), raw=b"clip")
    out = ex.write_asset(s, Asset("animation", "a", 1, ext="anim"), str(tmp_path / "a.gif"), "gif")
    assert out == [str(tmp_path / "a.anim")] and open(out[0], "rb").read() == b"clip"
    assert ex.export_ext(Asset("animation", "a", 1, ext="anim"), "obj", "sheet") == "png"
