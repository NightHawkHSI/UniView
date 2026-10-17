"""The Plugins tab's logic: reading plugins.json, refusing unsafe entries, checked downloads, rows."""

import hashlib
from types import SimpleNamespace

import pytest

from uniview import plugin_index as pi
from uniview import plugin_manager

PLUGIN_SRC = b"from engines.sdk import EnginePlugin\n\nclass P(EnginePlugin):\n    id = 'demo'\n\nPLUGIN = P()\n"
HELPER_SRC = b"X = 1\n"


def _file(path, data):
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _index(**over):
    entry = {"file": "demo.py", "id": "demo", "name": "Demo", "version": "1.2", "api_version": 1,
             "description": "A demo", "games": ["Demo Game"],
             "files": [_file("demo.py", PLUGIN_SRC), _file("demo_formats/__init__.py", HELPER_SRC)]}
    entry.update(over)
    return {"format": 1, "base_url": "https://example.com/p/", "plugins": [entry]}


def test_parse_index():
    (a,) = pi.parse_index(_index())
    assert (a.file, a.id, a.version, a.games) == ("demo.py", "demo", "1.2", ["Demo Game"])
    assert [f["path"] for f in a.files] == ["demo.py", "demo_formats/__init__.py"]
    assert pi.base_url(_index()) == "https://example.com/p/"
    assert pi.base_url({}, "https://h/x/plugins.json") == "https://h/x/"


@pytest.mark.parametrize("path", ["../evil.py", "/abs.py", "C:/x.py", "a\\b.py", "sub/../../x.py", "run.exe",
                                  "_removed/x.py", "a//b.py", "./a.py"])
def test_unsafe_paths_are_refused(path):
    bad = _index(files=[_file("demo.py", PLUGIN_SRC), _file(path, b"x")])
    with pytest.raises(pi.PluginIndexError):
        pi.parse_index(bad)


def test_entry_file_must_be_top_level_and_listed():
    with pytest.raises(pi.PluginIndexError):
        pi.parse_index(_index(file="sub/demo.py"))
    with pytest.raises(pi.PluginIndexError):
        pi.parse_index(_index(files=[_file("other.py", PLUGIN_SRC)]))
    with pytest.raises(pi.PluginIndexError):
        pi.parse_index({"format": 99, "plugins": []})


def test_download_checks_hashes():
    (a,) = pi.parse_index(_index())
    served = {"https://example.com/p/demo.py": PLUGIN_SRC, "https://example.com/p/demo_formats/__init__.py": HELPER_SRC}
    files = pi.download(a, "https://example.com/p/", fetch=served.__getitem__)
    assert files == [("demo.py", PLUGIN_SRC), ("demo_formats/__init__.py", HELPER_SRC)]
    served["https://example.com/p/demo.py"] = PLUGIN_SRC + b"# tampered\n"
    with pytest.raises(plugin_manager.PluginInstallError):
        pi.download(a, "https://example.com/p/", fetch=served.__getitem__)


def test_download_then_install(tmp_path):
    (a,) = pi.parse_index(_index())
    served = {"https://example.com/p/demo.py": PLUGIN_SRC, "https://example.com/p/demo_formats/__init__.py": HELPER_SRC}
    files = pi.download(a, "https://example.com/p/", fetch=served.__getitem__)
    assert plugin_manager.install(files, str(tmp_path)) == ["demo.py"]
    assert (tmp_path / "demo_formats" / "__init__.py").read_bytes() == HELPER_SRC
    assert pi.files_in_use(str(tmp_path), files) == ["demo.py", "demo_formats/__init__.py"]


def test_plain_http_is_refused():
    with pytest.raises(ValueError):
        pi._get("http://example.com/plugins.json")


def test_version_key():
    assert pi.version_key("1.10") > pi.version_key("1.9") > pi.version_key("1.2")
    assert pi.version_key("v2.0") == pi.version_key("2.0")


def _report(origin, version, ok=True, disabled=False):
    plugin = SimpleNamespace(version=version) if ok else None
    return SimpleNamespace(origin=origin, ok=ok, disabled=disabled, plugin=plugin)


def test_rows():
    available = pi.parse_index(_index()) + pi.parse_index(_index(file="new.py", id="new", name="New",
                                                                 files=[_file("new.py", PLUGIN_SRC)]))
    report = [SimpleNamespace(origin="built-in", ok=True, disabled=False, plugin=None),
              _report("demo.py", "1.1"), _report("mine.py", "0.1")]
    rows = {r.file: r for r in pi.rows(available, report)}
    assert rows["demo.py"].state == "update" and rows["demo.py"].installed == "1.1"
    assert rows["new.py"].state == "not installed"
    assert rows["mine.py"].state == "local" and rows["mine.py"].available == ""
    rows = {r.file: r for r in pi.rows(available, report, installed_now={"new.py": "1.2"})}
    assert rows["new.py"].pending == "installed"
    too_new = pi.parse_index(_index(api_version=9))
    assert pi.rows(too_new, [])[0].state == "needs newer UniView"


def test_missing_plugin():
    assert pi.missing_plugin({"plugin": "wakfu"}, ["unity", "generic"]) == "wakfu"
    assert pi.missing_plugin({"plugin": "wakfu"}, ["unity", "wakfu"]) is None
    assert pi.missing_plugin({}, ["unity"]) is None and pi.missing_plugin(None, []) is None
