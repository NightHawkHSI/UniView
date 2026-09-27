"""Game materials -> Unity .mat: shader choice, transparency, property mapping, YAML."""

import re
from types import SimpleNamespace

import pytest

from uniview import unity_materials as um


def tex(name):
    return SimpleNamespace(uid=f"tex:{name}", key=name, name=name)


def details(shader="", textures=(), colors=None, floats=None, keywords=(), queue=-1, tags=None, name="Mat"):
    return {"name": name, "shader": shader, "textures": list(textures), "colors": colors or {},
            "floats": floats or {}, "keywords": list(keywords), "queue": queue, "tags": tags or {}}


def guid(asset):
    return None if asset.key == "missing" else "a" * 32


@pytest.mark.parametrize("d, expected", [
    (details("Robocraft/Reflective/Diffuse - Colored"), "opaque"),
    (details("Custom/Translucent"), "transparent"),
    (details("Robocraft/Component/Shield_Glow"), "transparent"),
    (details("Anything", queue=3000), "transparent"),
    (details("Anything", tags={"RenderType": "Transparent"}), "transparent"),
    (details("Universal Render Pipeline/Lit", floats={"_Surface": 1}), "transparent"),
    (details("Standard-ish", keywords=["_ALPHAPREMULTIPLY_ON"]), "transparent"),
    (details("Anything", queue=2450), "cutout"),
    (details("Nature/Tree Cutout"), "cutout"),
    (details("Universal Render Pipeline/Lit", floats={"_AlphaClip": 1}), "cutout"),
    (details("Anything", queue=2000), "opaque"),
])
def test_surface(d, expected):
    assert um.surface(d) == expected


@pytest.mark.parametrize("shader, textures, expected", [
    ("Standard", (), ("Standard", True)),
    ("Legacy Shaders/Diffuse", (), ("Legacy Shaders/Diffuse", True)),
    ("Custom/Rock", (), ("Standard", False)),
    ("Mobile/Particles/Additive", (), ("Legacy Shaders/Particles/Additive", False)),
    ("FX/Particle Smoke", (), ("Legacy Shaders/Particles/Alpha Blended", False)),
    ("Custom/Unlit Scroll", [("_MainTex", tex("a"), (1, 1), (0, 0))], ("Unlit/Texture", False)),
    ("Custom/Unlit Flat", (), ("Unlit/Color", False)),
])
def test_pick_shader(shader, textures, expected):
    assert um.pick_shader(details(shader, textures)) == expected


def test_convert_urp_lit_to_standard_fade():
    d = details("Universal Render Pipeline/Lit", floats={"_Surface": 1, "_Smoothness": 0.7, "_Metallic": 0.3},
                textures=[("_BaseMap", tex("albedo"), (2, 2), (0.5, 0)), ("_BumpMap", tex("n"), (1, 1), (0, 0)),
                          ("_EmissionMap", tex("missing"), (1, 1), (0, 0))],
                colors={"_BaseColor": (1, 0.5, 0, 0.4), "_EmissionColor": (0, 0, 0, 1)})
    m = um.convert(d, guid)
    assert m["shader_id"] == 46 and m["queue"] == 3000 and m["tags"] == {"RenderType": "Transparent"}
    assert m["floats"]["_Mode"] == 2 and m["floats"]["_ZWrite"] == 0 and m["floats"]["_Glossiness"] == 0.7
    assert m["textures"]["_MainTex"] == ("a" * 32, (2, 2), (0.5, 0))
    assert "_BumpMap" in m["textures"] and "_NORMALMAP" in m["keywords"] and "_ALPHABLEND_ON" in m["keywords"]
    assert "_EmissionMap" not in m["textures"] and "_EMISSION" not in m["keywords"]  # not exported, black glow
    assert m["colors"]["_Color"] == (1, 0.5, 0, 0.4)


def test_convert_builtin_copies_everything():
    d = details("Standard", floats={"_Mode": 1, "_Weird": 3.5}, keywords=["_ALPHATEST_ON"], queue=2450,
                textures=[("_DetailAlbedoMap", tex("d"), (4, 4), (0, 0))], tags={"RenderType": "TransparentCutout"})
    m = um.convert(d, guid)
    assert m["floats"] == {"_Mode": 1, "_Weird": 3.5} and m["keywords"] == ["_ALPHATEST_ON"]
    assert m["queue"] == 2450 and "_DetailAlbedoMap" in m["textures"]


def test_convert_additive_particle_tint():
    m = um.convert(details("Mobile/Particles/Additive", colors={"_Color": (1, 0, 0, 1)}), guid)
    assert m["shader_id"] == 200 and m["colors"] == {"_TintColor": (1, 0, 0, 0.5)}


def test_convert_emission():
    m = um.convert(details("Custom/Glow", colors={"_EmissionColor": (0, 2, 0, 1)}), guid)
    assert "_EMISSION" in m["keywords"] and m["colors"]["_EmissionColor"] == (0, 2, 0, 1)


def test_mat_yaml():
    m = um.convert(details("Custom/Translucent", name='Glass "blue"', colors={"_Color": (0, 0, 1, 0.5)},
                           textures=[("_MainTex", tex("g"), (1, 1), (0, 0))]), guid)
    text = um.mat_yaml(m)
    assert text.startswith("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!21 &2100000\nMaterial:\n")
    assert 'm_Name: "Glass \\"blue\\""' in text
    assert "m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}" in text
    assert "m_CustomRenderQueue: 3000" in text and "    RenderType: \"Transparent\"" in text
    assert f"        m_Texture: {{fileID: 2800000, guid: {'a' * 32}, type: 3}}" in text
    assert "    - _Color: {r: 0, g: 0, b: 1, a: 0.5}" in text and "    - _Mode: 2" in text
    assert re.search(r"m_ShaderKeywords: _ALPHABLEND_ON\n", text)


def test_mat_yaml_empty_sections():
    text = um.mat_yaml(um.convert(details("Unlit/Color"), guid))
    assert "m_TexEnvs: []" in text and "stringTagMap: {}" in text and "m_ShaderKeywords: \n" in text


def test_number_format():
    assert [um._num(v) for v in (1.0, 0.5, float("inf"), float("-inf"), float("nan"), 3)] ==         ["1", "0.5", "Infinity", "-Infinity", "NaN", "3"]
