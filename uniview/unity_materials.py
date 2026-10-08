"""Game materials as Unity .mat files for exported projects. No Qt here.

The game's own shaders aren't in the project (their source isn't in the build), so each material is
put on one of Unity's built-in shaders: the same one when the game used a built-in shader, else the
closest match (Standard with Opaque/Cutout/Fade, Unlit, or a particle shader), with the texture,
color and surface properties carried over under the names that shader uses.
"""

import re

from engines.unity import material_alpha
from uniview import pbr

BUILTIN_GUID = "0000000000000000f000000000000000"  # Unity's built-in extra resources

# Built-in shaders and their file IDs in unity_builtin_extra (the same in every Unity version since 5.x).
BUILTIN_SHADERS = {
    "Standard": 46,
    "Standard (Specular setup)": 45,
    "Unlit/Texture": 10752,
    "Unlit/Color": 10755,
    "Unlit/Transparent": 10750,
    "Unlit/Transparent Cutout": 10751,
    "Legacy Shaders/Diffuse": 7,
    "Legacy Shaders/Transparent/Diffuse": 30,
    "Legacy Shaders/Particles/Additive": 200,
    "Particles/Additive": 200,
    "Legacy Shaders/Particles/Alpha Blended": 203,
    "Particles/Alpha Blended": 203,
    "Particles/Standard Surface": 210,
    "Particles/Standard Unlit": 211,
    "Sprites/Default": 10753,
    "UI/Default": 10770,
}

ALBEDO = ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo", "_BaseColorTexture", "_Diffuse", "_DiffuseMap")
COLOR = ("_Color", "_BaseColor", "_MainColor", "_TintColor", "_BaseColorFactor", "_Tint")
NORMAL = ("_BumpMap", "_NormalMap", "_Normal")
EMISSION_MAP = ("_EmissionMap", "_EmissiveColorMap", "_EmissiveMap", "_GlowTex")
EMISSION_COLOR = ("_EmissionColor", "_EmissiveColor", "_GlowColor")
METALLIC_MAP = ("_MetallicGlossMap", "_MaskMap")
OCCLUSION = ("_OcclusionMap", "_AOMap", "_AmbientOcclusionMap")
GLOSS = ("_Glossiness", "_Smoothness")


def _first(mapping, names):
    return next((mapping[n] for n in names if n in mapping), None)


def surface(details):
    """'opaque', 'cutout' or 'transparent', from the queue, tags, keywords, floats and shader name
    (engines.unity.material_alpha, which the 3D view uses too)."""
    queue, tags, blend = details.get("queue", -1), details.get("tags"), None
    if queue == -1 and details.get("shader_render"):  # the shader's own queue / tags / blending
        s_queue, s_tags, blend = details["shader_render"]
        queue, tags = s_queue, {**s_tags, **(tags or {})}
    mode, _cutoff = material_alpha(details.get("shader"), details.get("floats"), details.get("keywords"),
                                   queue, tags, blend)
    return {"mask": "cutout", "blend": "transparent", "add": "transparent"}.get(mode, "opaque")


def pick_shader(details):
    """(built-in shader name, copy_all): the game's own shader if it's a built-in, else the closest built-in."""
    name = details.get("shader") or ""
    if name in BUILTIN_SHADERS:
        return name, True
    low = name.lower()
    floats = details.get("floats") or {}
    additive = "additive" in low or (floats.get("_SrcBlend") in (1, 5) and floats.get("_DstBlend") == 1)
    if "particle" in low or additive:
        return ("Legacy Shaders/Particles/Additive" if additive else "Legacy Shaders/Particles/Alpha Blended"), False
    if "unlit" in low or low.startswith(("ui/", "sprites/")):
        kind = surface(details)
        has_texture = _first({p: t for p, *t in details.get("textures") or ()}, ALBEDO) is not None
        if not has_texture:
            return "Unlit/Color", False
        return {"transparent": "Unlit/Transparent", "cutout": "Unlit/Transparent Cutout"}.get(kind, "Unlit/Texture"), False
    return "Standard", False


STANDARD_MODES = {  # _Mode, _SrcBlend, _DstBlend, _ZWrite, keyword, queue, RenderType
    "opaque": (0, 1, 0, 1, None, -1, ""),
    "cutout": (1, 1, 0, 1, "_ALPHATEST_ON", 2450, "TransparentCutout"),
    "transparent": (2, 5, 10, 0, "_ALPHABLEND_ON", 3000, "Transparent"),
}


def channels(details, textures):
    """{pbr channel: (guid, scale, offset)} of the exported textures, plus {channel: texture Asset}: known
    property names first, then texture names for properties nobody knows."""
    found, assets = {}, {}
    for by_name in (False, True):
        for prop, asset, *_rest in details.get("textures") or ():
            if prop not in textures:
                continue
            ch = pbr.slot_channel(prop)
            if by_name:
                if ch is not None:
                    continue
                ch = pbr.name_channel(getattr(asset, "name", ""))
            if ch is not None and ch not in found:
                found[ch], assets[ch] = textures[prop], asset
    return found, assets


def convert(details, texture_guid, make_texture=None):
    """Unity material settings for a game material: {"name", "shader_id", "textures": {prop: (guid, scale, offset)},
    "floats", "colors", "keywords", "queue", "tags"}. texture_guid(texture Asset) -> guid or None (not exported).
    make_texture(recipe, [(pbr channel, texture Asset)]) -> guid or None: a texture repacked for Unity's layout
    ("metal_gloss": R metallic + A smoothness, "ao": occlusion), for maps the Standard shader can't read as they are."""
    shader, copy_all = pick_shader(details)
    textures = {}
    for prop, asset, scale, offset in details.get("textures") or ():
        guid = texture_guid(asset)
        if guid:
            textures[prop] = (guid, scale, offset)
    colors = dict(details.get("colors") or {})
    floats = dict(details.get("floats") or {})
    out = {"name": details.get("name") or "Material", "shader_id": BUILTIN_SHADERS[shader], "shader": shader}
    if copy_all:
        out.update(textures=textures, floats=floats, colors=colors, keywords=list(details.get("keywords") or ()),
                   queue=details.get("queue", -1), tags=dict(details.get("tags") or {}))
        return out

    chan, chan_assets = channels(details, textures)
    main = _first(textures, ALBEDO) or chan.get(pbr.ALBEDO)
    color = _first(colors, COLOR)
    t, f, c, kw, tags = {}, {}, {}, [], {}
    queue = -1
    if main:
        t["_MainTex"] = main
    if shader == "Standard":
        kind = surface(details)
        mode, src, dst, zwrite, keyword, queue, render_type = STANDARD_MODES[kind]
        f.update(_Mode=mode, _SrcBlend=src, _DstBlend=dst, _ZWrite=zwrite)
        if keyword:
            kw.append(keyword)
        if render_type:
            tags["RenderType"] = render_type
        normal = _first(textures, NORMAL) or chan.get(pbr.NORMAL)
        if normal:
            t["_BumpMap"] = normal
            kw.append("_NORMALMAP")
            f["_BumpScale"] = floats.get("_BumpScale", floats.get("_NormalScale", 1.0))
        gloss = _first(floats, GLOSS)
        gloss_scale = _first(floats, ("_GlossMapScale", "_Smoothness"))

        def made(recipe, sources):
            guid = make_texture(recipe, sources) if make_texture is not None else None
            first = chan[sources[0][0]]
            return (guid, first[1], first[2]) if guid else None

        metallic = _first(textures, METALLIC_MAP) or chan.get(pbr.METAL_GLOSS) or chan.get(pbr.MASK)
        if metallic is None:
            separate = [(ch, chan_assets[ch]) for ch in (pbr.ORM, pbr.METALLIC, pbr.ROUGHNESS, pbr.SMOOTHNESS)
                        if ch in chan]
            if separate and separate[0][0] == pbr.ORM:
                separate = separate[:1]
            if separate:
                metallic = made("metal_gloss", separate[:2])
                if metallic:  # smoothness is in the map now; a metallic-only map keeps the material's value
                    smooth_map = any(ch != pbr.METALLIC for ch, _a in separate[:2])
                    gloss_scale = 1.0 if smooth_map else gloss
        specular = chan.get(pbr.SPECULAR)
        if metallic:
            t["_MetallicGlossMap"] = metallic
            kw.append("_METALLICGLOSSMAP")
            f["_GlossMapScale"] = 1.0 if gloss_scale is None else gloss_scale
        elif specular:  # a specular map and no metallic one: Unity's specular-setup Standard shader
            shader = out["shader"] = "Standard (Specular setup)"
            out["shader_id"] = BUILTIN_SHADERS[shader]
            t["_SpecGlossMap"] = specular
            kw.append("_SPECGLOSSMAP")
            c["_SpecColor"] = colors.get("_SpecColor") or colors.get("_SpecularColor") or (0.2, 0.2, 0.2, 1.0)
            f["_GlossMapScale"] = 1.0 if gloss_scale is None else gloss_scale
        occlusion = _first(textures, OCCLUSION) or chan.get(pbr.AO) or chan.get(pbr.MASK)  # Standard reads green
        if occlusion is None and pbr.ORM in chan:
            occlusion = made("ao", [(pbr.ORM, chan_assets[pbr.ORM])])
        if occlusion:
            t["_OcclusionMap"] = occlusion
            f["_OcclusionStrength"] = floats.get("_OcclusionStrength", 1.0)
        height = chan.get(pbr.HEIGHT)
        if height:
            t["_ParallaxMap"] = height
            kw.append("_PARALLAXMAP")
            f["_Parallax"] = min(0.08, max(0.005, floats.get("_Parallax", 0.02)))
        if pbr.DETAIL_ALBEDO in chan or pbr.DETAIL_NORMAL in chan:
            for ch, prop in ((pbr.DETAIL_ALBEDO, "_DetailAlbedoMap"), (pbr.DETAIL_NORMAL, "_DetailNormalMap"),
                             (pbr.DETAIL_MASK, "_DetailMask")):
                if ch in chan:
                    t[prop] = chan[ch]
            kw.append("_DETAIL_MULX2")
            f["_DetailNormalMapScale"] = floats.get("_DetailNormalMapScale", 1.0)
            f["_UVSec"] = floats.get("_UVSec", 0)
        emission_map = _first(textures, EMISSION_MAP) or chan.get(pbr.EMISSION)
        emission = _first(colors, EMISSION_COLOR)
        if emission_map or (emission and max(emission[:3]) > 0.001):
            if emission_map:
                t["_EmissionMap"] = emission_map
            c["_EmissionColor"] = emission or (1.0, 1.0, 1.0, 1.0)
            kw.append("_EMISSION")
        f["_Metallic"] = floats.get("_Metallic", 0.0)
        f["_Glossiness"] = gloss if gloss is not None else 0.2
        f["_Cutoff"] = floats.get("_Cutoff", floats.get("_AlphaClipThreshold", 0.5))
        c["_Color"] = color or (1.0, 1.0, 1.0, 1.0)
    elif shader.startswith("Legacy Shaders/Particles"):
        c["_TintColor"] = colors.get("_TintColor") or (tuple(color[:3]) + (color[3] * 0.5,) if color else (0.5, 0.5, 0.5, 0.5))
    else:  # Unlit
        if color is not None:
            c["_Color"] = color
        f["_Cutoff"] = floats.get("_Cutoff", 0.5)
    out.update(textures=t, floats=f, colors=c, keywords=kw, queue=queue, tags=tags)
    return out


def _num(v):
    v = float(v)
    if v != v:
        return "NaN"
    if v in (float("inf"), float("-inf")):
        return "Infinity" if v > 0 else "-Infinity"  # how Unity writes them in YAML
    return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)


def _quote(text):
    return '"' + re.sub(r'(["\\])', r"\\\1", str(text)).replace("\n", " ") + '"'


def mat_yaml(m):
    """The .mat file text (Unity YAML) for convert()'s result."""
    lines = ["%YAML 1.1", "%TAG !u! tag:unity3d.com,2011:", "--- !u!21 &2100000", "Material:",
             "  serializedVersion: 6", "  m_ObjectHideFlags: 0", "  m_CorrespondingSourceObject: {fileID: 0}",
             "  m_PrefabInstance: {fileID: 0}", "  m_PrefabAsset: {fileID: 0}",
             f"  m_Name: {_quote(m['name'])}",
             f"  m_Shader: {{fileID: {m['shader_id']}, guid: {BUILTIN_GUID}, type: 0}}",
             f"  m_ShaderKeywords: {' '.join(m['keywords'])}" if m["keywords"] else "  m_ShaderKeywords: ",
             "  m_LightmapFlags: 4", "  m_EnableInstancingVariants: 0", "  m_DoubleSidedGI: 0",
             f"  m_CustomRenderQueue: {int(m['queue'])}"]
    if m["tags"]:
        lines.append("  stringTagMap:")
        lines += [f"    {k}: {_quote(v)}" for k, v in m["tags"].items()]
    else:
        lines.append("  stringTagMap: {}")
    lines += ["  disabledShaderPasses: []", "  m_SavedProperties:", "    serializedVersion: 3"]
    lines.append("    m_TexEnvs:" if m["textures"] else "    m_TexEnvs: []")
    for prop, (guid, scale, offset) in m["textures"].items():
        lines += [f"    - {prop}:", f"        m_Texture: {{fileID: 2800000, guid: {guid}, type: 3}}",
                  f"        m_Scale: {{x: {_num(scale[0])}, y: {_num(scale[1])}}}",
                  f"        m_Offset: {{x: {_num(offset[0])}, y: {_num(offset[1])}}}"]
    lines.append("    m_Floats:" if m["floats"] else "    m_Floats: []")
    lines += [f"    - {prop}: {_num(v)}" for prop, v in m["floats"].items()]
    lines.append("    m_Colors:" if m["colors"] else "    m_Colors: []")
    for prop, rgba in m["colors"].items():
        r, g, b, a = (list(rgba) + [1.0] * 4)[:4]
        lines.append(f"    - {prop}: {{r: {_num(r)}, g: {_num(g)}, b: {_num(b)}, a: {_num(a)}}}")
    return "\n".join(lines) + "\n"
