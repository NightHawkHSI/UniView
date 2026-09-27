"""Every engine plugin loads, and the plugin template (what contributors copy) actually works."""

import importlib.util
import os

import pytest
from PIL import Image

import engines
from engines.sdk import API_VERSION, EnginePlugin, GameSession, Progress

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(ROOT, "plugins")


def test_all_plugins_load():
    engines.load_plugins(PLUGINS_DIR)
    failed = [(name, origin, msg) for name, origin, ok, msg in engines.load_report() if not ok]
    assert not failed, failed
    ids = [p.id for p in engines.plugins()]
    assert len(ids) == len(set(ids)), f"duplicate plugin ids: {ids}"
    for builtin in ("unity", "source", "source2", "unreal", "fallout", "generic"):
        assert builtin in ids


@pytest.mark.parametrize("plugin", engines.load_plugins(PLUGINS_DIR), ids=lambda p: p.id)
def test_plugin_contract(plugin):
    assert isinstance(plugin, EnginePlugin)
    assert plugin.id and plugin.name and plugin.version
    assert plugin.detect(os.path.join(ROOT, "no such folder")) in (0, None)


def test_default_font_preview_uses_raw(tmp_path):
    """Engines without their own font code (Source, Unreal ...) still preview TTF/OTF fonts."""
    ttf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
    if not os.path.isfile(ttf):
        pytest.skip("no system TTF font to test with")
    session = GameSession(EnginePlugin(), str(tmp_path))
    session.raw = lambda asset: open(ttf, "rb").read()
    img = session.font(engines.sdk.Asset("font", "arial", "arial", ext="ttf"))
    assert img.width > 500 and img.height > 100


def load_template():
    spec = importlib.util.spec_from_file_location("uniview_template_test", os.path.join(PLUGINS_DIR, "_template.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PLUGIN


def test_template_plugin_opens_a_folder(tmp_path):
    plugin = load_template()
    assert getattr(plugin, "api_version", API_VERSION) == API_VERSION
    Image.new("RGB", (8, 4), (0, 128, 255)).save(tmp_path / "wall.png")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "cube.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "data.bin").write_bytes(b"\0" * 16)

    assert plugin.detect(str(tmp_path)) > 0
    session = plugin.open(str(tmp_path), Progress())
    assert isinstance(session, GameSession)
    kinds = {a.path: a.kind for a in session.assets}
    assert kinds == {"wall.png": "texture", "models/cube.obj": "model", "notes.txt": "text"}
    tex = next(a for a in session.assets if a.kind == "texture")
    assert session.image(tex).size == (8, 4)
    model = next(a for a in session.assets if a.kind == "model")
    md = session.mesh(model)
    assert len(md.points) == 3 and md.triangle_count == 1
