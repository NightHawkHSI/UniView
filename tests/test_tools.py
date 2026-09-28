"""Optional tools: finding UniView's own ilspycmd copy, and which missing tools trigger the startup popup."""

from uniview import tools


def test_ilspy_command_prefers_own_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "tools_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tools, "find_dotnet", lambda: "C:/dotnet/dotnet.exe")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools.os.path, "expanduser", lambda p: str(tmp_path / "home"))
    assert tools.ilspy_command() is None
    (tmp_path / "ilspycmd").mkdir()
    (tmp_path / "ilspycmd" / "ilspycmd.dll").write_bytes(b"")
    assert tools.ilspy_command() == ["C:/dotnet/dotnet.exe", str(tmp_path / "ilspycmd" / "ilspycmd.dll")]
    monkeypatch.setattr(tools, "find_dotnet", lambda: None)  # no .NET runtime: the dll alone can't run
    assert tools.ilspy_command() is None


def test_ilspy_env_allows_newer_runtime():
    assert tools.ilspy_env()["DOTNET_ROLL_FORWARD"] == "Major"


def test_only_recommended_tools_trigger_the_popup(monkeypatch):
    fake = [tools.Tool("a", "A", "", lambda: None, None), tools.Tool("b", "B", "", lambda: "C:/b.exe", None),
            tools.Tool("c", "C", "", lambda: None, None, recommended=False)]
    monkeypatch.setattr(tools, "all_tools", lambda blender_path="": fake)
    assert [t.id for t in tools.missing_recommended()] == ["a"]
