"""Cpp2IL stub assemblies for IL2CPP games: detection, the per-build cache, and the assembly list."""

import json
import os

from engines import cpp2il
from uniview.unity_packages import game_assemblies


def make_il2cpp_game(root, name="Game"):
    game = root / name
    (game / f"{name}_Data" / "il2cpp_data" / "Metadata").mkdir(parents=True)
    (game / "GameAssembly.dll").write_bytes(b"binary")
    (game / f"{name}_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"meta")
    return game


def test_il2cpp_detection(tmp_path):
    game = make_il2cpp_game(tmp_path)
    assert cpp2il.is_il2cpp(str(game))
    os.remove(game / "Game_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat")
    assert not cpp2il.is_il2cpp(str(game))
    assert not cpp2il.is_il2cpp(str(tmp_path / "missing"))


def test_stub_assemblies_cached_per_build(tmp_path, monkeypatch):
    game = make_il2cpp_game(tmp_path)
    monkeypatch.setattr(cpp2il, "cache_dir", lambda: str(tmp_path / "cache"))
    monkeypatch.setattr(cpp2il, "cpp2il_path", lambda: None)
    assert cpp2il.stub_assemblies(str(game)) is None  # not installed

    runs = []

    def fake_run(exe, game_dir, out_dir, timeout, cancelled):
        runs.append(out_dir)
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "Assembly-CSharp.dll"), "wb").close()

    monkeypatch.setattr(cpp2il, "cpp2il_path", lambda: "C:/tools/Cpp2IL.exe")
    monkeypatch.setattr(cpp2il, "_run", fake_run)
    first = cpp2il.stub_assemblies(str(game))
    assert os.path.isfile(os.path.join(first, "Assembly-CSharp.dll"))
    assert cpp2il.stub_assemblies(str(game)) == first and len(runs) == 1  # cached

    (game / "GameAssembly.dll").write_bytes(b"updated game")  # a game update: rebuilt, old result removed
    second = cpp2il.stub_assemblies(str(game))
    assert second != first and len(runs) == 2
    assert not os.path.exists(first)


def test_failure_not_retried(tmp_path, monkeypatch):
    game = make_il2cpp_game(tmp_path, "Broken")
    monkeypatch.setattr(cpp2il, "cache_dir", lambda: str(tmp_path / "cache"))
    monkeypatch.setattr(cpp2il, "cpp2il_path", lambda: "C:/tools/Cpp2IL.exe")
    runs = []

    def failing_run(*args):
        runs.append(1)
        raise RuntimeError("Unsupported metadata version")

    monkeypatch.setattr(cpp2il, "_run", failing_run)
    for _ in range(2):
        try:
            cpp2il.stub_assemblies(str(game))
            raise AssertionError("expected a failure")
        except RuntimeError as e:
            assert "Unsupported metadata" in str(e)
    assert len(runs) == 1


def test_game_assemblies_from_scripting_assemblies_json(tmp_path):
    game = make_il2cpp_game(tmp_path)
    (game / "Game_Data" / "ScriptingAssemblies.json").write_text(
        json.dumps({"names": ["UnityEngine.dll", "Unity.Timeline.dll", "Assembly-CSharp.dll"], "types": [16, 16, 16]}))
    assert game_assemblies(str(game)) == ["UnityEngine.dll", "Unity.Timeline.dll", "Assembly-CSharp.dll"]
    managed = game / "Game_Data" / "Managed"  # a Mono build's Managed folder wins
    managed.mkdir()
    (managed / "Assembly-CSharp.dll").write_bytes(b"")
    assert game_assemblies(str(game)) == ["Assembly-CSharp.dll"]
