"""Mod Maker: merging edited data-asset JSON, and installing/restoring modded game files with backups."""

import math
import os

import numpy as np

from uniview import modmaker


def test_merge_keeps_types_and_unknown_fields():
    original = {"m_Name": "Barrel", "hp": 10, "speed": 1.5, "alive": True, "tag": "a", "blob": b"\x01\x02",
                "inner": {"x": 1, "y": 2}, "items": [{"id": 1, "n": 2}], "big": b"\0" * 100}
    new = {"hp": "25", "speed": 3, "alive": 0, "tag": 5, "blob": "0a0b", "inner": {"x": 9}, "extra": 1,
           "items": [{"id": 7}, {"id": 8, "n": 9}], "big": "<100 bytes>"}
    out = modmaker.merge_tree(original, new)
    assert out["hp"] == 25 and isinstance(out["hp"], int)
    assert out["speed"] == 3.0 and isinstance(out["speed"], float)
    assert out["alive"] is False
    assert out["tag"] == "a"                      # wrong type: keep the game's value
    assert out["blob"] == b"\x0a\x0b"
    assert out["big"] == b"\0" * 100              # shortened in the JSON: untouched
    assert out["inner"] == {"x": 9, "y": 2}
    assert out["items"] == [{"id": 7, "n": 2}, {"id": 8, "n": 9}]  # new items start from the first one
    assert "extra" not in out
    assert math.isnan(modmaker.merge_tree(0.5, "NaN"))


def _game(tmp_path):
    game = tmp_path / "Game"
    (game / "Game_Data").mkdir(parents=True)
    for name in ("a.assets", "b.assets"):
        (game / "Game_Data" / name).write_bytes(b"original " + name.encode())
    return str(game)


def _stage(project, files):
    staging = os.path.join(project.dir, "build")
    for rel, data in files.items():
        os.makedirs(os.path.dirname(os.path.join(staging, rel)), exist_ok=True)
        with open(os.path.join(staging, rel), "wb") as f:
            f.write(data)
    return list(files)


def test_install_backs_up_and_restore_puts_originals_back(tmp_path):
    game = _game(tmp_path)
    p = modmaker.ModProject(game, root=str(tmp_path / "mods"))
    a = "Game_Data/a.assets"
    modmaker.install_staged(p, _stage(p, {a: b"modded"}))
    assert open(os.path.join(game, a), "rb").read() == b"modded"
    assert p.original(a).startswith(p.backup_dir)  # rebuilding reads the backup, not our modded file
    # a second install doesn't overwrite the backup with the modded file
    modmaker.install_staged(p, _stage(p, {a: b"modded 2"}))
    assert open(os.path.join(p.backup_dir, a), "rb").read() == b"original a.assets"
    modmaker.restore(p)
    assert open(os.path.join(game, a), "rb").read() == b"original a.assets"
    assert p.installed == {} and not os.path.exists(os.path.join(p.backup_dir, a))
    assert modmaker.ModProject(game, root=str(tmp_path / "mods")).installed == {}  # saved


def test_install_restores_files_the_mod_no_longer_changes(tmp_path):
    game = _game(tmp_path)
    p = modmaker.ModProject(game, root=str(tmp_path / "mods"))
    a, b = "Game_Data/a.assets", "Game_Data/b.assets"
    modmaker.install_staged(p, _stage(p, {a: b"mod a", b: b"mod b"}))
    modmaker.install_staged(p, _stage(p, {a: b"mod a"}))
    assert open(os.path.join(game, b), "rb").read() == b"original b.assets"
    assert list(p.installed) == [a]


def test_game_update_replaces_the_stale_backup(tmp_path):
    game = _game(tmp_path)
    p = modmaker.ModProject(game, root=str(tmp_path / "mods"))
    a = "Game_Data/a.assets"
    modmaker.install_staged(p, _stage(p, {a: b"modded"}))
    with open(os.path.join(game, a), "wb") as f:  # the game updated the file
        f.write(b"new version from the update")
    assert p.original(a) == os.path.join(game, a)
    assert a not in p.installed and not os.path.exists(os.path.join(p.backup_dir, a))
    modmaker.restore(p)  # nothing to undo
    assert open(os.path.join(game, a), "rb").read() == b"new version from the update"


def test_project_folder_is_per_game(tmp_path):
    one = modmaker.project_dir(str(tmp_path / "x" / "Game"), str(tmp_path))
    two = modmaker.project_dir(str(tmp_path / "y" / "Game"), str(tmp_path))
    assert one != two and os.path.basename(one).startswith("Game-")


def test_fsb5_banks_decode_back_through_fmod():
    import io
    import wave

    import fmod_toolkit

    from uniview import fsb
    for channels, rate in ((2, 44100), (1, 37000), (6, 48000)):  # 37 kHz / 6 channels need extra chunks
        pcm = (np.arange(rate * channels) % 2000 - 1000).astype("<i2").tobytes()
        wav = next(iter(fmod_toolkit.raw_to_wav(fsb.fsb5_pcm16(pcm, channels, rate), "t", channels, rate).values()))
        with wave.open(io.BytesIO(wav)) as w:
            assert (w.getnchannels(), w.getframerate()) == (channels, rate)
            assert w.readframes(w.getnframes()) == pcm


def test_wav_widths_become_16_bit(tmp_path):
    import wave

    from uniview import fsb
    path = str(tmp_path / "a.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(8000)
        w.writeframes(bytes([0x00, 0x34, 0x12, 0x00, 0xCC, 0xED]))  # 24-bit 0x123400, -0x123400
    pcm, channels, rate = fsb.decode(path)
    assert (channels, rate) == (1, 8000)
    assert list(np.frombuffer(pcm, "<i2")) == [0x1234, -0x1234]


def test_sound_data_is_appended_aligned_to_the_resource_file(tmp_path):
    game = _game(tmp_path)
    (tmp_path / "Game" / "Game_Data" / "a.resource").write_bytes(b"x" * 40)
    p = modmaker.ModProject(game, root=str(tmp_path / "mods"))
    res = modmaker._Resources(p, "Game_Data/a.assets", top=object())
    assert res.append("a.resource", b"one") == 64
    assert res.append("a.resource", b"two") == 96
    assert bytes(res.files["Game_Data/a.resource"]) == b"x" * 40 + bytes(24) + b"one" + bytes(29) + b"two"
