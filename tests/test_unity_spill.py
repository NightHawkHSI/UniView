"""engines/unity_lazy.SpillFile: one temp file per open game holding its unpacked bundles."""
import gc
import os

import pytest

from engines import unity_lazy


@pytest.fixture
def spill_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(unity_lazy, "SPILL_DIR", str(tmp_path))
    monkeypatch.setattr(unity_lazy, "SPILL_FREE", 0)
    return tmp_path


def test_store_maps_the_bytes_back(spill_dir):
    spill = unity_lazy.SpillFile("My Game: Deluxe")
    first = spill.store(6, [b"abc", b"def"])
    second = spill.store(3, [b"xyz"])
    assert bytes(first) == b"abcdef" and bytes(second) == b"xyz"
    assert first.readonly
    assert os.path.basename(spill.path).startswith("TempMy_Game_Deluxe-")
    del first, second
    spill.close()


def test_file_is_deleted_once_closed_and_released(spill_dir):
    spill = unity_lazy.SpillFile("Game")
    view = spill.store(4, [b"data"])
    spill.close()  # a reader still holds `view`: the map stays usable
    assert bytes(view) == b"data"
    del view
    gc.collect()
    assert not [n for n in os.listdir(spill_dir) if n.endswith(".bin")]


def test_low_disk_space_keeps_bundles_in_memory(spill_dir, monkeypatch):
    monkeypatch.setattr(unity_lazy, "SPILL_FREE", 1 << 62)
    spill = unity_lazy.SpillFile("Game")
    assert spill.store(4, [b"data"]) is None
    spill.close()


def test_closed_spill_stores_nothing(spill_dir):
    spill = unity_lazy.SpillFile("Game")
    unity_lazy.use_spill(spill)
    unity_lazy.release_spill(spill)
    assert unity_lazy._spill is None
    assert spill.store(4, [b"data"]) is None


def test_stale_leftovers_are_removed(spill_dir):
    (spill_dir / "TempOld-1-1.bin").write_bytes(b"x")
    (spill_dir / "preview.glb").write_bytes(b"x")  # other temp files in the folder are left alone
    unity_lazy.SpillFile("Game").close()
    assert sorted(os.listdir(spill_dir)) == ["preview.glb"]
