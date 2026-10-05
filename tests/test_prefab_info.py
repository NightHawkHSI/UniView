"""Structure view of prefabs with nothing to draw in 3D."""

from engines.sdk import Asset
from uniview import prefab_info


def nodes():
    return [
        {"name": "Crosshair", "parent": -1, "active": True, "pos": [0, 0, 0], "scale": [1, 1, 1],
         "components": [{"type": "MonoBehaviour", "script": "UnityEngine.UI.Image", "props": [
             {"p": "m_Sprite", "t": "ref", "asset": "s1", "kind": "sprite"},
             {"p": "m_Material", "t": "ref", "asset": "material:f:9", "kind": "material"},
             {"p": "m_Color.r", "t": "f", "v": 0.5},
             {"p": "m_RaycastTarget", "t": "b", "v": 1}]}]},
        {"name": "Dot", "parent": 0, "active": False, "pos": [1, 2, 3], "scale": [1, 1, 1],
         "components": [{"type": "AudioSource", "props": [
             {"p": "m_audioClip", "t": "ref", "asset": "a1", "kind": "file"},
             {"p": "m_Target", "t": "ref", "node": 0, "cls": "Transform"}]}]},
        {"name": "Child", "parent": 1, "active": True, "pos": [0, 0, 0], "scale": [1, 1, 1]},
    ]


def assets():
    return {"s1": Asset("sprite", "UI/rivet", "k1", uid="s1"), "a1": Asset("audio", "click", "k2", uid="a1"),
            "t1": Asset("texture", "atlas", "k3", uid="t1")}


def test_visible_flags_follow_parents():
    assert prefab_info.visible_flags(nodes()) == [True, False, False]


def test_used_assets_ranked_and_material_textures_followed():
    used = prefab_info.used_assets(nodes(), assets(), lambda uid: [assets()["t1"]] if uid == "material:f:9" else [])
    assert [a.uid for a in used] == ["s1", "t1", "a1"]


def test_node_text_labels_refs_and_values():
    text = prefab_info.node_text(nodes(), 1, assets(), prefab_info.visible_flags(nodes()))
    assert "inactive" in text and "AudioSource" in text
    assert "m_audioClip = → click [audio]" in text
    assert "m_Target = → Crosshair (Transform)" in text
    root = prefab_info.node_text(nodes(), 0, assets())
    assert "UnityEngine.UI.Image" in root and "m_Color.r = 0.5" in root and "m_RaycastTarget = true" in root


def test_components_mark_scripts_and_keep_referenced_assets():
    comps = prefab_info.components(nodes()[0], nodes(), assets())
    assert [(c["title"], c["script"]) for c in comps] == [("UnityEngine.UI.Image", True)]
    sprite_row = comps[0]["rows"][0]
    assert sprite_row[0] == "m_Sprite" and sprite_row[2].uid == "s1"
    audio = prefab_info.components(nodes()[1], nodes(), assets())[0]
    assert audio["script"] is False and audio["rows"][1][2] is None  # ref to a node, not an asset


def test_scripts_listed_once():
    assert prefab_info.scripts(nodes() + nodes()) == ["UnityEngine.UI.Image"]


def test_summary_and_component_names():
    assert prefab_info.component_names(nodes()[0]) == ["Image"]
    assert prefab_info.summary(nodes()).startswith("3 object(s)")
