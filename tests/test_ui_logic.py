"""Logic pulled out of the widgets: model display (textured parts, info rows) and the projects page."""

import logging
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import engines
from engines.sdk import ALBEDO, Asset, Material, MeshData, TextureRef
from uniview import catalog, model_display as md_


def tex(key):
    return TextureRef("_MainTex", key, Asset("texture", key, key), ALBEDO)


def mesh(n_sub):
    pts = [[i, 0, 0] for i in range(3 * n_sub)]
    return MeshData(pts, [[[3 * i, 3 * i + 1, 3 * i + 2]] for i in range(n_sub)])


# ---------------------------------------------------------------------------- model display

def test_texture_groups_merge_by_texture_and_color():
    m = mesh(4)
    mats = [Material("a", [tex("t1")]), Material("b", [tex("t1")]), Material("c", color=(2, 0.5, -1)),
            Material("d")]
    groups = md_.texture_groups(m, mats)
    assert len(groups) == 3
    t1 = next(g for g in groups if g[0] is not None)
    assert t1[0].key == "t1" and t1[2].shape == (2, 3)  # submeshes 0 and 1 drawn as one part
    colored = next(g for g in groups if g[1] is not None)
    assert colored[1] == (1.0, 0.5, 0.0)
    assert next(g for g in groups if g[0] is None and g[1] is None)[2].shape == (1, 3)


def test_texture_groups_split_by_alpha():
    m = mesh(3)
    mats = [Material("leaf", [tex("t1")], alpha_mode="mask", alpha_cutoff=0.3), Material("bark", [tex("t1")]),
            Material("glass", color=(0.2, 0.4, 1.0, 0.25), alpha_mode="blend")]
    groups = md_.texture_groups(m, mats)
    assert sorted(str(g[3]) for g in groups) == ["('blend', 0.25)", "('mask', 0.3)", "None"]
    assert all(g[2].shape == (1, 3) for g in groups)  # same texture, but cutout and opaque stay apart


def test_texture_groups_without_materials():
    assert md_.texture_groups(mesh(2), []) == []


def test_texture_loader_caches_scales_and_survives_failures():
    calls = []

    def image(a):
        calls.append(a.key)
        if a.key == "bad":
            raise ValueError("nope")
        return Image.new("RGB", (2048, 1024))

    session = SimpleNamespace(image=image)
    small = md_.texture_loader(session, 40)
    big = md_.texture_loader(session, 2)
    assert small(Asset("texture", "a", "a")).size == (256, 128)
    assert small(Asset("texture", "a", "a")).size == (256, 128)
    assert calls == ["a"]  # cached
    assert big(Asset("texture", "a", "a")).size == (1024, 512)
    assert small(Asset("texture", "bad", "bad")) is None and small(None) is None


@pytest.mark.parametrize("kind, name, ref, place", [
    ("scene", "x", None, True), ("model", "Terrain: island", None, True), ("model", "de_dust", "maps/de_dust.BSP", True),
    ("model", "crate", "crate.mdl", False), ("model", "crate", 42, False),
])
def test_is_place(kind, name, ref, place):
    assert md_.is_place(Asset(kind, name, 1, ref=ref)) is place


def test_model_info_rows(caplog):
    session = SimpleNamespace(describe=lambda a: [("Shader", "<b>Standard</b>")])
    rows = md_.model_info_rows(session, Asset("model", "m", 1), mesh(2), 1234, 2, ["UV0"], favorite=True)
    assert rows[0].startswith("<span") and "1,234" in rows[1]
    assert "&lt;b&gt;Standard&lt;/b&gt;" in rows[-1]  # plugin text is escaped
    broken = SimpleNamespace(describe=lambda a: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="viewer"):
        rows = md_.model_info_rows(broken, Asset("model", "m", 1), mesh(1), 3, 1, [])
    assert len(rows) == 4 and "UV sets:</b> none" in rows[3] and "describe() failed" in caplog.text


# ---------------------------------------------------------------------------- projects page

@pytest.fixture(scope="module", autouse=True)
def plugins():
    engines.load_plugins()


def game(name, path=None, **kw):
    return {"name": name, "path": path or f"C:/Games/{name}", **kw}


COMPAT = {"muck": {"status": "works"}, "tf2": {"status": "broken"}}


def test_project_matches_words_and_fields():
    g = game("Muck", engine="unity", engine_version="2019.4.40f1", engine_detail="Mono, 64-bit",
             tags=["Survival"], notes="co-op")
    for terms in ([], ["muck"], ["co-op"], ["#survival"], ["tag:surv"], ["engine:unity"], ["version:2019"],
                  ["unity:2019.4"], ["backend:mono"], ["status:works"]):
        assert catalog.project_matches(g, terms, COMPAT), terms
    for terms in (["valheim"], ["tag:rpg"], ["engine:source"], ["version:2020"], ["status:broken"],
                  ["muck", "valheim"]):
        assert not catalog.project_matches(g, terms, COMPAT), terms


def test_project_shown_filters():
    tagged = game("A", tags=["x"], engine="unity")
    plain = game("B")
    assert catalog.project_shown(plain, catalog.UNTAGGED, "", [], {})
    assert not catalog.project_shown(tagged, catalog.UNTAGGED, "", [], {})
    assert catalog.project_shown(tagged, "x", "unity", [], {})
    assert not catalog.project_shown(tagged, "y", "", [], {})
    assert catalog.project_shown(plain, "", catalog.UNKNOWN_ENGINE, [], {})
    assert not catalog.project_shown(tagged, "", catalog.UNKNOWN_ENGINE, [], {})


def test_group_projects_by_tag_and_status(tmp_path):
    a, b, c = game("A", tags=["zeta", "Alpha"]), game("B", tags=["alpha"]), game("C")
    by_tag = catalog.group_projects([a, b, c], "tag", {}, lambda p: False)
    assert [(t, [p["name"] for p in ps]) for t, ps in by_tag] == [
        ("#Alpha", ["A"]), ("#alpha", ["B"]), ("#zeta", ["A"]), ("Untagged", ["C"])]
    games = [game("x", path="D:/g/Muck"), game("y", path="D:/g/TF2"), game("z", path="D:/g/Other")]
    by_status = catalog.group_projects(games, "status", COMPAT, lambda p: False)
    assert [t for t, _ in by_status] == ["\u2714 Works well", "\u2716 Doesn't work", "Not rated yet"]


def test_group_projects_by_state(tmp_path):
    loaded, idle, missing = (game("L", path=str(tmp_path)), game("I", path=str(tmp_path) + "/.."),
                             game("M", path=str(tmp_path / "gone")))
    groups = catalog.group_projects([missing, idle, loaded], "state", {}, lambda p: p == str(tmp_path))
    assert [t for t, _ in groups] == ["Loaded", "Not loaded", "Folder missing"]


def test_group_projects_by_engine_unknown_last():
    groups = catalog.group_projects([game("u"), game("s", engine="source"), game("n", engine="unity")],
                                    "engine", {}, lambda p: False)
    assert [t for t, _ in groups][-1] == "Unknown engine"
    assert np.all([ps for _t, ps in groups])
