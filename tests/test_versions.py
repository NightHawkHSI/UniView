"""Game version diff: snapshots and comparing them."""

from engines.sdk import Asset
from uniview import versions


def test_entries_and_diff():
    def rows(*items):
        return versions.entries([(Asset(kind, name, name, uid=name, size=size, path=path), digest)
                                 for kind, name, path, size, digest in items])
    old = rows(("texture", "a", "", 10, b"\x01"), ("texture", "b", "", 20, b"\x02"), ("text", "c", "data/c.txt", 5, b"\x03"),
               ("font", "f", "", 1, b"\x09"), ("font", "f", "", 1, b"\x09"))
    new = rows(("texture", "a", "", 10, b"\x01"), ("texture", "b", "", 25, b"\x22"), ("text", "d", "", 7, b"\x04"),
               ("font", "f", "", 1, b"\x09"), ("font", "f", "", 1, b"\x09"), ("model", "m", "", 3, None))
    assert old[2][1] == "text:c@data/c.txt" and versions.key_name(old[2][1]) == "c"  # name + where it's stored
    d = versions.diff(old, new)
    assert d["changed"] == [("texture", "texture:b", 20, 25)]
    assert d["added"] == [("text", "text:d", None, 7)]
    assert d["removed"] == [("text", "text:c@data/c.txt", 5, None)]
    assert d["same"] == 2  # 'a', and the two identical copies of 'f' count once


def test_save_list_load(tmp_path):
    p1 = versions.save_snapshot("Game", [["text", "text:x", 1, "aa"]], "patch 1", folder=str(tmp_path))
    snaps = versions.list_snapshots("Game", folder=str(tmp_path))
    assert snaps[0][0] == p1 and snaps[0][1]["label"] == "patch 1" and snaps[0][1]["count"] == 1
    assert versions.load_snapshot(p1)["assets"] == [["text", "text:x", 1, "aa"]]


def test_steam_build_id(tmp_path):
    steamapps = tmp_path / "steamapps"
    (steamapps / "common" / "My Game").mkdir(parents=True)
    (steamapps / "appmanifest_123.acf").write_text('"AppState"\n{\n "installdir" "My Game"\n "buildid" "4567"\n}')
    assert versions.steam_build_id(str(steamapps / "common" / "My Game")) == "4567"
    assert versions.steam_build_id(str(tmp_path)) == ""
