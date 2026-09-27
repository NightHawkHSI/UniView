"""Game materials as Unity .mat files for exported projects. No Qt here.

The game's own shaders aren't in the project (their source isn't in the build), so each material is
put on one of Unity's built-in shaders: the same one when the game used a built-in shader, else the
closest match (Standard with Opaque/Cutout/Fade, Unlit, or a particle shader), with the texture,
color and surface properties carried over under the names that shader uses.
"""

import re

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
METALLIC_MAP = ("_MetallicGlossMap", "_MetallicMap", "_MaskMap")
OCCLUSION = ("_OcclusionMap",)
GLOSS = ("_Glossiness", "_Smoothness")
TRANSPARENT_WORDS = ("transparent", "translucent", "fade", "glass", "alpha blend", "shield", "hologram",
                     "forcefield", "force field", "water")


def _first(mapping, names):
    return next((mapping[n] for n in names if n in mapping), None)


def surface(details):
    """'opaque', 'cutout' or 'transparent', from the queue, tags, keywords, floats and shader name."""
    name = (details.get("shader") or "").lower()
    kw = set(details.get("keywords") or ())
    floats = details.get("floats") or {}
    queue = details.get("queue", -1)
    render_type = (details.get("tags") or {}).get("RenderType", "")
    if (render_type == "Transparent" or queue >= 2750
            or kw & {"_ALPHABLEND_ON", "_ALPHAPREMULTIPLY_ON", "_SURFACE_TYPE_TRANSPARENT"}
            or floats.get("_Surface") == 1 or floats.get("_Mode") in (2, 3)
            or any(w in name for w in TRANSPARENT_WORDS)):
        return "transparent"
    if (render_type == "TransparentCutout" or 2400 <= queue < 2750 or "_ALPHATEST_ON" in kw
            or floats.get("_AlphaClip") == 1 or floats.get("_Mode") == 1 or "cutout" in name):
        return "cutout"
    return "opaque"


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


def convert(details, texture_guid):
    """Unity material settings for a game material: {"name", "shader_id", "textures": {prop: (guid, scale, offset)},
    "floats", "colors", "keywords", "queue", "tags"}. texture_guid(texture Asset) -> guid or None (not exported)."""
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

    main = _first(textures, ALBEDO)
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
        normal = _first(textures, NORMAL)
        if normal:
            t["_BumpMap"] = normal
            kw.append("_NORMALMAP")
            f["_BumpScale"] = floats.get("_BumpScale", 1.0)
        metallic = _first(textures, METALLIC_MAP)
        if metallic:
            t["_MetallicGlossMap"] = metallic
            kw.append("_METALLICGLOSSMAP")
        occlusion = _first(textures, OCCLUSION)
        if occlusion:
            t["_OcclusionMap"] = occlusion
        emission_map = _first(textures, EMISSION_MAP)
        emission = _first(colors, EMISSION_COLOR)
        if emission_map or (emission and max(emission[:3]) > 0.001):
            if emission_map:
                t["_EmissionMap"] = emission_map
            c["_EmissionColor"] = emission or (1.0, 1.0, 1.0, 1.0)
            kw.append("_EMISSION")
        f["_Metallic"] = floats.get("_Metallic", 0.0)
        f["_Glossiness"] = _first(floats, GLOSS) if _first(floats, GLOSS) is not None else 0.2
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
