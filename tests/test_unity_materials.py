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
    (details("Mobile/Particles/Additive"), "transparent"),
])
def test_surface(d, expected):
    assert um.surface(d) == expected


@pytest.mark.parametrize("d, expected", [
    # HDRP shader graph with alpha clipping on (Procelio robot parts)
    (details("Shader Graphs/TexturedMaterialShaderMain", floats={"_AlphaCutoffEnable": 1, "_AlphaCutoff": 0.3},
             queue=2475), ("mask", 0.3)),
    # URP opaque with a leftover Standard _Mode from an upgraded material
    (details("Universal Render Pipeline/Lit", floats={"_Surface": 0, "_Mode": 2}), ("opaque", 0.5)),
    (details("", floats={"_Mode": 2, "_Cutoff": 0.065}), ("blend", 0.065)),
    # Legacy additive: leftover HDRP/Standard floats don't count (Procelio LightningParticle 2)
    (details("Legacy Shaders/Particles/Additive", floats={"_SurfaceType": 0, "_SrcBlend": 1, "_DstBlend": 10}),
     ("add", 0.5)),
    (details("Universal Render Pipeline/Particles/Unlit", floats={"_Surface": 1, "_Blend": 2}), ("add", 0.5)),
    (details("HDRP/Unlit", floats={"_SurfaceType": 1, "_BlendMode": 1}), ("add", 0.5)),
    (details("FX/Glow", floats={"_SrcBlend": 5, "_DstBlend": 1}, queue=3000), ("add", 0.5)),
    # HDRP transparent alpha blend, premultiplied One/OneMinusSrcAlpha (Procelio OSC_eye)
    (details("HDRP/Unlit", floats={"_SurfaceType": 1, "_BlendMode": 0, "_SrcBlend": 1, "_DstBlend": 10},
             keywords=["_SURFACE_TYPE_TRANSPARENT"]), ("blend", 0.5)),
    (details("Custom/Grass Wind", floats={"_Cutoff": 0}), ("mask", 0.01)),
])
def test_material_alpha(d, expected):
    from engines.unity import material_alpha
    mode, cutoff = material_alpha(d["shader"], d["floats"], d["keywords"], d["queue"], d["tags"])
    assert (mode, round(cutoff, 3)) == expected


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


def test_shader_declared_transparency():
    from engines.unity import material_alpha, queue_value
    assert [queue_value(q) for q in ("Transparent", "Geometry+1", "AlphaTest-50", "2450", "", "Weird")] == \
        [3000, 2001, 2400, 2450, -1, -1]
    # Fallout Shelter's Underground/Dweller: nothing on the material, the shader says Transparent + alpha blend
    assert material_alpha("Underground/Dweller", {}, [], 3000, {"RenderType": "Transparent"}, (1, 10))[0] == "blend"
    assert material_alpha("Custom/Sprite", {}, [], -1, {}, (5, 10))[0] == "blend"   # blend state alone
    assert material_alpha("Custom/Glow", {}, [], -1, {}, (1, 1))[0] == "opaque"     # One One without transparency
    assert material_alpha("Custom/Glow", {}, [], 3000, {}, (5, 1))[0] == "add"
    assert material_alpha("Custom/Rock", {}, [], 2000, {"RenderType": "Opaque"}, (1, 0))[0] == "opaque"


def test_export_surface_uses_shader_render():
    from uniview.unity_materials import surface
    d = details("Underground/Dweller")
    assert surface(d) == "opaque"
    assert surface({**d, "shader_render": (3000, {"RenderType": "Transparent"}, (1, 10))}) == "transparent"
    assert surface({**d, "queue": 2000, "shader_render": (3000, {"RenderType": "Transparent"}, None)}) == "opaque"


def cube(name):
    return SimpleNamespace(uid=f"tex:{name}", key=name, name=name, ref=SimpleNamespace(type=SimpleNamespace(name="Cubemap")))


def test_sky_shaders():
    assert um.pick_shader(details("Skybox/Procedural")) == ("Skybox/Procedural", True)  # Unity's own: kept
    assert um.pick_shader(details("Nature/Terrain/Standard")) == ("Nature/Terrain/Standard", True)
    m = um.convert(details("Funly/Sky Studio/Skybox", textures=[("_MainTex", tex("t"), (1, 1), (0, 0)),
                                                                ("_StarsCube", cube("c"), (1, 1), (0, 0))],
                           floats={"_Exposure": 0.8}, colors={"_Tint": (1, 0, 0, 1)}), guid)
    assert m["shader"] == "Skybox/Cubemap" and m["shader_id"] == 103
    assert m["textures"]["_Tex"] == ("a" * 32, (1, 1), (0, 0))
    assert m["floats"]["_Exposure"] == 0.8 and m["colors"]["_Tint"] == (1, 0, 0, 1)
    m = um.convert(details("Custom/Gradient Sky", colors={"_TopColor": (0, 0, 1, 1), "_BottomColor": (0, 1, 0, 1)}),
                   guid)
    assert m["shader_id"] == 106 and m["colors"] == {"_SkyTint": (0, 0, 1, 1), "_GroundColor": (0, 1, 0, 1)}


def test_convert_urp_keeps_game_shader_and_props():
    d = details("Universal Render Pipeline/Lit", floats={"_Surface": 1, "_Smoothness": 0.7, "_QueueOffset": 2},
                textures=[("_BaseMap", tex("albedo"), (2, 2), (0.5, 0)), ("_BumpMap", tex("n"), (1, 1), (0, 0))],
                colors={"_BaseColor": (1, 0.5, 0, 0.4)}, keywords=["_NORMALMAP", "_SURFACE_TYPE_TRANSPARENT"],
                queue=3002, tags={"RenderType": "Transparent", "Custom": "x"})
    m = um.convert(d, guid, pipeline="urp")
    assert m["shader_id"] == 46  # the .mat still opens on Standard; the builder puts the URP shader back
    assert m["game_shader"] == "Universal Render Pipeline/Lit" and m["game_queue"] == 3002
    assert m["game_keywords"] == ["_NORMALMAP", "_SURFACE_TYPE_TRANSPARENT"]
    assert m["textures"]["_BaseMap"] == ("a" * 32, (2, 2), (0.5, 0)) and "_MainTex" in m["textures"]
    assert m["floats"]["_QueueOffset"] == 2 and m["floats"]["_Smoothness"] == 0.7 and m["floats"]["_Mode"] == 2
    assert m["colors"]["_BaseColor"] == (1, 0.5, 0, 0.4) and m["tags"]["Custom"] == "x"
    assert set(m["lit_keywords"]) == {"_NORMALMAP", "_SURFACE_TYPE_TRANSPARENT"} and m["lit_queue"] == 3000


def test_convert_urp_fallback_lit_names():
    d = details("Shader Graphs/Rock", textures=[("_Albedo", tex("a"), (1, 1), (0, 0)),
                                                ("_MetallicGlossMap", tex("m"), (1, 1), (0, 0))],
                colors={"_Tint": (0.5, 0.5, 0.5, 1)}, floats={"_Glossiness": 0.4})
    m = um.convert(d, guid, pipeline="urp")
    assert m["textures"]["_BaseMap"] == m["textures"]["_MainTex"] == m["textures"]["_Albedo"]
    assert m["colors"]["_BaseColor"] == (0.5, 0.5, 0.5, 1) and m["floats"]["_Smoothness"] == 0.4
    assert m["floats"]["_Surface"] == 0 and m["floats"]["_WorkflowMode"] == 1
    assert "_METALLICSPECGLOSSMAP" in m["lit_keywords"] and "_METALLICGLOSSMAP" not in m["lit_keywords"]


def test_convert_urp_unlit_builtin_has_no_lit_fallback():
    m = um.convert(details("Universal Render Pipeline/Particles/Unlit", textures=[("_BaseMap", tex("a"), (1, 1),
                                                                                    (0, 0))]), guid, pipeline="urp")
    assert m["shader"].startswith("Legacy Shaders/Particles") and "lit_keywords" not in m
    assert m["game_shader"] == "Universal Render Pipeline/Particles/Unlit"


def test_convert_without_pipeline_unchanged():
    m = um.convert(details("Universal Render Pipeline/Lit"), guid)
    assert "game_shader" not in m and "_BaseColor" not in m["colors"]


def test_convert_hdrp_fallback_lit_names():
    d = details("Custom/Metal", textures=[("_MainTex", tex("a"), (1, 1), (0, 0)), ("_BumpMap", tex("n"), (1, 1), (0, 0)),
                                         ("_MetallicGlossMap", tex("m"), (1, 1), (0, 0)),
                                         ("_EmissionMap", tex("e"), (1, 1), (0, 0))],
                colors={"_Color": (1, 0, 0, 1), "_EmissionColor": (2, 2, 2, 1)}, floats={"_Glossiness": 0.6},
                keywords=["_ALPHATEST_ON"], queue=2450)
    m = um.convert(d, guid, pipeline="hdrp")
    t, f = m["textures"], m["floats"]
    assert t["_BaseColorMap"] == t["_MainTex"] and t["_NormalMap"] == t["_BumpMap"] and "_MaskMap" in t
    assert t["_EmissiveColorMap"] == t["_EmissionMap"] and m["colors"]["_EmissiveColor"] == (2, 2, 2, 1)
    assert m["colors"]["_BaseColor"] == (1, 0, 0, 1) and f["_Smoothness"] == 0.6
    assert f["_AORemapMin"] == f["_AORemapMax"] == 1.0 and f["_SurfaceType"] == 0
    kw = m["lit_keywords"]
    assert {"_NORMALMAP", "_NORMALMAP_TANGENT_SPACE", "_MASKMAP", "_EMISSIVE_COLOR_MAP"} <= set(kw)
    assert "_METALLICGLOSSMAP" not in kw and "_EMISSION" not in kw


def test_convert_hdrp_keeps_game_mask_map_remaps():
    d = details("HDRP/Lit", textures=[("_BaseColorMap", tex("a"), (1, 1), (0, 0)), ("_MaskMap", tex("m"), (1, 1),
                                                                                       (0, 0))],
                floats={"_AORemapMin": 0.2, "_SurfaceType": 1}, keywords=["_MASKMAP"])
    m = um.convert(d, guid, pipeline="hdrp")
    assert m["floats"]["_AORemapMin"] == 0.2 and m["floats"]["_SurfaceType"] == 1
    assert m["game_shader"] == "HDRP/Lit" and m["game_keywords"] == ["_MASKMAP"]
