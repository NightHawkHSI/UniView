"""Settings: typed fields, lenient load, lossless save, atomic JSON helpers."""

import json
import logging
import os

import pytest

from uniview import projects, settings, util


def load(tmp_path, data):
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return settings.Settings.load(path), path


def test_defaults_when_missing(tmp_path):
    s = settings.Settings.load(str(tmp_path / "none.json"))
    assert s.model_format == "obj" and s.volume == 0.7 and s.view == "list"
    assert s.option("hide_skybox") is True
    assert not os.path.exists(tmp_path / "none.json.bad")


def test_load_real_file_shape(tmp_path):
    s, _ = load(tmp_path, {"last_dir": "C:\\x", "home_group": "version", "view": "grid",
                           "opt_hide_skybox": False, "opt_hide_tool_surfaces": True, "volume": 1})
    assert s.last_dir == "C:\\x" and s.home_group == "version" and s.view == "grid"
    assert s.option("hide_skybox") is False and s.option("hide_tool_surfaces") is True
    assert s.volume == 1.0 and isinstance(s.volume, float)


def test_bad_values_fall_back_and_log(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="viewer"):
        s, _ = load(tmp_path, {"volume": 5, "view": "tiles", "autoplay": "yes", "home_engine": None,
                               "opt_hide_lods": 0, "last_dir": "ok"})
    assert s.volume == 0.7 and s.view == "list" and s.autoplay is False and s.home_engine == ""
    assert s.option("hide_lods") is True
    assert s.last_dir == "ok"  # one bad value doesn't cost the others
    assert caplog.text.count("Ignoring") == 5


def test_corrupt_file_backed_up(tmp_path):
    s, path = load(tmp_path, '{"view": "grid",')
    assert s.view == "list"
    assert open(path + ".bad", encoding="utf-8").read() == '{"view": "grid",'


def test_not_an_object(tmp_path):
    s, _ = load(tmp_path, "[1, 2]")
    assert s.view == "list"


def test_unknown_keys_survive_save(tmp_path):
    s, path = load(tmp_path, {"future_thing": {"a": 1}, "view": "grid"})
    s.autoplay = True
    s.save()
    data = json.load(open(path, encoding="utf-8"))
    assert data["future_thing"] == {"a": 1} and data["view"] == "grid" and data["autoplay"] is True
    assert "path" not in data and "extra" not in data and "options" not in data


def test_roundtrip(tmp_path):
    path = str(tmp_path / "s.json")
    s = settings.Settings.load(path)
    s.volume, s.model_format, s.blender_path = 0.25, "glb", "C:/b/blender.exe"
    s.set_option("hide_unreadable", False)
    s.save()
    t = settings.Settings.load(path)
    assert t.to_json() == s.to_json()
    assert json.load(open(path))["opt_hide_unreadable"] is False  # flat file, readable by older versions


@pytest.mark.parametrize("name, value, error", [
    ("volum", 0.5, AttributeError),
    ("volume", "0.5", TypeError),
    ("volume", 1.5, ValueError),
    ("autoplay", 1, TypeError),
    ("home_engine", None, TypeError),
    ("model_format", "fbx", ValueError),
    ("view", "tiles", ValueError),
])
def test_assignment_is_checked(name, value, error):
    s = settings.Settings(path="unused")
    with pytest.raises(error):
        setattr(s, name, value)


def test_set_option_checked():
    s = settings.Settings(path="unused")
    with pytest.raises(TypeError):
        s.set_option("hide_lods", "no")


def test_save_failure_is_logged_not_raised(tmp_path, caplog):
    s = settings.Settings(path=str(tmp_path / "missing_dir" / "s.json"))
    with caplog.at_level(logging.WARNING, logger="viewer"):
        s.save()
    assert "Could not save settings" in caplog.text


def test_write_json_never_truncates(tmp_path):
    path = str(tmp_path / "p.json")
    util.write_json(path, [{"a": 1}])
    with pytest.raises(TypeError):
        util.write_json(path, [{"a": object()}])
    assert json.load(open(path)) == [{"a": 1}]
    assert not os.path.exists(path + ".tmp")


def test_project_store_corrupt_file_kept(tmp_path):
    path = str(tmp_path / "projects.json")
    open(path, "w").write('[{"path": "C:/x"')
    store = projects.ProjectStore(path)
    assert store.projects == []
    assert open(path + ".bad").read() == '[{"path": "C:/x"'


def test_loading_screen_sound_config(tmp_path):
    from uniview.ui.loading_screen import sound_config
    s = settings.Settings.load(str(tmp_path / "none.json"))
    files = sound_config(s)["files"]
    assert set(files) == {"pop", "drop", "bits"} and all(files.values())  # Sounds/ ships with the app
    s.set_option("sound_bits", False)
    assert sound_config(s)["files"]["bits"] == "" and sound_config(s)["files"]["pop"]
    s.loading_sounds = False
    assert not any(sound_config(s)["files"].values())
    s.sound_volume = 1
    assert sound_config(s)["volume"] == 1.0
    with pytest.raises(ValueError):
        s.sound_volume = 1.5


def test_memory_settings_round_trip_and_ranges(tmp_path):
    import pytest

    from uniview.settings import Settings
    path = str(tmp_path / "settings.json")
    s = Settings(path=path)
    s.ram_budget_gb, s.ram_warn_pct, s.ram_slow_pct = 6, 80, 75  # an int budget is taken as float
    s.save()
    loaded = Settings.load(path)
    assert (loaded.ram_budget_gb, loaded.ram_warn_pct, loaded.ram_slow_pct) == (6.0, 80, 75)
    with pytest.raises(ValueError):
        s.ram_warn_pct = 150
    with pytest.raises(ValueError):
        s.ram_budget_gb = -1.0


def test_ram_budget_and_throttle_thresholds():
    from uniview.perf import Throttle, ram_budget
    gb = 2**30
    assert ram_budget(0, 16 * gb) == 8 * gb          # Auto: half
    assert ram_budget(0, 6 * gb) == 4 * gb           # ...at least 4 GB
    assert ram_budget(0, 3 * gb) == 3 * gb           # ...but never more than installed
    assert ram_budget(10, 16 * gb) == 10 * gb
    assert ram_budget(32, 16 * gb) == 16 * gb
    t = Throttle(clock=lambda: 0.0)
    t.configure(70)
    assert (t.MEM_EASE, t.MEM_SEVERE) == (70, 78)
    assert t.report_memory(50, 72) and t.level == 1 and "allocated" in t.reason()
    assert t.report_memory(50, 101) and t.level == 2
    t.report_memory(80, 10)  # still severe (system 80% >= 78%), now because of the system
    assert t.level == 2 and t.reason() == "memory is almost full"
    assert t.report_memory(40, None) and t.level == 0
