"""Where exported assets go: original paths, capitals, next-to-owner and by-type guesses."""

import os
from types import SimpleNamespace

from uniview.unity_layout import BY_TYPE, NEXT_TO, REAL, CaseMap, Layout, original_path, prefab_uses, target


def asset(kind, name, path="", uid=None):
    return SimpleNamespace(kind=kind, name=name, path=path, uid=uid or f"{kind}:{name}:{path}", source="x")


def test_original_path():
    assert original_path("assets/art/gun.png") == "art/gun.png"
    assert original_path("Assets/Scenes/Main.unity") == "Scenes/Main.unity"
    assert original_path("prefabs/player") == "Resources/prefabs/player"  # Resources index
    assert original_path("Resources/prefabs/player") == "Resources/prefabs/player"  # as the engine marks them
    assert original_path("packages/com.unity.ugui/x.png") == ""
    assert original_path("") == ""


def test_case_map_restores_folder_capitals():
    case = CaseMap(["Assets/Art/Weapons/Gun.unity"])
    assert case.fix("art/weapons/guns") == "Art/Weapons/guns"
    assert case.fix("other/thing") == "other/thing"


def test_real_place_uses_asset_name_capitals_and_container_folders():
    layout = Layout([asset("scene", "Scene: Main", "Assets/Art/Main.unity")])
    assert layout.real_place(asset("texture", "GunAlbedo", "assets/art/gunalbedo.png")) == ("Art", "GunAlbedo")
    # a mesh inside a model file: a folder named after the file
    assert layout.real_place(asset("model", "Barrel", "assets/art/gun.fbx")) == ("Art/gun", "Barrel")
    assert layout.real_place(asset("scene", "Prefab: Door", "assets/prefabs/door.prefab")) == ("prefabs", "Door")
    assert layout.real_place(asset("texture", "x")) is None


def test_place_next_to_single_owner_else_by_type():
    layout = Layout()
    one, shared, lonely = asset("texture", "a"), asset("texture", "b"), asset("audio", "c")
    layout.use(one.uid, "Props/Crate")
    layout.use(shared.uid, "Props/Crate")
    layout.use(shared.uid, "Props/Barrel")
    assert layout.place(one) == ("Props/Crate", "a", NEXT_TO)
    assert layout.place(shared) == ("Textures", "b", BY_TYPE)
    assert layout.place(lonely) == ("Audio", "c", BY_TYPE)
    assert layout.place(one) == ("Props/Crate", "a", NEXT_TO)  # repeated: same answer, counted once
    assert layout.counts == {REAL: 0, NEXT_TO: 1, BY_TYPE: 2}
    assert "1 are next to" in layout.report() and "0 of 3" in layout.report()


def test_model_owner_wins_over_prefab_owner():
    layout = Layout()
    tex = asset("texture", "wood")
    layout.use(tex.uid, "Prefabs")
    layout.use(tex.uid, "Prefabs/Other")
    layout.use(tex.uid, "Models/Crate", model=True)
    assert layout.place(tex)[0] == "Models/Crate"
    assert layout.material_folder("material:x:1") == "Materials"
    layout.use("material:x:1", "Props")
    assert layout.material_folder("material:x:1") == "Props/Materials"


def test_target_unique_names(tmp_path):
    used = set()
    a = target(str(tmp_path), "Textures", "wall", "png", used)
    b = target(str(tmp_path), "textures", "Wall", "png", used)
    c = target(str(tmp_path), "Textures", "wall", "jpg", used)
    rel = [os.path.relpath(p, tmp_path).replace("\\", "/") for p in (a, b, c)]
    assert rel == ["Textures/wall.png", "textures/Wall_2.png", "Textures/wall.jpg"]


def test_prefab_uses():
    nodes = [{"mesh": "m:1", "materials": ["material:f:1", None]},
             {"mesh": None, "batch": {"mesh": "m:2", "material_uids": ["material:f:2"]},
              "components": [{"type": "AudioSource", "props": [{"p": "m_audioClip", "asset": "a:1"}]}]}]
    used = prefab_uses(nodes, lambda mat: ["t:" + mat[-1]])
    assert used == {"m:1", "material:f:1", "t:1", "m:2", "material:f:2", "t:2", "a:1"}
