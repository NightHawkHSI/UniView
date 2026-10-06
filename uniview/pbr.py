"""Which texture of a material is which PBR map (albedo, normal, metallic, roughness, AO, height, ...),
and the pixel conversions between the engines' channel layouts and glTF / Unity Standard. No Qt here.

Slots are matched by the material's property name (Unity "_BumpMap", Source "$bumpmap", Source 2
"g_tNormal"); textures in slots nobody knows are guessed from the texture's name ("Rock_R", "Rock_ORM").
"""

import os
import re

import numpy as np
from PIL import Image

from engines.sdk import ALBEDO as ALBEDO_ROLE, NORMAL as NORMAL_ROLE

# Channels. Packed layouts:
#   metal_gloss  Unity Standard/URP: R metallic, A smoothness
#   mask         HDRP mask map:      R metallic, G occlusion, B detail mask, A smoothness
#   orm          glTF / Unreal:      R occlusion, G roughness, B metallic
#   specular     Unity specular:     RGB specular colour, A smoothness
ALBEDO, NORMAL, METAL_GLOSS, MASK, ORM, METALLIC, ROUGHNESS, SMOOTHNESS = (
    "albedo", "normal", "metal_gloss", "mask", "orm", "metallic", "roughness", "smoothness")
AO, HEIGHT, EMISSION, SPECULAR, DETAIL_ALBEDO, DETAIL_NORMAL, DETAIL_MASK = (
    "ao", "height", "emission", "specular", "detail_albedo", "detail_normal", "detail_mask")

SLOTS = {
    ALBEDO: ("_maintex", "_basemap", "_basecolormap", "_albedo", "_basecolortexture", "_diffuse", "_diffusemap",
             "_albedomap", "_colormap", "$basetexture", "g_tcolor", "g_tcolor1", "g_tcolora", "g_tbasecolor",
             "g_talbedo", "g_tlayer1color"),
    NORMAL: ("_bumpmap", "_normalmap", "_normal", "_normaltex", "_normaltexture", "$bumpmap", "$normalmap",
             "g_tnormal", "g_tnormal1", "g_tnormala", "g_tnormalroughness", "g_tlayer1normal"),
    METAL_GLOSS: ("_metallicglossmap", "_metallictex", "_metaltex", "_metallicsmoothness", "_metallicsmoothnessmap"),
    MASK: ("_maskmap",),
    ORM: ("_ormmap", "_ormtex", "_occlusionroughnessmetallic", "_metallicroughnesstexture"),
    METALLIC: ("_metallicmap", "_metalnessmap", "_metalness", "_metallic", "g_tmetalness", "g_tmetallic"),
    ROUGHNESS: ("_roughnessmap", "_roughness", "_roughtex", "_roughnesstex", "g_troughness"),
    SMOOTHNESS: ("_glossmap", "_smoothnessmap", "_glossinessmap", "_glosstex", "_smoothnesstex"),
    AO: ("_occlusionmap", "_aomap", "_ambientocclusionmap", "_ao", "_aotex", "_occlusion", "_occlusiontex",
         "g_tambientocclusion", "g_tao"),
    HEIGHT: ("_parallaxmap", "_heightmap", "_height", "_heighttex", "_displacementmap", "_dispmap",
             "_displacement", "g_theight", "g_tdisplacement"),
    EMISSION: ("_emissionmap", "_emissivecolormap", "_emissivemap", "_emissivetex", "_emission", "_emissiontex",
               "_glowtex", "_illum", "g_tselfillum", "g_temissive"),
    SPECULAR: ("_specglossmap", "_specularmap", "_specmap", "_speculartex", "_spectex"),
    DETAIL_ALBEDO: ("_detailalbedomap", "$detail"),
    DETAIL_NORMAL: ("_detailnormalmap",),
    DETAIL_MASK: ("_detailmask",),
}
_BY_SLOT = {slot: channel for channel, slots in SLOTS.items() for slot in slots}

# Texture-name endings (after the last "_" / "-" / " "): Rock_BC, Rock_N, Rock_ORM, Rock_Roughness ...
NAME_SUFFIXES = {
    ALBEDO: ("bc", "d", "basecolor", "albedo", "diffuse", "diff", "col", "color", "colour", "alb", "base"),
    NORMAL: ("n", "nrm", "nm", "nor", "norm", "normal", "normalmap", "bump"),
    ORM: ("orm", "arm", "occlusionroughnessmetallic"),
    METAL_GLOSS: ("ms", "metallicsmoothness", "metalsmooth", "metallicgloss"),
    METALLIC: ("m", "metal", "metallic", "metalness", "mtl"),
    ROUGHNESS: ("r", "rough", "roughness", "rgh"),
    SMOOTHNESS: ("gloss", "glossiness", "smoothness", "smooth"),
    AO: ("ao", "occlusion", "ambientocclusion", "occ"),
    HEIGHT: ("h", "height", "disp", "displacement", "parallax", "heightmap"),
    EMISSION: ("e", "emissive", "emission", "glow", "illum", "emit"),
    SPECULAR: ("spec", "specular", "specgloss"),
}
_BY_SUFFIX = {suffix: channel for channel, suffixes in NAME_SUFFIXES.items() for suffix in suffixes}


def slot_channel(slot):
    """Channel of a material property name, or None."""
    return _BY_SLOT.get((slot or "").strip().lower())


def name_channel(name):
    """Channel guessed from a texture's name ending, or None."""
    stem = os.path.splitext(os.path.basename((name or "").replace("\\", "/")))[0].lower()
    parts = re.split(r"[_\-. ]+", stem)
    return _BY_SUFFIX.get(parts[-1]) if len(parts) > 1 else None


def assign(textures):
    """{channel: TextureRef} for a Material's textures. The engine's albedo/normal roles and known slots
    first; then the slot's own words ("Mesh_AO", the shader's "Emission Mask"), then texture names fill channels
    that are still empty."""
    def known(t):
        return ({ALBEDO_ROLE: ALBEDO, NORMAL_ROLE: NORMAL}.get(t.role) or slot_channel(t.slot)
                or slot_channel(getattr(t, "label", "")))

    out = {}
    for t in textures:
        channel = known(t)
        if channel and channel not in out:
            out[channel] = t
    for t in textures:
        if known(t):
            continue
        channel = (name_channel(t.slot) or name_channel(getattr(t, "label", "")) or name_channel(t.name)
                   or name_channel(getattr(t.asset, "name", "")))
        if channel and channel not in out:
            out[channel] = t
    return out


# Words in a slot / shader label / texture name that mean "not a picture to show": data maps for the shader.
DATA_WORDS = ("mask", "masks", "noise", "flow", "distortion", "distort", "dissolve", "ramp", "lut", "cubemap",
              "reflection", "matcap", "lightmap", "lightmaps", "shadowmask", "depth", "screen", "material")


def _slot_words(t):
    return set(re.split(r"[^a-z0-9]+", " ".join((t.slot, getattr(t, "label", ""))).lower()))


def base_texture(mat):
    """The TextureRef to draw a material with: its albedo (known slot, shader name or texture name), else its
    first texture that isn't known to be another map (normal, AO, mask, metallic, ...); None if all are."""
    if mat is None or not mat.textures:
        return None
    maps = assign(mat.textures)
    if ALBEDO in maps:
        return maps[ALBEDO]
    taken = {id(t) for t in maps.values()}
    for t in mat.textures:
        if t.role == NORMAL_ROLE or id(t) in taken or _slot_words(t) & set(DATA_WORDS):
            continue
        return t
    return None


def property_float(material, names):
    """First of `names` among a Material's properties [(name, value text)] as a float, else None."""
    props = dict(material.properties or ())
    for n in names:
        try:
            return float(props[n])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def property_color(material, names):
    """First of `names` among a Material's properties as (r, g, b) in 0..1 ('#rrggbb  alpha a' text), else None."""
    props = dict(material.properties or ())
    for n in names:
        m = re.match(r"#([0-9a-fA-F]{6})", str(props.get(n, "")))
        if m:
            h = m.group(1)
            return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return None


# --------------------------------------------------------------------------- pixels

def _rgba(img):
    return np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0


def _gray(img, size=None):
    """One channel (0..1) of a grayscale map: its red (= every channel of a gray image)."""
    if size is not None and img.size != size:
        img = img.resize(size, Image.BILINEAR)
    return _rgba(img)[..., 0]


def _image(channels):
    """uint8 RGB(A) image from a list of 0..1 arrays."""
    return Image.fromarray((np.clip(np.stack(channels, axis=-1), 0, 1) * 255 + 0.5).astype(np.uint8))


def normal_layout(img):
    """'dxt5nm' (X in alpha, Y in green - Unity's PC normal maps), 'rg' (BC5: blue empty) or 'rgb'."""
    a = np.asarray(img.convert("RGBA"), dtype=np.float32).reshape(-1, 4)
    if len(a) > 65536:
        a = a[:: len(a) // 65536]
    mean, std = a.mean(0), a.std(0)
    # A real RGB normal map never has red stuck at 1 (all normals facing +X) or blue at 0 (all facing inward).
    if mean[0] > 250 and std[0] < 2 and mean[3] < 250:
        return "dxt5nm"
    if mean[2] < 5 and std[2] < 2:
        return "rg"
    return "rgb"


def unpack_normal(img):
    """A tangent-space normal map as a plain RGB image (what glTF, Blender and a Unity PNG import expect)."""
    layout = normal_layout(img)
    if layout == "rgb":
        return img.convert("RGB")
    p = _rgba(img)
    x = p[..., 3] if layout == "dxt5nm" else p[..., 0]
    y = p[..., 1]
    nx, ny = x * 2 - 1, y * 2 - 1
    z = np.sqrt(np.clip(1 - nx * nx - ny * ny, 0, 1)) * 0.5 + 0.5
    return _image([x, y, z])


def gltf_metal_rough(channel, img, gloss_scale=1.0, second=None):
    """glTF metallicRoughness image (G roughness, B metallic) from a map of `channel`.

    second: (channel, image) of a separate metallic or roughness/smoothness map to combine with.
    Channels without a map are white (the material's factor applies)."""
    size = img.size
    maps = {channel: img}
    if second is not None:
        maps[second[0]] = second[1]
    metal = rough = None
    for ch, im in maps.items():
        if ch in (METAL_GLOSS, MASK):
            p = _rgba(im if im.size == size else im.resize(size, Image.BILINEAR))
            metal, rough = p[..., 0], 1 - p[..., 3] * gloss_scale
        elif ch == ORM:
            p = _rgba(im if im.size == size else im.resize(size, Image.BILINEAR))
            metal, rough = p[..., 2], p[..., 1]
        elif ch == METALLIC:
            metal = _gray(im, size)
        elif ch == ROUGHNESS:
            rough = _gray(im, size)
        elif ch == SMOOTHNESS:
            rough = 1 - _gray(im, size) * gloss_scale
    ones = np.ones(size[::-1], np.float32)
    return _image([ones, ones if rough is None else rough, ones if metal is None else metal])


def unity_metal_gloss(channel, img, second=None):
    """Unity Standard _MetallicGlossMap (R metallic, A smoothness) from a map of `channel` (+ an optional
    separate (channel, image)). Missing metallic is black, missing smoothness white (scaled by _GlossMapScale)."""
    size = img.size
    maps = {channel: img}
    if second is not None:
        maps[second[0]] = second[1]
    metal = smooth = None
    for ch, im in maps.items():
        if ch == ORM:
            p = _rgba(im if im.size == size else im.resize(size, Image.BILINEAR))
            metal, smooth = p[..., 2], 1 - p[..., 1]
        elif ch == METALLIC:
            metal = _gray(im, size)
        elif ch == ROUGHNESS:
            smooth = 1 - _gray(im, size)
        elif ch == SMOOTHNESS:
            smooth = _gray(im, size)
    shape = size[::-1]
    metal = np.zeros(shape, np.float32) if metal is None else metal
    smooth = np.ones(shape, np.float32) if smooth is None else smooth
    return _image([metal, metal, metal, smooth])


def occlusion(channel, img):
    """Grayscale occlusion from an AO map, HDRP mask (G) or ORM (R) - glTF reads red, Unity green."""
    p = _rgba(img)
    ao = p[..., 1] if channel == MASK else p[..., 0]
    return _image([ao, ao, ao])


def single(channel, img, want):
    """One grayscale map (`want`: METALLIC or ROUGHNESS) from a map of `channel` (for OBJ's map_Pm / map_Pr)."""
    p = _rgba(img)
    if channel in (METAL_GLOSS, MASK):
        v = p[..., 0] if want == METALLIC else 1 - p[..., 3]
    elif channel == ORM:
        v = p[..., 2] if want == METALLIC else p[..., 1]
    elif channel == SMOOTHNESS:
        v = 1 - p[..., 0]
    else:
        v = p[..., 0]
    return _image([v, v, v])
