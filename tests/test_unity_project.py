"""Export as Unity project: GUIDs, .meta files, layout, version."""

import os
import threading

from PIL import Image

from engines.sdk import Asset
from uniview import unity_project as up


class Session:
    def __init__(self, assets):
        self.lock = threading.RLock()
        self.assets = assets

    def image(self, a):
        if a.name == "empty":
            raise ValueError("Empty texture")
        return Image.new("RGB", (2, 2))

    def audio(self, a):
        return b"RIFF", "wav"

    def raw(self, a):
        return b"data"


def test_guid_is_stable_and_unity_shaped():
    g = up.asset_guid("sharedassets0.assets:42")
    assert g == up.asset_guid("sharedassets0.assets:42") != up.asset_guid("sharedassets0.assets:43")
    assert len(g) == 32 and int(g, 16) >= 0


def test_unity_version():
    assert up.unity_version("", "2019.4.19f1") == "2019.4.19f1"
    assert up.unity_version("2022.3.62f2", "2019.4.11f1") == "2022.3.62f2"
    assert up.unity_version("6000.0.23f1") == "6000.0.23f1"
    assert up.unity_version("5.6.7f1", "Unity 2019", None) == ""  # Unity 5 has no 4-digit year


def test_plan_skips_builtins_and_unhandled_kinds(tmp_path):
    assets = [Asset("texture", "Soft", 1, source="unity default resources"),
              Asset("texture", "wall", 2, source="sharedassets0.assets"),
              Asset("texture", "wall", 3, source="sharedassets0.assets"),
              Asset("model", "crate", 4, source="sharedassets0.assets"),
              Asset("audio", "boom", 5, source="resources.assets", ext="wav")]
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for _a, p in up.plan(assets, str(tmp_path))]
    assert rel == ["sharedassets0.assets/Textures/wall.png", "sharedassets0.assets/Textures/wall_2.png",
                   "resources.assets/Audio/boom.wav"]


def test_export_writes_project(tmp_path):
    assets = [Asset("texture", "wall", 1, uid="s:1", source="sharedassets0.assets"),
              Asset("texture", "empty", 2, uid="s:2", source="sharedassets0.assets"),
              Asset("audio", "boom", 3, uid="r:3", source="resources.assets", ext="wav"),
              Asset("text", "readme", 4, uid="r:4", source="resources.assets", ext="txt")]
    calls = []
    written, failed = up.export_unity_project(Session(assets), str(tmp_path), "2019.4.19f1",
                                              progress=lambda d, t, n: calls.append((d, t)))
    assert (written, failed) == (3, 1)
    assert calls[-1] == (4, 4)
    assert open(tmp_path / "ProjectSettings" / "ProjectVersion.txt").read() == "m_EditorVersion: 2019.4.19f1\n"
    png = tmp_path / "Assets" / "sharedassets0.assets" / "Textures" / "wall.png"
    assert png.exists()
    assert open(str(png) + ".meta").read() == f"fileFormatVersion: 2\nguid: {up.asset_guid('s:1')}\n"
    folder_meta = open(tmp_path / "Assets" / "sharedassets0.assets" / "Textures.meta").read()
    assert "folderAsset: yes" in folder_meta
    assert (tmp_path / "Assets" / "sharedassets0.assets.meta").exists()
    assert not (tmp_path / "Assets.meta").exists()  # Assets/ itself never has a .meta


def test_export_cancel(tmp_path):
    assets = [Asset("text", f"t{i}", i, uid=f"t{i}", source="x", ext="txt") for i in range(30)]
    written, _ = up.export_unity_project(Session(assets), str(tmp_path), cancelled=lambda: True)
    assert written == 0
    assert not (tmp_path / "ProjectSettings" / "ProjectVersion.txt").exists()  # no version given
