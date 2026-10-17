"""The built-in engines load on their own: UniView works with an empty (or missing) plugins folder.

Game plugins live in their own repo (UniView-Plugins) and are tested there.
"""

import os

import pytest

import engines
from engines.sdk import EnginePlugin, GameSession


def _fresh_load(user_dir):
    """(plugins, report) of a first load from user_dir; the loader's state before is put back after."""
    saved = list(engines._plugins), list(engines._load_report)
    engines._plugins.clear()
    engines._load_report.clear()
    try:
        return engines.load_plugins(user_dir), engines.load_report()
    finally:
        engines._plugins[:], engines._load_report[:] = saved


@pytest.mark.parametrize("folder", ["empty", "missing"])
def test_starts_with_zero_plugins(tmp_path, folder):
    user_dir = tmp_path / "plugins"
    if folder == "empty":
        user_dir.mkdir()
    loaded, report = _fresh_load(str(user_dir))
    assert [p.id for p in loaded] == ["unity", "generic"]
    assert all(e.ok for e in report)


def test_bad_plugin_does_not_stop_startup(tmp_path):
    (tmp_path / "broken.py").write_text("raise SystemExit('boom')\n")
    loaded, report = _fresh_load(str(tmp_path))
    assert [p.id for p in loaded] == ["unity", "generic"]
    assert [e.origin for e in report if not e.ok] == ["broken.py"]


def test_default_font_preview_uses_raw(tmp_path):
    """Engines without their own font code (Source, Unreal ...) still preview TTF/OTF fonts."""
    ttf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
    if not os.path.isfile(ttf):
        pytest.skip("no system TTF font to test with")
    session = GameSession(EnginePlugin(), str(tmp_path))
    session.raw = lambda asset: open(ttf, "rb").read()
    img = session.font(engines.sdk.Asset("font", "arial", "arial", ext="ttf"))
    assert img.width > 500 and img.height > 100
