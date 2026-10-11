"""Plugin manager: switching plugins off at load, installing from .py / .zip, removing, the dialog's rows."""

import os
import sys
import zipfile

import pytest

import engines
from uniview import plugin_manager, settings
from uniview.plugin_manager import PluginInstallError

PLUGIN_SOURCE = """from engines.sdk import EnginePlugin

class TestPlugin(EnginePlugin):
    id = "{id}"
    name = "{id} engine"
    version = "2.0"

PLUGIN = TestPlugin()
"""


def write_plugin(folder, fname, plugin_id):
    path = os.path.join(folder, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(PLUGIN_SOURCE.format(id=plugin_id))
    return path


@pytest.fixture
def fresh_engines(monkeypatch):
    """load_plugins() loads once per process; give it empty lists so a test can load again."""
    monkeypatch.setattr(engines, "_plugins", [])
    monkeypatch.setattr(engines, "_load_report", [])
    monkeypatch.setattr(sys, "path", list(sys.path))  # load_plugins() adds the plugins folder to it


def test_disabled_plugins_are_skipped(tmp_path, fresh_engines):
    write_plugin(str(tmp_path), "alpha.py", "alpha")
    write_plugin(str(tmp_path), "beta.py", "beta")
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    engines.load_plugins(str(tmp_path), disabled=["generic", "beta.py", "broken.py"])
    ids = [p.id for p in engines.plugins()]
    assert "alpha" in ids and "unity" in ids
    assert "beta" not in ids and "generic" not in ids
    report = {e.key: e for e in engines.load_report()}
    assert report["generic"].disabled and report["generic"].origin == "built-in"
    assert report["beta.py"].disabled and report["broken.py"].disabled  # switched off: never imported
    assert report["alpha.py"].ok and report["alpha.py"].plugin.id == "alpha"


def test_rows_mark_disabled_failed_installed_removed(tmp_path, fresh_engines):
    write_plugin(str(tmp_path), "alpha.py", "alpha")
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    engines.load_plugins(str(tmp_path), disabled=["generic"])
    write_plugin(str(tmp_path), "new.py", "new")  # installed after the start
    rows = {r.key: r for r in plugin_manager.rows(engines.load_report(), ["generic"], str(tmp_path),
                                                  installed=["new.py", "gone.py"], removed=["alpha.py"])}
    assert not rows["generic"].enabled and not rows["generic"].removable
    assert rows["unity"].enabled and rows["unity"].version != "-"
    assert rows["broken.py"].failed and "boom" in rows["broken.py"].about and rows["broken.py"].removable
    assert rows["alpha.py"].pending == "removed"
    assert rows["new.py"].pending == "installed" and rows["new.py"].enabled
    assert "gone.py" not in rows  # installed then deleted outside the app


def test_install_py(tmp_path):
    src = write_plugin(str(tmp_path), "myengine.py", "mine")
    dest = tmp_path / "plugins"
    files = plugin_manager.plan(src)
    assert plugin_manager.conflicts(files, str(dest)) == []
    assert plugin_manager.install(files, str(dest)) == ["myengine.py"]
    assert (dest / "myengine.py").read_text() == PLUGIN_SOURCE.format(id="mine")
    assert plugin_manager.conflicts(plugin_manager.plan(src), str(dest)) == ["myengine.py"]


@pytest.mark.parametrize("fname, text, error", [
    ("bad.py", "def broken(:\n", "Python error on line 1"),
    ("_helper.py", "x = 1\n", "No plugin file"),
    ("notes.txt", "hello", "is a .py file"),
])
def test_install_rejects(tmp_path, fname, text, error):
    path = tmp_path / fname
    path.write_text(text)
    with pytest.raises(PluginInstallError, match=error):
        plugin_manager.plan(str(path))


def test_install_zip_strips_wrapping_folder(tmp_path):
    path = tmp_path / "MyEngine-main.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("MyEngine-main/myengine.py", PLUGIN_SOURCE.format(id="mine"))
        zf.writestr("MyEngine-main/myengine_formats/__init__.py", "")
        zf.writestr("MyEngine-main/myengine_formats/__pycache__/x.pyc", b"junk")
        zf.writestr("MyEngine-main/README.md", "docs")
    files = plugin_manager.plan(str(path))
    assert plugin_manager.top_level(files) == ["README.md", "myengine.py", "myengine_formats"]
    dest = tmp_path / "plugins"
    assert plugin_manager.install(files, str(dest)) == ["myengine.py"]
    assert (dest / "myengine_formats" / "__init__.py").is_file()
    assert not (dest / "myengine_formats" / "__pycache__").exists()


@pytest.mark.parametrize("member", ["../evil.py", "/abs.py", "C:/evil.py"])
def test_install_zip_rejects_unsafe_paths(tmp_path, member):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("good.py", PLUGIN_SOURCE.format(id="good"))
        zf.writestr(member, "x = 1\n")
    with pytest.raises(PluginInstallError, match="unsafe path"):
        plugin_manager.plan(str(path))


def test_install_bad_zip(tmp_path):
    path = tmp_path / "fake.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(PluginInstallError, match="not a valid zip"):
        plugin_manager.plan(str(path))


def test_remove_moves_to_backup(tmp_path):
    write_plugin(str(tmp_path), "alpha.py", "alpha")
    first = plugin_manager.remove("alpha.py", str(tmp_path))
    assert not (tmp_path / "alpha.py").exists()
    assert first == str(tmp_path / plugin_manager.REMOVED_DIR / "alpha.py")
    write_plugin(str(tmp_path), "alpha.py", "alpha")
    second = plugin_manager.remove("alpha.py", str(tmp_path))
    assert second != first and os.path.isfile(second) and os.path.isfile(first)
    with pytest.raises(FileNotFoundError):
        plugin_manager.remove("alpha.py", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        plugin_manager.remove("../alpha.py", str(tmp_path))


def test_disabled_plugins_setting(tmp_path):
    s = settings.Settings.load(str(tmp_path / "settings.json"))
    assert s.disabled_plugins == []
    s.disabled_plugins = ["generic", "wakfu.py"]
    s.save()
    assert settings.Settings.load(s.path).disabled_plugins == ["generic", "wakfu.py"]
    with pytest.raises(TypeError):
        s.disabled_plugins = ["ok", 3]


@pytest.mark.parametrize("body", ["import sys\nsys.exit(1)\n", "import pytest\npytest.skip('no', allow_module_level=True)\n"])
def test_plugin_raising_base_exception_doesnt_stop_startup(tmp_path, fresh_engines, body):
    """A test file or a script dropped in plugins/ (sys.exit, pytest.skip) must not kill UniView at startup."""
    (tmp_path / "bad.py").write_text(body)
    write_plugin(str(tmp_path), "good.py", "good")
    engines.load_plugins(str(tmp_path))
    report = {e.key: e for e in engines.load_report()}
    assert not report["bad.py"].ok
    assert "good" in [p.id for p in engines.plugins()]


def test_install_rejects_a_file_that_isnt_a_plugin(tmp_path):
    path = tmp_path / "test_something.py"
    path.write_text("import os\n\ndef test_x():\n    assert os\n")
    with pytest.raises(PluginInstallError, match="isn't an engine plugin"):
        plugin_manager.plan(str(path))
    typed = tmp_path / "typed.py"
    typed.write_text(PLUGIN_SOURCE.format(id="t").replace("PLUGIN = TestPlugin()", "PLUGIN: TestPlugin = TestPlugin()"))
    assert plugin_manager.plan(str(typed))
