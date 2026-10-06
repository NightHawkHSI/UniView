"""Unity engine plugin (UnityPy): meshes, Texture2D, Sprites and TextAssets from any Unity game."""

import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

import numpy as np

from .sdk import (
    ALBEDO, NORMAL, OTHER, Asset, EnginePlugin, GameSession, Material, MeshData, TextureRef,
)

log = logging.getLogger("viewer.unity")

TYPE_KINDS = {"Mesh": "model", "Texture2D": "texture", "Sprite": "sprite", "TextAsset": "text", "AudioClip": "audio",
              "AnimationClip": "animation",
              "Font": "font", "VideoClip": "video", "MonoBehaviour": "data", "Cubemap": "texture",
              "PhysicMaterial": "data", "PhysicsMaterial": "data", "PhysicsMaterial2D": "data",
              "AnimatorController": "controller"}
BUILTIN_DATA = ("PhysicMaterial", "PhysicsMaterial", "PhysicsMaterial2D")  # data assets that aren't scripts
ALBEDO_PROPS = ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo", "_BaseColorTexture")
NORMAL_PROPS = ("_BumpMap", "_NormalMap")

BUNDLE_MAGICS = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
RESOURCE_EXTS = (".ress", ".resource")
SKIP_EXTS = (".exe", ".dll", ".so", ".pdb", ".mdb", ".txt", ".log", ".json", ".xml",
             ".config", ".ini", ".cfg", ".png", ".jpg", ".bat", ".sys", ".manifest",
             ".vpk", ".pak", ".utoc", ".ucas", ".sig")
DATA_MARKERS = ("globalgamemanagers", "mainData", "data.unity3d", "level0")
SNIFF_EXTS = ("", ".assets", ".unity3d", ".bundle", ".ab", ".asset", ".assetbundle")  # detect() on loose folders
UNITY_VERSION_RE = re.compile(rb"\d{1,4}\.\d+\.\d+[a-zA-Z]?\d*")


# --------------------------------------------------------------------------- finding files

def looks_like_unity_file(path):
    """Sniff a file's header to decide whether UnityPy can load it."""
    lower = path.lower()
    if lower.endswith(RESOURCE_EXTS) or ".split" in os.path.basename(lower):
        return True  # streamed texture/mesh data referenced by .assets files
    if lower.endswith(SKIP_EXTS):
        return False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(48)
    except OSError:
        return False
    if head.startswith(BUNDLE_MAGICS):
        return True
    # Serialized file (.assets, level0, globalgamemanagers...): no magic, so check
    # that the big-endian header's version and recorded file size are sane.
    if len(head) < 48:
        return False
    version = int.from_bytes(head[8:12], "big")
    if not 5 <= version <= 50:
        return False
    if version < 22:
        return int.from_bytes(head[4:8], "big") == size
    return int.from_bytes(head[24:32], "big") == size


def find_unity_files(root, limit=None):
    if os.path.isfile(root):
        return [root] if looks_like_unity_file(root) else []
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if looks_like_unity_file(path):
                found.append(path)
                if limit and len(found) >= limit:
                    return found
    return sorted(found)


def unity_data_dir(game_dir):
    """Return the game's <Name>_Data folder if this looks like a Unity game, else None."""
    try:
        entries = os.listdir(game_dir)
    except OSError:
        return None
    for name in entries:
        path = os.path.join(game_dir, name)
        if name.endswith("_Data") and os.path.isdir(path):
            if any(os.path.exists(os.path.join(path, f)) for f in DATA_MARKERS):
                return path
    return None


def read_unity_version(path):
    """Unity editor version stored in a serialized file's or bundle's header (e.g. '2019.4.40f1')."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if head.startswith(BUNDLE_MAGICS):
        # magic\0, uint32 format, player version\0, engine version\0
        strings = head[head.index(b"\0") + 5:].split(b"\0")
        candidates = strings[1:2] + strings[:1]
    else:
        if len(head) < 48:
            return None
        version = int.from_bytes(head[8:12], "big")
        if version < 9:
            return None  # older files keep the version at the end of the file
        candidates = [head[48 if version >= 22 else 20:].split(b"\0")[0]]
    for text in candidates:
        if UNITY_VERSION_RE.fullmatch(text) and not text.startswith(b"0.0"):
            return text.decode()
    return None


def detect_unity_info(game_dir):
    """{'engine_version': '2019.4.40f1' or '', 'detail': 'IL2CPP' / 'Mono' / ''} without loading the game."""
    data = unity_data_dir(game_dir) if os.path.isdir(game_dir) else None
    version = None
    for name in DATA_MARKERS if data else ():
        version = read_unity_version(os.path.join(data, name))
        if version:
            break
    if not version:
        # Loose asset folder or a single file: try the first few Unity files found.
        for path in find_unity_files(game_dir, limit=50):
            version = read_unity_version(path)
            if version:
                break
    backend = ""
    if os.path.isdir(game_dir) and (os.path.isfile(os.path.join(game_dir, "GameAssembly.dll")) or (
            data and os.path.isdir(os.path.join(data, "il2cpp_data")))):
        backend = "IL2CPP"
    elif data and os.path.isdir(os.path.join(data, "Managed")):
        backend = "Mono"
    return {"engine_version": version or "", "detail": backend}


def version_key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def unity_branch(version):
    """'2019.4.40f1' -> 'Unity 2019.4', '6000.0.58f2' -> 'Unity 6.0'."""
    nums = version_key(version)
    if len(nums) < 2:
        return "Unity (unknown version)"
    major = nums[0] // 1000 if nums[0] >= 6000 else nums[0]
    return f"Unity {major}.{nums[1]}"


# --------------------------------------------------------------------------- mesh conversion

SURFACE_TOPOLOGIES = (0, 1, 2)  # Triangles, TriangleStrip, Quads (not Lines/LineStrip/Points)


@contextmanager
def surface_submeshes(mesh):
    """Temporarily hide line/point submeshes, which UnityPy can't turn into triangles."""
    original = mesh.m_SubMeshes
    surfaces = [sm for sm in original or [] if int(sm.topology) in SURFACE_TOPOLOGIES]
    if original and not surfaces:
        raise ValueError("Mesh only has lines/points (no surfaces to show).")
    mesh.m_SubMeshes = surfaces
    try:
        yield len(original or []) - len(surfaces)
    finally:
        mesh.m_SubMeshes = original


def triangle_count(submeshes):
    tris = 0
    for sm in submeshes or []:
        topology, count = int(sm.topology), sm.indexCount
        if topology == 0:
            tris += count // 3
        elif topology == 1:
            tris += max(count - 2, 0)
        elif topology == 2:
            tris += count // 2
    return tris


def mesh_to_meshdata(mesh):
    """UnityPy Mesh -> MeshData (x flipped: Unity is left-handed, so winding flips too)."""
    from UnityPy.helpers.MeshHelper import MeshHandler
    handler = MeshHandler(mesh)
    handler.process()
    if not handler.m_VertexCount or not handler.m_Vertices:
        raise ValueError("Mesh has no vertices (it may be stripped or read/write disabled).")
    points = np.asarray(handler.m_Vertices, dtype=np.float32)[:, :3].copy()
    points[:, 0] *= -1
    count = len(points)
    keep = [i for i, sm in enumerate(mesh.m_SubMeshes or []) if int(sm.topology) in SURFACE_TOPOLOGIES]
    with surface_submeshes(mesh) as skipped:
        triangles = handler.get_triangles()
    submeshes, slots = [], []
    for j, sub in enumerate(triangles):
        tris = [t for t in sub if len(t) == 3]
        if tris:
            submeshes.append(np.asarray(tris, dtype=np.int64)[:, ::-1])
            slots.append(keep[j] if j < len(keep) else j)
    if not submeshes:
        raise ValueError("Mesh has no triangles.")
    normals = None
    if handler.m_Normals and len(handler.m_Normals) == count:
        normals = np.asarray(handler.m_Normals, dtype=np.float32)[:, :3].copy()
        normals[:, 0] *= -1
    uvs = {}
    for i in range(8):
        uv = getattr(handler, f"m_UV{i}", None)
        if uv and len(uv) == count:
            uvs[f"UV{i}"] = np.asarray(uv, dtype=np.float32)[:, :2]
    colors = None
    if handler.m_Colors and len(handler.m_Colors) == count:
        colors = np.asarray(handler.m_Colors, dtype=np.float32)[:, :4]
        if colors.max() > 1.0:
            colors = colors / 255.0
    return MeshData(points, submeshes, normals=normals, uvs=uvs, colors=colors, material_slots=slots,
                    name=mesh.m_Name or "", skipped=skipped)


# TextureFormat -> (channels, numpy dtype) of the float formats: RHalf, RGHalf, RGBAHalf, RFloat, RGFloat, RGBAFloat.
FLOAT_FORMATS = {15: (1, "<f2"), 16: (2, "<f2"), 17: (4, "<f2"), 18: (1, "<f4"), 19: (2, "<f4"), 20: (4, "<f4")}


def float_pixels(tex):
    """(rows top-down, H x W x channels float32) of a half/float Texture2D, else None. These often hold data,
    not colours (vertex animation, lookup tables) with values outside 0..1."""
    fmt = FLOAT_FORMATS.get(int(getattr(tex, "m_TextureFormat", -1) or -1))
    if fmt is None:
        return None
    channels, dtype = fmt
    w, h = tex.m_Width, tex.m_Height
    data = tex.get_image_data() if hasattr(tex, "get_image_data") else tex.image_data
    count = w * h * channels
    arr = np.frombuffer(bytes(data), dtype=dtype, count=count).astype(np.float32)
    return arr.reshape(h, w, channels)[::-1]  # Unity stores the bottom row first


def float_preview(pixels):
    """8-bit RGBA PIL image of float pixels (clamped to 0..1; missing channels 0, alpha 1)."""
    from PIL import Image
    h, w, c = pixels.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[..., 3] = 1.0
    rgba[..., :c] = pixels
    rgba = np.nan_to_num(rgba, nan=0.0, posinf=1.0, neginf=0.0)
    return Image.fromarray((np.clip(rgba, 0.0, 1.0) * 255 + 0.5).astype(np.uint8))  # H x W x 4 -> RGBA


def asset_image(asset):
    """PIL image of a Texture2D/Sprite, with a clear error for textures that have no pixels.

    Some textures (e.g. "Font Texture") are generated while the game runs, so the files only
    hold an empty 0x0 placeholder; UnityPy would otherwise fail with a confusing file error.
    Half/float textures are decoded here: UnityPy's converter fails on values outside 0..1.
    """
    width = getattr(asset, "m_Width", None)
    stream = getattr(asset, "m_StreamData", None)
    if width is not None and (width == 0 or asset.m_Height == 0 or (
            not getattr(asset, "image_data", None) and (stream is None or not stream.path))):
        raise ValueError("Empty texture - it's created while the game runs (e.g. a font), "
                         "so there are no pixels saved in the game files.")
    if width is not None:
        pixels = float_pixels(asset)
        if pixels is not None:
            return float_preview(pixels)
    return asset.image


CUBE_FACES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")  # Unity's face order


def cubemap_faces(cube, max_side=1024):
    """The 6 faces of a parsed Cubemap as PIL images in Unity's order (+X, -X, +Y, -Y, +Z, -Z), at most
    max_side pixels wide. Unity stores the faces one after another (each with its mipmaps)."""
    from UnityPy.enums import BuildTarget
    from UnityPy.export.Texture2DConverter import parse_image_data
    data = cube.get_image_data() if hasattr(cube, "get_image_data") else cube.image_data
    w, h = int(cube.m_Width), int(cube.m_Height)
    if not w or not h or not data:
        raise ValueError("Empty cubemap - it's made while the game runs (e.g. a reflection probe).")
    size = int(getattr(cube, "m_CompleteImageSize", 0) or 0) or len(data) // 6
    reader = getattr(cube, "object_reader", None)
    faces = []
    for i in range(6):
        chunk = bytes(data[i * size:(i + 1) * size])
        if len(chunk) < size:
            raise ValueError("This cubemap has fewer than 6 faces.")
        img = parse_image_data(chunk, w, h, cube.m_TextureFormat, getattr(reader, "version", (0, 0, 0, 0)),
                               getattr(reader, "platform", BuildTarget.UnknownPlatform),
                               getattr(cube, "m_PlatformBlob", None), False)
        if max(img.size) > max_side:
            img = img.resize((max_side, max_side * img.height // img.width))
        faces.append(img.convert("RGB"))
    return faces


def cubemap_cross(faces):
    """6 cube faces (Unity order) laid out as a horizontal cross: +Y on top, -X +Z +X -Z, -Y below."""
    from PIL import Image
    s = faces[0].width
    out = Image.new("RGB", (4 * s, 3 * s), (0, 0, 0))
    for face, (col, row) in zip(faces, ((2, 1), (0, 1), (1, 0), (1, 2), (1, 1), (3, 1))):
        out.paste(face.resize((s, s)), (col * s, row * s))
    return out


def obj_key(assets_file, path_id):
    return (id(assets_file), path_id)


# --------------------------------------------------------------------------- materials

COLOR_PROPS = ("_Color", "_BaseColor", "_MainColor", "_TintColor", "_Albedo", "_BaseColorFactor")


def _prop_name(prop):
    return getattr(prop, "name", prop) if not isinstance(prop, str) else prop


_SHADER_NAMES = {}


def shader_name(ptr):
    """'Legacy Shaders/Diffuse'-style name of a Shader PPtr ('' if unknown). Shader objects keep it in their
    parsed form, not in m_Name; read once per shader."""
    try:
        if not ptr.path_id:
            return ""
        reader = ptr.deref()
    except Exception:
        return ""
    key = (reader.assets_file.name, reader.path_id)
    if key not in _SHADER_NAMES:
        name = ""
        try:
            shader = reader.read()
            parsed = getattr(shader, "m_ParsedForm", None)
            name = (getattr(parsed, "m_Name", "") if parsed is not None else "") or getattr(shader, "m_Name", "") or ""
        except Exception as e:
            log.debug("Shader name of %s: %s", key, e)
        _SHADER_NAMES[key] = name
    return _SHADER_NAMES[key]


def read_material_details(mat):
    """Everything a Unity project needs to rebuild a Material: {"name", "shader", "textures":
    [(property, texture ObjectReader, (scale x, y), (offset x, y))], "colors": {prop: rgba}, "floats": {prop: v},
    "keywords": [..], "queue" (-1 = the shader's), "tags": {name: value}}."""
    saved = mat.m_SavedProperties
    textures = []
    for prop, tex_env in saved.m_TexEnvs:
        tptr = tex_env.m_Texture
        if not tptr.path_id:
            continue
        try:
            reader = tptr.deref()
            if reader.type.name != "Texture2D":
                continue
            sc, off = tex_env.m_Scale, tex_env.m_Offset
            textures.append((_prop_name(prop), reader, (float(sc.x), float(sc.y)), (float(off.x), float(off.y))))
        except Exception:
            continue
    colors = {}
    for prop, c in getattr(saved, "m_Colors", None) or []:
        try:
            colors[_prop_name(prop)] = (float(c.r), float(c.g), float(c.b), float(c.a))
        except (AttributeError, TypeError, ValueError):
            continue
    floats = {}
    for prop, value in getattr(saved, "m_Floats", None) or []:
        try:
            floats[_prop_name(prop)] = float(value)
        except (TypeError, ValueError):
            continue
    keywords = getattr(mat, "m_ValidKeywords", None)
    if keywords is None:
        keywords = (getattr(mat, "m_ShaderKeywords", "") or "").split()
    tags = {}
    for entry in getattr(mat, "stringTagMap", None) or []:
        try:
            k, v = entry if isinstance(entry, (tuple, list)) else (entry.first, entry.second)
            tags[str(k)] = str(v)
        except (TypeError, ValueError, AttributeError):
            continue
    return {"name": mat.m_Name, "shader": shader_name(mat.m_Shader), "textures": textures, "colors": colors, "floats": floats,
            "keywords": list(keywords), "queue": int(getattr(mat, "m_CustomRenderQueue", -1)), "tags": tags}


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
ROLE_NORMAL_WORDS = ("normal", "bump", "nrm")
ROLE_OTHER_WORDS = ("mask", "ao", "occlusion", "metal", "metallic", "rough", "roughness", "smooth", "smoothness",
                    "gloss", "emission", "emissive", "height", "displacement", "parallax", "detail", "noise",
                    "distortion", "distort", "flow", "cubemap", "cube", "reflection", "lightmap", "lightmaps",
                    "screen", "spec", "specular", "ramp", "lut", "dissolve", "matcap", "depth", "shadow")
ROLE_ALBEDO_WORDS = ("albedo", "diffuse", "basecolor", "basecolour", "maintex", "maintexture", "color", "colour",
                     "basemap", "basetex", "albedomap", "diffusemap", "colormap", "colourmap")
SECONDARY_WORDS = ("overlay", "secondary", "layer2", "second", "top", "decal")


def _words(text):
    """'Texture2D_5238b8' / 'Triplanar_Base_Colour' / 'BaseColorMap' -> ['base', 'color', 'map'] (lower case)."""
    text = _CAMEL.sub(" ", str(text or "")).lower()
    return [w for w in re.split(r"[^a-z0-9]+", text) if w and not re.fullmatch(r"[0-9a-f]{8,}|\d+|texture2d", w)]


def texture_role(prop, label=""):
    """(role, rank) of a material texture slot from its property name and the shader's display name for it:
    the engine's albedo/normal slots, else words ("Base Color Map", "Triplanar_Base_Colour", "Mesh_AO").
    rank orders albedo candidates: 0 the standard slots, 1 named like a base color, 2 an overlay/secondary one."""
    if prop in ALBEDO_PROPS:
        return ALBEDO, 0
    if prop in NORMAL_PROPS:
        return NORMAL, 0
    for text in (label, prop):
        words = _words(text)
        if not words:
            continue
        joined = "".join(words)
        if any(w in words for w in ROLE_NORMAL_WORDS) or "normal" in joined:
            return NORMAL, 9
        if any(w in words for w in ROLE_OTHER_WORDS):
            return OTHER, 9
        pairs = {a + b for a, b in zip(words, words[1:])}
        if any(w in ROLE_ALBEDO_WORDS for w in set(words) | pairs) or joined in ("main", "maintexture", "tex"):
            return ALBEDO, 2 if any(w in words for w in SECONDARY_WORDS) else 1
    return OTHER, 9


def shader_labels(shader_ptr):
    """{texture property: the shader's display name for it} (e.g. Shader Graph's "Texture2D_<guid>" ->
    "Base Color Map"), read from the compiled Shader once per shader; {} if it can't be read."""
    try:
        if not shader_ptr.path_id:
            return {}
        reader = shader_ptr.deref()
    except Exception:
        return {}
    cache = _LABELS.setdefault(id(reader.assets_file), {})
    if reader.path_id not in cache:
        labels = {}
        try:
            form = reader.read_typetree().get("m_ParsedForm") or {}
            for prop in (form.get("m_PropInfo") or {}).get("m_Props") or []:
                if prop.get("m_Type") == 4 and prop.get("m_Description") and prop.get("m_Name"):
                    labels[prop["m_Name"]] = str(prop["m_Description"])
        except Exception as e:
            log.debug("Shader labels: %s", e)
        cache[reader.path_id] = labels
    return cache[reader.path_id]


_LABELS = {}  # id(assets file) -> {shader path id: {property: display name}}


def _lod_stem(name):
    """'barrel_T10_LOD2' -> 'barrel_t10' (a mesh name without its LOD suffix, lower case)."""
    return re.sub(r"[\s_\-.]*lod\s*\d+$", "", str(name or ""), flags=re.I).lower()


def _loose(text):
    """Lower case letters and digits only, for matching names written differently ("MAT_Wing Delta")."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


WORLD_MAPPED_WORDS = ("triplanar", "trilinear", "worldspace", "world space", "worldaligned", "world aligned")


def world_mapped(shader, floats):
    """True if a material projects its textures from world space (triplanar rocks, terrain-like pieces) instead of
    the mesh's UVs: by the shader's name, or a triplanar switch that is on (Valheim's _TriplanarMap)."""
    name = (shader or "").lower()
    if any(w in name for w in WORLD_MAPPED_WORDS):
        return True
    return any("triplanar" in prop.lower() and not prop.lower().endswith(("scale", "sharpness", "blend", "pos"))
               and value >= 0.5 for prop, value in (floats or {}).items())


def make_material(mat, asset_for):
    """read_material() dict -> sdk Material. asset_for(texture reader, name) -> the texture's Asset."""
    refs = []
    for prop, tex_name, tex_reader in mat["textures"]:
        scale, offset = mat["st"].get(prop, ((1.0, 1.0), (0.0, 0.0)))
        refs.append(TextureRef(prop, tex_name, asset_for(tex_reader, tex_name), mat["roles"].get(prop, OTHER),
                               mat["labels"].get(prop, ""), scale, offset))
    return Material(mat["name"], refs, color=mat.get("color"), properties=mat.get("properties"),
                    alpha_mode=mat.get("alpha_mode", "opaque"), alpha_cutoff=mat.get("alpha_cutoff", 0.5))


def read_material(mat):
    """A parsed Material -> {"name", "textures": [(property, texture name, ObjectReader)], "color", "properties",
    "roles": {property: ALBEDO/NORMAL/OTHER}, "labels": {property: shader display name}, "st": {property:
    ((scale x, y), (offset x, y))}, "alpha_mode", "alpha_cutoff"}. Textures are sorted best albedo first."""
    saved = mat.m_SavedProperties
    labels = shader_labels(mat.m_Shader)
    textures, roles, ranks, st = [], {}, {}, {}
    for prop, tex_env in saved.m_TexEnvs:
        prop = _prop_name(prop)
        tptr = tex_env.m_Texture
        if not tptr.path_id:
            continue
        try:
            reader = tptr.deref()
            if reader.type.name != "Texture2D":
                continue
            textures.append((prop, reader.peek_name() or prop, reader))
            roles[prop], ranks[prop] = texture_role(prop, labels.get(prop, ""))
            try:
                sc, off = tex_env.m_Scale, tex_env.m_Offset
                st[prop] = ((float(sc.x), float(sc.y)), (float(off.x), float(off.y)))
            except (AttributeError, TypeError, ValueError):
                pass
        except Exception:
            continue
    textures.sort(key=lambda t: (roles[t[0]] != ALBEDO, ranks[t[0]]))
    colors, properties = {}, []
    for prop, c in getattr(saved, "m_Colors", None) or []:
        prop = _prop_name(prop)
        try:
            rgba = (float(c.r), float(c.g), float(c.b), float(c.a))
        except AttributeError:
            continue
        colors[prop] = rgba
        properties.append((prop, "#%02x%02x%02x  alpha %.2f" % tuple(
            [int(max(0, min(1, v)) * 255) for v in rgba[:3]] + [rgba[3]])))
    for prop, value in getattr(saved, "m_Floats", None) or []:
        try:
            properties.append((_prop_name(prop), f"{float(value):.3g}"))
        except (TypeError, ValueError):
            continue
    color_prop = next((p for p in COLOR_PROPS if p in colors), None)
    color = colors[color_prop] if color_prop else None
    shader = shader_name(mat.m_Shader)
    if color_prop == "_TintColor" and "particle" in (shader or "").lower():
        color = tuple(min(1.0, 2.0 * v) for v in color)  # legacy particle shaders draw 2 x _TintColor
    if shader:
        properties.insert(0, ("Shader", shader))
    floats = {}
    for prop, value in getattr(saved, "m_Floats", None) or []:
        try:
            floats[_prop_name(prop)] = float(value)
        except (TypeError, ValueError):
            continue
    keywords = getattr(mat, "m_ValidKeywords", None)
    if keywords is None:
        keywords = (getattr(mat, "m_ShaderKeywords", "") or "").split()
    tags = {}
    for entry in getattr(mat, "stringTagMap", None) or []:
        try:
            k, v = entry if isinstance(entry, (tuple, list)) else (entry.first, entry.second)
            tags[str(k)] = str(v)
        except (TypeError, ValueError, AttributeError):
            continue
    alpha_mode, cutoff = material_alpha(shader, floats, keywords, int(getattr(mat, "m_CustomRenderQueue", -1)), tags)
    if world_mapped(shader, floats):
        st = {}  # its tiling is per world unit, not for the mesh's UVs
    for prop, label in labels.items():
        if prop in roles:
            properties.append((f"{prop} (shader name)", label))
    return {"name": mat.m_Name, "textures": textures, "color": color, "properties": properties,
            "alpha_mode": alpha_mode, "alpha_cutoff": cutoff, "roles": roles,
            "labels": {k: v for k, v in labels.items() if k in roles}, "st": st}


MASK_KEYWORDS = {"_ALPHATEST_ON", "_ALPHA_CLIP", "_ALPHACLIP_ON"}
BLEND_KEYWORDS = {"_ALPHABLEND_ON", "_ALPHAPREMULTIPLY_ON", "_SURFACE_TYPE_TRANSPARENT", "_BLENDMODE_ALPHA"}
CUTOFF_PROPS = ("_Cutoff", "_AlphaClipThreshold", "_AlphaCutoff")
MASK_WORDS = ("cutout", "alphatest", "alpha test", "foliage", "leaves", "leaf", "grass")
BLEND_WORDS = ("transparent", "translucent", "fade", "glass", "alpha blend", "shield", "hologram", "forcefield",
               "force field", "water", "particle", "additive")


def is_additive(shader, floats):
    """True if a material adds its color onto what's behind it (black = invisible): glows, sparks, flashes.
    Legacy/mobile "Additive" shaders, URP _Blend 2, HDRP _BlendMode 1, Standard Particles _Mode 4, or
    One/SrcAlpha + One blending."""
    floats = floats or {}
    if "additive" in (shader or "").lower():
        return True
    if floats.get("_Surface") == 1 and floats.get("_Blend") == 2:  # URP transparent, Additive
        return True
    if floats.get("_SurfaceType") == 1 and floats.get("_BlendMode") == 1:  # HDRP transparent, Additive
        return True
    if "particle" in (shader or "").lower() and floats.get("_Mode") == 4:  # Standard Particles, Additive
        return True
    return floats.get("_SrcBlend") in (1, 5) and floats.get("_DstBlend") == 1


def material_alpha(shader, floats, keywords, queue=-1, tags=None):
    """("opaque" | "mask" | "blend" | "add", cutoff) of a Unity material: what its shader does with the albedo
    alpha (the 3D view draws it see-through, Unity project export picks a matching shader). "add" is a
    blended material whose shader adds it on top (is_additive). The material's own switches (keywords,
    URP _Surface/_AlphaClip, HDRP flags, Standard _Mode) win; then the RenderType tag, the render queue and
    finally words in the shader's name."""
    if "additive" in (shader or "").lower():
        return "add", _cutoff(floats)  # legacy additive shaders ignore any leftover surface switches
    mode, cutoff = _surface_alpha(shader, floats, keywords, queue, tags)
    if mode == "blend" and is_additive(shader, floats):
        return "add", cutoff
    return mode, cutoff


def _cutoff(floats):
    cutoff = next((floats[p] for p in CUTOFF_PROPS if p in floats), 0.5) if floats else 0.5
    return min(max(cutoff, 0.01), 0.99)


def _surface_alpha(shader, floats, keywords, queue, tags):
    floats = floats or {}
    cutoff = _cutoff(floats)
    keywords = {str(k).upper() for k in keywords or ()}
    if keywords & BLEND_KEYWORDS:
        return "blend", cutoff
    if keywords & MASK_KEYWORDS:
        return "mask", cutoff
    if floats.get("_Surface") == 1 or floats.get("_SurfaceType") == 1:  # URP / HDRP transparent surface
        return "blend", cutoff
    if floats.get("_AlphaClip") == 1 or floats.get("_AlphaCutoffEnable") == 1:  # URP / HDRP alpha clipping
        return "mask", cutoff
    if "_Surface" in floats or "_SurfaceType" in floats:
        return "opaque", cutoff  # URP / HDRP opaque (a leftover Standard _Mode doesn't count)
    if floats.get("_Mode") in (1, 2, 3):  # Standard: Opaque, Cutout, Fade, Transparent
        return ("mask" if floats["_Mode"] == 1 else "blend"), cutoff
    render_type = ((tags or {}).get("RenderType") or "").lower()
    if render_type == "transparent" or queue >= 2750:
        return "blend", cutoff
    if render_type == "transparentcutout" or 2400 <= queue < 2750:
        return "mask", cutoff
    name = (shader or "").lower()
    if any(w in name for w in BLEND_WORDS):
        return "blend", cutoff
    if any(w in name for w in MASK_WORDS):
        return "mask", cutoff
    return "opaque", cutoff


# --------------------------------------------------------------------------- material index

def bind_matrices(bind):
    """A mesh's m_BindPose (Matrix4x4f list) as a (B, 4, 4) array."""
    out = []
    for m in bind or []:
        if hasattr(m, "e00"):
            out.append([[getattr(m, f"e{r}{c}") for c in range(4)] for r in range(4)])
        else:
            out.append(list(m) if len(m) == 4 else [m[i * 4:(i + 1) * 4] for i in range(4)])
    return np.asarray(out, float).reshape(-1, 4, 4)


def bones_from_bind(bone_keys, bind, nodes):
    """Bones of a skinned mesh in its bind pose (UniView space): a bone sits at its inverse bind pose's origin;
    its parent is the nearest ancestor transform (from nodes, see UnitySession.transforms) that is a bone too."""
    from .sdk import Bones
    n = min(len(bone_keys), len(bind))
    if not n:
        return None
    keys = list(bone_keys[:n])
    points = np.array([np.linalg.inv(b)[:3, 3] if abs(np.linalg.det(b)) > 1e-12 else np.zeros(3)
                       for b in bind[:n]], float)
    points[:, 0] *= -1  # UniView meshes are x-flipped
    index = {k: i for i, k in enumerate(keys) if k is not None}
    parents, names = [], []
    for k in keys:
        node = nodes.get(k) if k is not None else None
        names.append(node["name"] if node else "?")
        p, hops = (node or {}).get("parent"), 0
        while p is not None and p not in index and hops < 64:
            p, hops = (nodes.get(p) or {}).get("parent"), hops + 1
        parents.append(index.get(p, -1) if p is not None else -1)
    return Bones(points, parents, names)


class TextureFinder:
    """Finds what uses a mesh and which materials/textures it has (renderer -> material).

    Big games have a million+ MeshFilters/MeshRenderers, so the index only reads the first
    pointers of each component (its GameObject, and a MeshFilter's mesh) straight from the
    file bytes; materials are read on demand. The index is built in the background right after
    a game loads, a chunk at a time under the session lock, and whoever needs it first finishes it.
    """

    CHUNK = 4000      # objects handled per lock hold (keeps the UI responsive)
    MAX_USERS = 40    # GameObject names resolved per model

    def __init__(self, env, lock):
        self.env = env
        self.lock = lock
        self._steps = None
        self._indexed = False   # mesh -> users index done
        self._finished = False  # texture -> models index done too (or gave up)
        self._mesh_users = {}   # mesh key -> [(assets file, GameObject path id)]
        self._renderers = {}    # GameObject key -> MeshRenderer ObjectReader
        self._skinned = {}      # mesh key -> [SkinnedMeshRenderer ObjectReader]
        self._files = {}        # (id(assets file), file id) -> target assets file or None
        self._tex_users = {}    # texture key -> {mesh keys}

    # ---- building
    def start_background(self):
        def work():
            while not self._finished:
                with self.lock:
                    self._step()

        threading.Thread(target=work, daemon=True, name="mesh-index").start()

    def stop(self):
        """Give up on the background indexing (the game is being unloaded)."""
        self._finished = True

    @property
    def indexed(self):
        return self._indexed or self._finished

    def _run_until(self, done):
        with self.lock:
            while not done() and not self._finished:
                self._step()

    def _step(self):
        """Advance the index by one chunk. Callers hold the lock."""
        if self._finished:
            return
        if self._steps is None:
            self._steps = self._build_steps()
        try:
            next(self._steps)
        except StopIteration:
            self._indexed = self._finished = True
        except Exception:
            log.exception("Indexing which models use which materials failed")
            self._indexed = self._finished = True

    def _build_steps(self):
        log.info("Indexing which objects use which meshes/materials...")
        started = time.time()
        for n, obj in enumerate(self.env.objects, 1):
            if n % self.CHUNK == 0:
                yield
            tname = obj.type.name
            try:
                if tname == "MeshFilter":
                    ptrs = self._read_pptrs(obj, 2)
                    if ptrs and ptrs[1][1]:
                        go, mesh = ptrs
                        self._mesh_users.setdefault(obj_key(*mesh), []).append(go)
                elif tname == "MeshRenderer":
                    ptrs = self._read_pptrs(obj, 1)
                    if ptrs:
                        self._renderers[obj_key(*ptrs[0])] = obj
                elif tname == "SkinnedMeshRenderer":
                    mesh_ptr = obj.read().m_Mesh
                    if mesh_ptr.path_id:
                        mesh = mesh_ptr.deref()
                        self._skinned.setdefault(obj_key(mesh.assets_file, mesh.path_id), []).append(obj)
            except Exception:
                continue
        self._indexed = True
        log.info("Indexed %d meshes with renderers in %.1fs",
                 len(set(self._mesh_users) | set(self._skinned)), time.time() - started)
        yield

        # Second pass for the texture view's "used by": one renderer's materials per model.
        started = time.time()
        material_textures = {}
        for n, mesh_key in enumerate(set(self._mesh_users) | set(self._skinned), 1):
            if n % 500 == 0:
                yield
            for ptr in self._material_ptrs(mesh_key, first_only=True):
                try:
                    mat_key = (id(ptr.assetsfile), ptr.path_id)
                except Exception:
                    continue
                if mat_key not in material_textures:
                    material_textures[mat_key] = self._texture_keys(ptr)
                for tex_key in material_textures[mat_key]:
                    self._tex_users.setdefault(tex_key, set()).add(mesh_key)
        log.info("Indexed which models use which textures (%d textures) in %.1fs",
                 len(self._tex_users), time.time() - started)

    def _read_pptrs(self, obj, count):
        """Read the first `count` PPtrs of a component without parsing it: [(assets file, path id)]."""
        assets_file = obj.assets_file
        wide = assets_file.header.version >= 14
        reader = obj.reader
        obj.reset()
        out = []
        for _ in range(count):
            file_id = reader.read_int()
            path_id = reader.read_long() if wide else reader.read_int()
            target = self._file(assets_file, file_id)
            if target is None:
                return None
            out.append((target, path_id))
        return out

    def _file(self, assets_file, file_id):
        """Assets file a PPtr's file id points to (same lookup as UnityPy's PPtr.deref)."""
        key = (id(assets_file), file_id)
        if key not in self._files:
            target = assets_file if file_id == 0 else None
            if 0 < file_id <= len(assets_file.externals):
                name = assets_file.externals[file_id - 1].path
                name = (name[9:] if name.startswith("archive:/") else name).rsplit("/", 1)[-1].lower()
                container = assets_file.parent
                if container is not None:
                    target = next((f for k, f in container.files.items() if k.lower() == name), None)
                if target is None:
                    try:
                        target = assets_file.environment.find_file(name)
                    except Exception:
                        target = None
            self._files[key] = target
        return self._files[key]

    # ---- lookups
    def _material_ptrs(self, mesh_key, first_only=False):
        for assets_file, go_id in self._mesh_users.get(mesh_key, []):
            renderer = self._renderers.get(obj_key(assets_file, go_id))
            if renderer is not None:
                try:
                    return list(renderer.read().m_Materials)
                except Exception:
                    if first_only:
                        break
        for renderer in self._skinned.get(mesh_key, []):
            try:
                return list(renderer.read().m_Materials)
            except Exception:
                continue
        return []

    @staticmethod
    def _object_name(reader):
        try:
            return reader.peek_name() or reader.read().m_Name
        except Exception:
            return "?"

    def info(self, mesh_obj):
        """Return (gameobject names, materials, number of users).

        names: up to MAX_USERS GameObjects using the mesh.
        materials: [{"name": str, "textures": [(property, texture name, ObjectReader)]}],
        one per submesh slot, with albedo textures first.
        """
        self._run_until(lambda: self._indexed)
        with self.lock:
            key = obj_key(mesh_obj.assets_file, mesh_obj.path_id)
            gos = self._mesh_users.get(key, [])
            skinned = self._skinned.get(key, [])
            names = []
            for assets_file, go_id in gos[:self.MAX_USERS]:
                reader = assets_file.objects.get(go_id)
                if reader is not None:
                    names.append(self._object_name(reader))
            for renderer in skinned[:max(0, self.MAX_USERS - len(names))]:
                try:
                    names.append(self._object_name(renderer.read().m_GameObject.deref()))
                except Exception:
                    pass
            materials = []
            for ptr in self._material_ptrs(key):
                try:
                    materials.append(read_material(ptr.deref_parse_as_object()))
                except Exception:
                    continue
            return names, materials, len(gos) + len(skinned)

    def texture_users(self, tex_obj):
        """Keys of the meshes whose materials use this texture."""
        self._run_until(lambda: False)
        return self._tex_users.get(obj_key(tex_obj.assets_file, tex_obj.path_id), set())

    @staticmethod
    def _texture_keys(mat_ptr):
        try:
            tex_envs = mat_ptr.deref_parse_as_object().m_SavedProperties.m_TexEnvs
        except Exception:
            return ()
        keys = []
        for _prop, tex_env in tex_envs:
            ptr = tex_env.m_Texture
            if ptr.path_id:
                try:
                    reader = ptr.deref()
                    keys.append(obj_key(reader.assets_file, reader.path_id))
                except Exception:
                    pass
        return keys



# --------------------------------------------------------------------------- scripts (MonoBehaviour data)

UNDER_READ = re.compile(r"Expected to read (\d+) bytes, but only read (\d+) bytes")


def resource_paths(env):
    """{(file name lowercase, path id): "Resources/<path>"} from the build's Resources index
    (globalgamemanagers' ResourceManager). UnityPy attaches AssetBundle paths to objects but not these.
    Which Resources folder an entry came from isn't stored, so they're all under one "Resources/"."""
    out = {}
    for f in getattr(env, "assets", []):
        if (getattr(f, "name", "") or "").lower() != "globalgamemanagers":
            continue
        for obj in f.objects.values():
            if obj.type.name != "ResourceManager":
                continue
            try:
                tree = obj.read_typetree()
            except Exception as e:
                log.debug("Resources index unreadable: %s", e)
                return out
            externals = f.externals
            for entry in tree.get("m_Container") or []:
                try:
                    path, ptr = entry
                    fid, pid = ptr.get("m_FileID", 0), ptr.get("m_PathID", 0)
                    if not pid or not path:
                        continue
                    fname = f.name if fid == 0 else os.path.basename(externals[fid - 1].path.replace("\\", "/"))
                    out.setdefault((fname.lower(), pid), "Resources/" + path)
                except (IndexError, TypeError, ValueError, AttributeError):
                    continue
            if out:
                log.info("%d path(s) from the Resources index", len(out))
            return out
    return out


def _aligned_nodes(gen):
    """get_nodes_up for a TypeTreeGenerator, with MonoBehaviour's m_Enabled marked as 4-byte aligned.

    The IL2CPP (AssetStudio) backend leaves the flag off, so every field after it was read 3 bytes
    early. The C field reader copies nodes when first used, so they're rebuilt rather than patched.
    """
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    original = gen.get_nodes_up
    cache = {}

    def get_nodes_up(assembly, fullname):
        key = (assembly, fullname)
        if key not in cache:
            try:
                with _quiet_native_output():  # the native generator prints every failure to the console
                    root = original(assembly, fullname)
            except Exception as e:
                # Remember the failure: a class that can't be laid out fails the same way for each of its
                # objects, and a game can have thousands (UnityScript assemblies the generator can't load).
                cache[key] = e
                raise
            flat = []

            def walk(node):
                flag = node.m_MetaFlag or 0
                if node.m_Level == 1 and node.m_Name == "m_Enabled" and node.m_Type == "UInt8":
                    flag |= 0x4000
                flat.append(TypeTreeNode(node.m_Level, node.m_Type, node.m_Name, 0, 0, m_MetaFlag=flag))
                for child in node.m_Children or []:
                    walk(child)

            walk(root)
            cache[key] = TypeTreeNode.from_list(flat)
        if isinstance(cache[key], Exception):
            raise cache[key]
        return cache[key]
    return get_nodes_up


_quiet_lock = threading.Lock()


_std_handles_silenced = False


def _silence_native_std_handles():
    """Point the Win32 standard output/error handles at NUL, once and for good.

    Native and .NET code writes through those handles; Python's streams (and faulthandler) use the C
    runtime's fds 1/2, which keep their own handles, so they still reach the console or log. Swapping fds
    1/2 around each call instead crashed the app: the .NET type tree generator caches the handle it first
    wrote to, dup2 closes that handle when the fds are restored, and its next write ended the process."""
    global _std_handles_silenced
    if _std_handles_silenced:
        return True
    try:
        import ctypes
        import msvcrt
        nul = msvcrt.get_osfhandle(os.open(os.devnull, os.O_WRONLY))  # kept open for the app's lifetime
        kernel32 = ctypes.windll.kernel32
        kernel32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        for std in (-11 & 0xFFFFFFFF, -12 & 0xFFFFFFFF):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            kernel32.SetStdHandle(std, nul)
    except (ImportError, OSError, AttributeError) as e:
        log.debug("Couldn't silence native output: %s", e)
        return False
    _std_handles_silenced = True
    return True


@contextmanager
def _quiet_native_output():
    """Send what native code writes to stdout/stderr to nowhere (Python's own streams are left alone)."""
    if sys.platform == "win32" and _silence_native_std_handles():
        yield
        return
    with _quiet_lock:
        saved = []
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
        except OSError:
            yield
            return
        try:
            for fd in (1, 2):
                try:
                    saved.append((fd, os.dup(fd)))
                    os.dup2(devnull, fd)
                except OSError:
                    pass
            yield
        finally:
            for fd, copy in saved:
                os.dup2(copy, fd)
                os.close(copy)
            os.close(devnull)


def _warm_cpp2il(path):
    """Start Cpp2IL on an IL2CPP game in the background while it opens: the first script view (or export)
    then finds the result cached instead of waiting up to a minute."""
    from . import cpp2il
    game_dir = path if os.path.isdir(path) else os.path.dirname(path)
    if not cpp2il.is_il2cpp(game_dir):
        game_dir = os.path.dirname(game_dir)  # opened the _Data folder itself
    if cpp2il.cpp2il_path() is None or not cpp2il.is_il2cpp(game_dir):
        return

    def work():
        try:
            cpp2il.stub_assemblies(game_dir)
        except Exception as e:
            log.warning("Cpp2IL couldn't rebuild the game's code: %s", e)

    threading.Thread(target=work, daemon=True, name="cpp2il").start()


class ScriptReader:
    """Reads MonoBehaviour fields. Cooked games don't store script field layouts, so they're generated
    from the game's code (Managed/*.dll for Mono, GameAssembly.dll + metadata for IL2CPP) with
    TypeTreeGeneratorAPI, trying a second backend when the first fails for a class."""

    def __init__(self, env, game_dir):
        self.env = env
        self.game_dir = game_dir
        self.generators = None
        self.error = ""
        self._embedded = None  # script class -> field layout stored in some file (usually an AssetBundle)
        self._ref_types = []   # [SerializeReference] type layouts stored in those files

    def _setup(self):
        if self.generators is not None:
            return
        self.generators = []
        try:
            from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
        except ImportError:
            self.error = "Install TypeTreeGeneratorAPI to read script fields (py -m pip install TypeTreeGeneratorAPI)."
            return
        version = next((f.unity_version for f in getattr(self.env, "assets", []) if getattr(f, "unity_version", "")), "")
        il2cpp = os.path.isfile(os.path.join(self.game_dir, "GameAssembly.dll"))
        stubs = None
        if il2cpp:
            # Cpp2IL's rebuilt assemblies read with the Mono generator: the native IL2CPP one gets some
            # layouts wrong and can crash the whole app on others.
            from . import cpp2il
            try:
                stubs = cpp2il.stub_assemblies(self.game_dir)
            except Exception as e:
                log.warning("Cpp2IL couldn't rebuild the game's code (%s) - using the IL2CPP reader", e)
        if stubs:
            try:
                gen = TypeTreeGenerator(version, "AssetsTools")
                gen.load_local_dll_folder(stubs)
                gen.get_nodes_up = _aligned_nodes(gen)
                self.generators.append(gen)
                log.info("Script field layouts from the game's IL2CPP code, rebuilt by Cpp2IL")
                return
            except Exception as e:
                log.warning("Couldn't load Cpp2IL's assemblies (%s) - using the IL2CPP reader", e)
        for backend in (("AssetStudio", "AssetsTools") if il2cpp else ("AssetsTools", "AssetStudio")):
            try:
                gen = TypeTreeGenerator(version, backend)
                gen.load_local_game(self.game_dir)
                gen.get_nodes_up = _aligned_nodes(gen)
                self.generators.append(gen)
            except Exception as e:
                self.error = f"Couldn't load the game's code: {e}"
        if self.generators:
            log.info("Script field layouts from the game's %s code (%s)", "IL2CPP" if il2cpp else "Mono",
                     ", ".join(type(g).__name__ for g in self.generators))

    def _embedded_layouts(self):
        """Field layouts that AssetBundles store with their objects, by script class. The same class in a
        file without layouts (resources.assets, sharedassets) can borrow them: they come from the build
        itself and include [SerializeReference] fields the code-based generators leave out."""
        if self._embedded is None:
            self._embedded = {}
            seen, files = set(), set()
            for obj in self.env.objects:
                if id(obj.assets_file) not in files:
                    files.add(id(obj.assets_file))
                    self._ref_types += [r for r in getattr(obj.assets_file, "ref_types", None) or () if r.node is not None]
                st = obj.serialized_type
                if obj.type.name != "MonoBehaviour" or st is None or st.node is None:
                    continue
                key = (id(obj.assets_file), id(st))  # one script per type entry of a file
                if key in seen:
                    continue
                seen.add(key)
                cls = script_class(obj)
                if cls:
                    self._embedded.setdefault(cls, st.node)
            if self._embedded:
                log.info("%d script layout(s) found stored with bundle objects", len(self._embedded))
        return self._embedded

    def read(self, obj):
        """(dict of fields, note)"""
        st = obj.serialized_type
        if st is not None and st.node is not None:
            try:
                return obj.read_typetree(), ""  # stored with the object (AssetBundles): no need for the game's code
            except Exception as e:
                log.debug("Stored layout of '%s' didn't fit: %s", script_class(obj), e)
        else:
            node = self._embedded_layouts().get(script_class(obj))
            if node is not None:
                f = obj.assets_file
                own_refs = f.ref_types
                # Layouts of the objects its [SerializeReference] fields hold (this file lists their names only).
                f.ref_types = self._ref_types + [r for r in own_refs or () if r.node is not None]
                try:
                    return obj.read_typetree(nodes=node), ""
                except Exception as e:  # a different version of the class: generate the layout instead
                    log.debug("Bundle layout of '%s' didn't fit: %s", script_class(obj), e)
                finally:
                    f.ref_types = own_refs
        self._setup()
        previous = self.env.typetree_generator
        best = None  # (bytes read, generator) of the layout that got furthest without overrunning
        try:
            for gen in self.generators:
                self.env.typetree_generator = gen
                try:
                    return obj.read_typetree(), ""
                except Exception as e:
                    m = UNDER_READ.search(str(e))
                    if m and (best is None or int(m.group(2)) > best[0]):
                        best = (int(m.group(2)), gen)
            if best is not None:
                # The generated layout ends before the data does: usually the registry of
                # [SerializeReference] objects Unity appends at the end, which the generators leave out.
                read, gen = best
                self.env.typetree_generator = gen
                try:
                    tree = obj.read_typetree(check_read=False)
                except Exception:
                    tree = None
                if tree is not None:
                    tail = bytes(obj.get_raw_data())[read:]
                    if len(tail) == 8 and tail[4:] == bytes(4) and tail[:4] in (b"\1\0\0\0", b"\2\0\0\0"):
                        tree["references"] = {"version": tail[0], "RefIds": []}
                        return tree, ""
                    _add_strings(tree, tail)
                    return tree, (f"The last {len(tail):,} bytes of this object couldn't be decoded (objects "
                                  "stored by [SerializeReference], or fields the reader doesn't know); "
                                  "every field above them is shown.")
        finally:
            self.env.typetree_generator = previous
        # Only the fields every MonoBehaviour has.
        try:
            tree = obj.read_typetree(check_read=False)
        except Exception:
            tree = {}
        try:
            _add_strings(tree, bytes(obj.get_raw_data()), skip=(tree.get("m_Name"),))
        except Exception:
            pass
        note = self.error or "This script's own fields couldn't be decoded (its class uses features the field " \
                             "reader doesn't support yet); showing the common fields only."
        return tree, note

    def layout(self, obj):
        """Field layout (TypeTreeNode) that reads all of obj's data, or None. Writing the object back needs
        one: a layout that stops early would cut off the fields after it."""
        st = obj.serialized_type
        candidates = []
        if st is not None and st.node is not None:
            candidates.append(st.node)
        else:
            node = self._embedded_layouts().get(script_class(obj))
            if node is not None:
                candidates.append(node)
        for node in candidates:
            try:
                obj.read_typetree(nodes=node)
                return node
            except Exception:
                pass
        self._setup()
        previous = self.env.typetree_generator
        try:
            for gen in self.generators:
                self.env.typetree_generator = gen
                try:
                    node = obj._get_typetree_node()
                    obj.read_typetree(nodes=node)
                    return node
                except Exception:
                    continue
        finally:
            self.env.typetree_generator = previous
        return None


def find_strings(data, min_len=2):
    """Text stored the way Unity serializes strings (int32 length, UTF-8, aligned to 4) in undecoded bytes."""
    found, pos, end = [], 0, len(data) - 4
    while pos <= end:
        n = int.from_bytes(data[pos:pos + 4], "little")
        if min_len <= n <= 4096 and pos + 4 + n <= len(data):
            chunk = data[pos + 4:pos + 4 + n]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text.isprintable() and any(c.isalnum() for c in text):
                found.append(text)
                pos += (4 + n + 3) & ~3
                continue
        pos += 4
    return found


def _add_strings(tree, data, skip=()):
    """Add the text found in bytes the reader couldn't decode, so names/values can still be searched."""
    strings = [t for t in find_strings(data) if t not in skip]
    if strings:
        tree["(text in undecoded data)"] = strings


def _path_closeness(a, b):
    """How many leading folders two asset paths share (+1 when they're the same file)."""
    if not a or not b:
        return 0
    pa, pb = a.lower().split("/"), b.lower().split("/")
    n = 0
    while n < min(len(pa), len(pb)) - 1 and pa[n] == pb[n]:
        n += 1
    return n + (a.lower() == b.lower())


def script_class(obj):
    """'Namespace.ClassName' of a MonoBehaviour's script ('PhysicMaterial' etc. for built-in data), or ''."""
    if getattr(getattr(obj, "type", None), "name", "") in BUILTIN_DATA:
        return obj.type.name
    try:
        script = obj.read(check_read=False).m_Script.read()
        ns, cls = getattr(script, "m_Namespace", ""), getattr(script, "m_ClassName", "")
        return f"{ns}.{cls}" if ns else cls
    except Exception:
        return ""


def json_safe(value):
    """typetree values -> JSON-friendly (bytes shortened)."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return f"<{len(data)} bytes>" if len(data) > 64 else data.hex()
    if isinstance(value, float) and value != value:
        return "NaN"
    return value


# --------------------------------------------------------------------------- session

# Project settings kept in globalgamemanagers that an editor project can take back as they are.
SETTINGS_TYPES = ("TagManager", "PhysicsManager", "Physics2DSettings", "InputManager", "TimeManager", "AudioManager",
                  "QualitySettings", "NavMeshProjectSettings")
BUILTIN_TAGS = {0: "Untagged", 1: "Respawn", 2: "Finish", 3: "EditorOnly", 5: "MainCamera", 6: "Player",
                7: "GameController"}


class UnitySession(GameSession):
    def __init__(self, plugin, path, env, file_count):
        super().__init__(plugin, path)
        self.env = env
        self.file_count = file_count
        self.finder = TextureFinder(env, self.lock)
        self.by_key = {}
        self._paths = None
        self._transforms = None
        self._hierarchy_done = False
        self._scenes = {}
        self._script_reader = None
        self._sheets = None
        self._skins = {}
        self._terrains = {}
        self._rigs = None        # humanoid Avatars (unity_humanoid.HumanRig)
        self._scene_settings = {}  # id(scene file) -> (RenderSettings tree, lightmaps)
        self._lod_index = None     # renderer key -> (LODGroup, level), for lod_siblings
        self._container_scripts = None  # container -> its MonoBehaviours (for _data_materials)
        self._material_names = {}        # material name -> reader
        self._material_keys = []         # (loose name, loose .mat file name, name length, reader)
        self._model_rigs = {}    # mesh key -> the HumanRig that fits its skeleton, or None
        self._humanoid = {}      # (clip key, rig) -> clip converted to bone curves
        self._material_readers = {}  # "material:<file>:<id>" -> ObjectReader (filled by hierarchy())
        self._tags = None            # custom tag names (TagManager)
        self._tmp_fonts = {}         # obj key -> tmp_font() result (TextMeshPro font assets)
        self._controller_readers = {}  # "controller:<file>:<id>" -> ObjectReader (filled by controllers())

    def _asset_for(self, obj, type_name="Texture2D", name=None):
        """Asset for an ObjectReader (the listed one if we have it)."""
        key = obj_key(obj.assets_file, obj.path_id)
        asset = self.by_key.get(key)
        if asset is None:
            asset = Asset(TYPE_KINDS.get(type_name, "file"), name or obj.peek_name() or type_name, key,
                          uid=f"{obj.assets_file.name}:{obj.path_id}", size=obj.byte_size,
                          path=getattr(obj, "container", None) or "", source=obj.assets_file.name, ref=obj)
        return asset

    def start_background(self):
        self.finder.start_background()

    def close(self):
        self.finder.stop()

    def options_changed(self):
        self._scenes.clear()

    # ------------------------------------------------------------------ terrains

    @staticmethod
    def _is_terrain(asset):
        return isinstance(asset.ref, dict) and asset.ref.get("type") in ("terrain", "terrain_tex")

    def terrain(self, obj, grid=None):
        """(MeshData, Material, info rows, heightmap resolution) of a TerrainData reader, cached."""
        from . import unity_terrain
        key = (obj_key(obj.assets_file, obj.path_id), grid)
        if key not in self._terrains:
            with self.lock:
                tree = obj.read_typetree()
            if grid:
                old, unity_terrain.MAX_GRID = unity_terrain.MAX_GRID, grid
            try:
                md, size = unity_terrain.terrain_mesh(tree)
            finally:
                if grid:
                    unity_terrain.MAX_GRID = old
            name = tree.get("m_Name") or "Terrain"
            md.name = name
            tex = Asset("texture", f"{name} (blended terrain layers)", ("terrain_tex",) + key[0],
                        uid=f"terrain_tex:{obj.assets_file.name}:{obj.path_id}", source=obj.assets_file.name,
                        ref={"type": "terrain_tex", "obj": obj, "tree": tree, "size": size})
            hm = tree["m_Heightmap"]
            splat = tree.get("m_SplatDatabase") or {}
            layers = len(splat.get("m_TerrainLayers") or splat.get("m_Splats") or [])
            res = hm.get("m_Resolution") or hm.get("m_Width")
            info = [("Heightmap", f"{res}\u00d7{res}"),
                    ("Size", f"{size[0]:g} \u00d7 {size[1]:g} m, {hm['m_Scale']['y']:g} m high"),
                    ("Terrain layers", str(layers))]
            if splat.get("m_AlphaTextures") and layers:
                material = Material(f"{name} layers", [TextureRef("_Splat", tex.name, tex, ALBEDO)])
            else:  # never painted (or drawn by a custom material): plain ground color
                material = Material(f"{name} layers", [], color=(0.42, 0.5, 0.3, 1.0))
            self._terrains[key] = (md, material, info, res)
        return self._terrains[key]

    def _terrain_image(self, ref):
        from .unity_terrain import terrain_texture
        if "image" not in ref:
            with self.lock:
                ref["image"] = terrain_texture(self, ref["obj"], ref["tree"], ref["size"])
        if ref["image"] is None:
            raise ValueError("This terrain has no painted layers.")
        return ref["image"]

    def image(self, asset):
        if self._is_terrain(asset):
            return self._terrain_image(asset.ref)
        if asset.kind == "font":
            from .sdk import font_preview
            return font_preview(self.raw(asset), asset.name)
        if getattr(asset.ref, "type", None) is not None and asset.ref.type.name == "Cubemap":
            return cubemap_cross(self.cube_faces(asset, 512))
        return asset_image(asset.ref.read())

    def cube_faces(self, asset, max_side=1024):
        """The 6 faces of a Cubemap asset (Unity order +X, -X, +Y, -Y, +Z, -Z)."""
        with self.lock:
            return cubemap_faces(asset.ref.read(), max_side)

    def float_texture(self, asset):
        """(pixels H x W x C float32, settings dict) of a half/float Texture2D, else None. Settings: the
        game's filter_mode, wrap_mode, mipmaps and whether it was 32-bit float."""
        if asset.kind != "texture" or self._is_terrain(asset):
            return None
        tex = asset.ref.read()
        if int(getattr(tex, "m_TextureFormat", -1) or -1) not in FLOAT_FORMATS or not tex.m_Width:
            return None
        ts = getattr(tex, "m_TextureSettings", None)
        return float_pixels(tex), {
            "filter_mode": int(getattr(ts, "m_FilterMode", 1)),
            "wrap_mode": int(getattr(ts, "m_WrapU", getattr(ts, "m_WrapMode", 0)) or 0),
            "mipmaps": int(getattr(tex, "m_MipCount", 1) or 1) > 1,
            "float32": FLOAT_FORMATS[int(tex.m_TextureFormat)][1] == "<f4",
        }

    def mesh(self, asset):
        if self._is_terrain(asset):
            md = self.terrain(asset.ref["obj"])[0]
            asset.ref["resolution"] = self.terrain(asset.ref["obj"])[3]
            return md
        if asset.kind == "scene":
            return self._scene(asset)[0]
        return mesh_to_meshdata(asset.ref.read())

    def _scene_roots(self, asset):
        """Root Transform readers of a scene or prefab asset."""
        ref = asset.ref
        if ref["type"] == "scene":
            roots = []
            for t in ref["transforms"]:
                try:
                    if not t.read().m_Father.path_id:
                        roots.append(t)
                except Exception:
                    continue
            return roots
        if ref["type"] == "root":
            return [ref["transform"]]
        # A prefab bundle entry: find its root GameObject's transform.
        gos = ref["gameobjects"]
        base = os.path.splitext(os.path.basename(asset.path))[0].lower()
        go = next((g for g in gos if (g.peek_name() or "").lower() == base), gos[0])
        from .unity_scene import _components
        transform = next((r for n, r in _components(go.read()) if n in ("Transform", "RectTransform")), None)
        if transform is None:
            raise ValueError("This prefab has no transform.")
        for _ in range(200):
            father = transform.read().m_Father
            if not father.path_id:
                break
            transform = father.deref()
        return [transform]

    def is_ui(self, asset):
        """True for a prefab whose root is a UI object (RectTransform): drawn as a 2D picture, not in 3D."""
        if asset.kind != "scene" or asset.ref["type"] == "scene":
            return False
        try:
            with self.lock:
                return all(r.type.name == "RectTransform" for r in self._scene_roots(asset))
        except Exception:
            return False

    def sprite_info(self, asset):
        """{"border": [left, bottom, right, top] in pixels, "ppu": pixels per unit, "size": [w, h] of the sprite's
        rect, "trim": [x, y from the bottom, w, h] - the part of that rect image() returns (trimmed of empty
        space)} of a sprite, or None."""
        if asset.kind != "sprite":
            return None
        with self.lock:
            tree = asset.ref.read_typetree()
        border = tree.get("m_Border") or {}
        rect = tree.get("m_Rect") or {}
        size = [float(rect.get("width", 0)), float(rect.get("height", 0))]
        rd = tree.get("m_RD") or {}
        tex_rect, offset = rd.get("textureRect") or {}, rd.get("textureRectOffset") or {}
        trim = [float(offset.get("x", 0)), float(offset.get("y", 0)),
                float(tex_rect.get("width", size[0])), float(tex_rect.get("height", size[1]))]
        return {"border": [float(border.get(k, 0)) for k in ("x", "y", "z", "w")],
                "ppu": float(tree.get("m_PixelsToUnits", 100.0) or 100.0), "size": size, "trim": trim}

    def tmp_font(self, asset):
        """A TextMeshPro font asset ready to draw text with: unity_tmp.parse_font() plus "images" (PIL "L" image
        of each atlas: the distance field or coverage), "fallback_fonts" (the same for its fallbacks, in order)
        and "font_file" (Asset of the TTF it was made from, when the game ships it). None if it isn't one."""
        reader = getattr(asset, "ref", None)
        if getattr(getattr(reader, "type", None), "name", "") != "MonoBehaviour":
            return None
        with self.lock:
            return self._tmp_font(reader, set())

    def _tmp_font(self, reader, seen):
        from .unity_tmp import parse_font
        key = obj_key(reader.assets_file, reader.path_id)
        if key in self._tmp_fonts:
            return self._tmp_fonts[key]
        if key in seen or len(seen) > 8:
            return None
        seen.add(key)

        def deref(ptr):
            if not isinstance(ptr, dict) or not ptr.get("m_PathID"):
                return None
            target = self.finder._file(reader.assets_file, ptr.get("m_FileID", 0))
            return target.objects.get(ptr["m_PathID"]) if target is not None else None

        try:
            if script_class(reader) not in ("TMPro.TMP_FontAsset", "TMPro.TextMeshProFont"):
                return None
            font = parse_font(self._scripts().read(reader)[0])
        except Exception as e:
            log.debug("TMP font %s: %s", reader.peek_name(), e)
            self._tmp_fonts[key] = None
            return None
        font["images"] = []
        for ptr in font.pop("atlases"):
            obj = deref(ptr)
            try:
                img = asset_image(obj.read()) if obj is not None else None
            except Exception as e:
                log.debug("TMP atlas of %s: %s", font["name"], e)
                img = None
            if img is not None:
                img = img.getchannel("A") if "A" in img.getbands() else img.convert("L")
            font["images"].append(img)
        font["fallback_fonts"] = []
        for ptr in font.pop("fallbacks"):
            obj = deref(ptr)
            fallback = self._tmp_font(obj, seen) if obj is not None and obj.type.name == "MonoBehaviour" else None
            if fallback is not None:
                font["fallback_fonts"].append(fallback)
        source = deref(font.pop("source_font"))
        font["font_file"] = self._asset_for(source, "Font") if source is not None and source.type.name == "Font" else None
        self._tmp_fonts[key] = font
        return font

    def hierarchy(self, asset):
        """GameObject tree of a scene/prefab asset as a flat list (parents before children):
        [{"name", "parent" (index or -1), "active", "pos" (x,y,z), "rot" (x,y,z,w), "scale",
          "mesh" (uid of the Mesh asset or None), "skinned" (bool), "renderer_enabled",
          "batch" (static batching: {"mesh": combined mesh uid, "first", "count", "materials": [Material]}),
          "light" ({"type", "color", "intensity", "range", "spot_angle"}), "terrain" (uid of the terrain model),
          "rect" (UI objects' RectTransform: {"anchor_min", "anchor_max", "pos" (anchored), "size" (size delta),
          "pivot"} as [x, y]),
          "components" ([{"type": Unity class, "props": unity_components.flatten() entries}] for the other
          built-in components: colliders, rigidbodies, audio sources, cameras, LOD groups..., and
          {"type": "MonoBehaviour", "script": "Namespace.Class", "props"} for script components)}],
        in Unity's own space. Optional keys are missing when they don't apply."""
        from .unity_components import SKIP_COMPONENTS, flatten
        from .unity_scene import _components
        nodes = []
        material_cache = {}
        node_keys = {}    # obj key of a GameObject / Transform / component -> (node index, class name)
        pending = []      # (node index, class name, component reader) to flatten once every node is known

        def material_uids(renderer):
            out = []
            for ptr in getattr(renderer, "m_Materials", None) or []:
                try:
                    reader = ptr.deref() if ptr.path_id else None
                except Exception:
                    reader = None
                if reader is None:
                    out.append(None)
                    continue
                uid = f"material:{reader.assets_file.name}:{reader.path_id}"
                self._material_readers[uid] = reader
                out.append(uid)
            return out

        def renderer_materials(renderer):
            out = []
            for ptr in getattr(renderer, "m_Materials", None) or []:
                try:
                    reader = ptr.deref()
                    key = obj_key(reader.assets_file, reader.path_id)
                except Exception:
                    out.append(None)
                    continue
                if key not in material_cache:
                    try:
                        material_cache[key] = make_material(read_material(reader.read()), self._texture_asset)
                    except Exception:
                        material_cache[key] = None
                out.append(material_cache[key])
            return out

        def visit(transform_reader, parent, depth):
            if depth > 200:
                return
            try:
                t = transform_reader.read()
                go_reader = t.m_GameObject.deref()
                go = go_reader.read()
            except Exception:
                return
            index = len(nodes)
            node_keys[obj_key(go_reader.assets_file, go_reader.path_id)] = (index, "GameObject")
            node_keys[obj_key(transform_reader.assets_file, transform_reader.path_id)] = (index, "Transform")
            p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
            node = {"name": getattr(go, "m_Name", "") or "GameObject", "parent": parent,
                    "go": f"go:{go_reader.assets_file.name}:{go_reader.path_id}",
                    "active": bool(getattr(go, "m_IsActive", True)),
                    "layer": int(getattr(go, "m_Layer", 0) or 0),
                    "tag": self.tag_name(int(getattr(go, "m_Tag", 0) or 0)),
                    "pos": [p.x, p.y, p.z], "rot": [r.x, r.y, r.z, r.w], "scale": [s.x, s.y, s.z],
                    "mesh": None, "skinned": False, "renderer_enabled": True}
            if transform_reader.type.name == "RectTransform":
                try:
                    node["rect"] = {key: [float(v.x), float(v.y)] for key, v in (
                        ("anchor_min", t.m_AnchorMin), ("anchor_max", t.m_AnchorMax), ("pos", t.m_AnchoredPosition),
                        ("size", t.m_SizeDelta), ("pivot", t.m_Pivot))}
                except Exception:
                    pass
            comps = _components(go)
            mesh_ptr = None
            batch = None
            for name, reader in comps:
                if name == "MonoBehaviour":
                    name = script_class(reader) or ""  # a script component is known by its class
                    if not name:
                        continue
                    pending.append((index, name, reader))
                elif name not in SKIP_COMPONENTS:
                    pending.append((index, name, reader))
                node_keys.setdefault(obj_key(reader.assets_file, reader.path_id), (index, name))
                try:
                    if name == "SkinnedMeshRenderer":
                        renderer = reader.read()
                        mesh_ptr, node["skinned"] = renderer.m_Mesh, True
                        node["renderer_enabled"] = bool(getattr(renderer, "m_Enabled", True))
                        node["materials"] = material_uids(renderer)
                    elif name == "MeshFilter" and mesh_ptr is None:
                        mesh_ptr = reader.read().m_Mesh
                    elif name == "MeshRenderer":
                        renderer = reader.read()
                        node["renderer_enabled"] = bool(getattr(renderer, "m_Enabled", True))
                        node["materials"] = material_uids(renderer)
                        info = getattr(renderer, "m_StaticBatchInfo", None)
                        if info is not None and getattr(info, "subMeshCount", 0):
                            batch = {"first": int(info.firstSubMesh), "count": int(info.subMeshCount),
                                     "materials": renderer_materials(renderer), "material_uids": node["materials"]}
                    elif name == "Light":
                        light = reader.read()
                        c = light.m_Color
                        node["light"] = {"type": int(getattr(light, "m_Type", 1)), "color": [c.r, c.g, c.b, c.a],
                                         "intensity": float(getattr(light, "m_Intensity", 1.0)),
                                         "range": float(getattr(light, "m_Range", 10.0)),
                                         "spot_angle": float(getattr(light, "m_SpotAngle", 30.0)),
                                         "enabled": bool(getattr(light, "m_Enabled", True))}
                    elif name == "Terrain":
                        data = reader.read().m_TerrainData
                        if data.path_id:
                            obj = data.deref()
                            node["terrain"] = f"terrain:{obj.assets_file.name}:{obj.path_id}"
                except Exception:
                    continue
            if mesh_ptr is not None and getattr(mesh_ptr, "path_id", 0):
                try:
                    node["mesh"] = self._asset_for(mesh_ptr.deref(), "Mesh").uid
                except Exception:
                    pass
            if batch is not None and node["mesh"]:
                # The mesh is a combined (world space) static batch: not this object's own mesh.
                batch["mesh"], node["mesh"] = node["mesh"], None
                node["batch"] = batch
            nodes.append(node)
            for child in t.m_Children or []:
                try:
                    visit(child.deref(), index, depth + 1)
                except Exception:
                    continue

        def resolver(reader):
            return self._ref_resolver(reader, node_keys)

        with self.lock:
            for root in self._scene_roots(asset):
                visit(root, -1, 0)
            for index, name, reader in pending:
                script = reader.type.name == "MonoBehaviour"
                try:
                    tree = self._scripts().read(reader)[0] if script else reader.read_typetree()
                    props = flatten(tree, resolver(reader))
                except Exception as e:
                    log.debug("Component %s of '%s': %s", name, nodes[index]["name"], e)
                    continue
                entry = {"type": "MonoBehaviour", "script": name, "props": props} if script else {"type": name, "props": props}
                seen = self.__dict__.setdefault("_seen_props", set())
                seen.update(e["p"] for e in props if "Array" not in e["p"] and len(seen) < 200_000)
                nodes[index].setdefault("components", []).append(entry)
        return nodes

    def _settings_objects(self):
        return [o for o in self.env.objects if o.type.name in SETTINGS_TYPES + ("BuildSettings", "PlayerSettings")]

    def _ref_resolver(self, reader, node_keys=None):
        """resolve(file id, path id) for unity_components.flatten(): an object of the same prefab/scene
        ({"node", "cls"}), an exported asset ({"asset": uid, "kind"}), a GameObject or component of another
        prefab ({"asset": "go:<file>:<id>" of its GameObject, "kind": "gameobject", "cls"}), or None."""
        from .unity_components import ASSET_KINDS
        node_keys = node_keys or {}

        def resolve(file_id, path_id):
            target_file = self.finder._file(reader.assets_file, file_id)
            if target_file is None:
                return None
            key = obj_key(target_file, path_id)
            found = node_keys.get(key)
            if found is not None:
                return {"node": found[0], "cls": found[1]}
            obj = target_file.objects.get(path_id)
            if obj is None:
                return None
            name = obj.type.name
            kind = ASSET_KINDS.get(name)
            if kind == "material":
                uid = f"material:{obj.assets_file.name}:{obj.path_id}"
                self._material_readers[uid] = obj
                return {"asset": uid, "kind": kind}
            if kind:
                return {"asset": self._asset_for(obj, name).uid, "kind": kind}
            if name == "GameObject":
                return {"asset": f"go:{obj.assets_file.name}:{obj.path_id}", "kind": "gameobject", "cls": "GameObject"}
            if name == "Sprite":
                return {"asset": self._asset_for(obj, "Sprite").uid, "kind": "sprite"}
            if name == "AnimationClip":
                return {"asset": self._asset_for(obj, "AnimationClip").uid, "kind": "file"}
            if name == "AnimatorController":
                return {"asset": f"controller:{obj.assets_file.name}:{obj.path_id}", "kind": "file"}
            listed = self.by_key.get(key)
            if name == "MonoBehaviour" and listed is not None and listed.kind == "data":
                return {"asset": listed.uid, "kind": "data"}
            try:  # a component (Transform, Rigidbody, a script...) of another prefab: its GameObject + type
                go = obj.read_typetree().get("m_GameObject") or {}
                if go.get("m_PathID"):
                    go_file = self.finder._file(obj.assets_file, go.get("m_FileID", 0))
                    if go_file is not None:
                        cls = script_class(obj) if name == "MonoBehaviour" else name
                        return {"asset": f"go:{go_file.name}:{go['m_PathID']}", "kind": "gameobject", "cls": cls}
            except Exception:
                pass
            return None
        return resolve

    def object_paths(self, asset):
        """{"go:<file>:<id>": child path ('' for the root, 'Body/Arm' below it)} of a prefab's GameObjects,
        without reading any components (cheap)."""
        out = {}

        def visit(transform_reader, path, depth):
            if depth > 200:
                return
            try:
                t = transform_reader.read()
                go_reader = t.m_GameObject.deref()
                name = go_reader.peek_name() or "GameObject"
            except Exception:
                return
            here = name if path is None else (f"{path}/{name}" if path else name)
            out[f"go:{go_reader.assets_file.name}:{go_reader.path_id}"] = "" if path is None else here
            for child in t.m_Children or []:
                try:
                    visit(child.deref(), "" if path is None else here, depth + 1)
                except Exception:
                    continue

        with self.lock:
            for root in self._scene_roots(asset):
                visit(root, None, 0)
        return out

    def data_asset(self, asset):
        """(script class, unity_components.flatten() props) of a ScriptableObject data asset, or None."""
        from .unity_components import flatten
        reader = asset.ref
        type_name = getattr(getattr(reader, "type", None), "name", "")
        if type_name in BUILTIN_DATA:
            with self.lock:
                return type_name, flatten(reader.read_typetree(), self._ref_resolver(reader))
        if type_name != "MonoBehaviour":
            return None
        cls = script_class(reader)
        if not cls:
            return None
        with self.lock:
            tree = self._scripts().read(reader)[0]
            return cls, flatten(tree, self._ref_resolver(reader))

    def script_assemblies(self):
        """Names of the assemblies (e.g. 'Assembly-CSharp.dll') the game's MonoScripts come from."""
        names = set()
        with self.lock:
            for obj in self.env.objects:
                if obj.type.name == "MonoScript":
                    try:
                        names.add(obj.read_typetree().get("m_AssemblyName") or "")
                    except Exception:
                        continue
        names.discard("")
        return sorted(names)

    def tag_name(self, tag_id):
        """A GameObject's m_Tag number as the tag's name (custom tags are 20000 + their index)."""
        if tag_id in BUILTIN_TAGS:
            return BUILTIN_TAGS[tag_id]
        if self._tags is None:
            self._tags = []
            for obj in self._settings_objects():
                if obj.type.name == "TagManager":
                    try:
                        self._tags = list(obj.read_typetree().get("tags") or [])
                    except Exception as e:
                        log.debug("TagManager: %s", e)
        i = tag_id - 20000
        return self._tags[i] if 0 <= i < len(self._tags) else ""

    def project_settings(self):
        """{"managers": [{"type", "props" (unity_components.flatten)}], "tags", "scene_order": [original scene
        paths in build order], "product", "company"} from globalgamemanagers."""
        from .unity_components import flatten
        out = {"managers": [], "tags": [], "scene_order": [], "product": "", "company": ""}
        with self.lock:
            for obj in self._settings_objects():
                try:
                    tree = obj.read_typetree()
                except Exception as e:
                    log.debug("Settings %s: %s", obj.type.name, e)
                    continue
                name = obj.type.name
                if name == "BuildSettings":
                    out["scene_order"] = list(tree.get("scenes") or tree.get("m_Scenes") or [])
                elif name == "PlayerSettings":
                    out["product"], out["company"] = tree.get("productName", ""), tree.get("companyName", "")
                else:
                    if name == "TagManager":
                        out["tags"] = list(tree.get("tags") or [])
                    out["managers"].append({"type": name, "props": flatten(tree, lambda file_id, path_id: None)})
        return out

    def material_details(self, uid):
        """read_material_details() of a material named by hierarchy() ("material:<file>:<id>"), with texture
        Assets instead of readers; None if unknown."""
        reader = self._material_readers.get(uid)
        if reader is None:
            return None
        with self.lock:
            d = read_material_details(reader.read())
        d["textures"] = [(prop, self._asset_for(tex, "Texture2D"), scale, offset)
                         for prop, tex, scale, offset in d["textures"]]
        return d

    def _scene(self, asset):
        """(MeshData, materials, info rows) of a scene/prefab, cached (the last few, per LOD level)."""
        from .sdk import view_level
        from .unity_scene import build
        key = (asset.key, view_level("lod_level"))
        if key not in self._scenes:
            with self.lock:
                result = build(self, self._scene_roots(asset), asset.name.split(": ", 1)[-1])
            self._scenes[key] = result
            while len(self._scenes) > 4:
                self._scenes.pop(next(iter(self._scenes)))
        return self._scenes[key]

    def raw(self, asset):
        if asset.kind == "font":
            with self.lock:
                return bytes(asset.ref.read().m_FontData)
        if asset.kind == "video":
            return self.video(asset)[0]
        if asset.kind == "data":
            import json
            tree, _note = self._data(asset)
            return json.dumps(tree, indent=2, ensure_ascii=False).encode("utf-8")
        if asset.kind == "controller":
            import json
            return json.dumps(self.controller(asset), indent=2, ensure_ascii=False,
                              default=lambda a: getattr(a, "name", str(a))).encode("utf-8")
        if asset.kind == "animation":
            import json
            return json.dumps(self._clip(asset), indent=1).encode("utf-8")
        if asset.kind == "text":
            script = asset.ref.read().m_Script
            return script.encode("utf-8", "surrogateescape") if isinstance(script, str) else bytes(script)
        if hasattr(asset.ref, "get_raw_data"):
            # Anything else: the object as the game stores it (big pixel/sound data may sit in a .resS file).
            with self.lock:
                return bytes(asset.ref.get_raw_data())
        raise ValueError("This asset is built from several objects, so there are no single raw bytes to show.")

    def content_hash(self, asset):
        """Hash of the object's fields without its name, with streamed pixel / sound / vertex data hashed in
        place of where it sits in the .resS file - so copies of one asset in different bundles match."""
        import hashlib
        reader = asset.ref
        if not hasattr(reader, "read_typetree"):
            return None
        from UnityPy.helpers.ResourceReader import get_resource_data
        h = hashlib.blake2b(digest_size=16)
        h.update(reader.type.name.encode())
        with self.lock:
            try:
                tree = reader.read_typetree()
            except Exception:  # e.g. script data without a stored field layout: the stored bytes will do
                h.update(bytes(reader.get_raw_data()))
                return h.digest()

            def external(source, offset, size):
                if not size or not source:
                    return False
                try:
                    h.update(get_resource_data(source, reader.assets_file, offset, size))
                    return True
                except Exception:
                    return False

            def feed(value):
                if isinstance(value, dict):
                    if {"offset", "size", "path"} <= set(value) and external(value["path"], value["offset"], value["size"]):
                        return  # m_StreamData: the data, not where it is
                    if {"m_Source", "m_Offset", "m_Size"} <= set(value) and external(
                            value["m_Source"], value["m_Offset"], value["m_Size"]):
                        return  # m_Resource / m_ExternalResources (sound, video)
                    for key, item in value.items():
                        if key in ("m_Name", "m_CorrespondingSourceObject", "m_PrefabInstance", "m_PrefabAsset"):
                            continue
                        h.update(key.encode())
                        feed(item)
                elif isinstance(value, (list, tuple)):
                    h.update(b"[%d" % len(value))
                    for item in value:
                        feed(item)
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    h.update(bytes(value))
                elif isinstance(value, str):
                    h.update(value.encode("utf-8", "surrogateescape"))
                else:
                    h.update(repr(value).encode())

            feed(tree)
        return h.digest()

    def text(self, asset):
        if asset.kind == "data":
            import json
            tree, note = self._data(asset)
            with self.lock:
                tree = self._label_refs(asset.ref, tree)
            header = f"{asset.name}   ({script_class(asset.ref) or 'script'})\n"
            if note:
                header += f"\nNote: {note}\n"
            return header + "\n" + json.dumps(tree, indent=2, ensure_ascii=False)
        if asset.kind == "animation":
            from .unity_anim import summary_text
            return summary_text(self._clip(asset))
        script = asset.ref.read().m_Script
        return script.decode("utf-8", "replace") if isinstance(script, bytes) else script

    def _label_refs(self, obj, tree):
        """Copy of a data tree where object references also say what they point to."""
        names = {}

        def label(file_id, path_id):
            key = (file_id, path_id)
            if key not in names:
                names[key] = None
                try:
                    target = self.finder._file(obj.assets_file, file_id)
                    reader = target.objects.get(path_id) if target is not None else None
                    if reader is not None:
                        name = reader.peek_name() if reader.type.name != "MonoBehaviour" else (
                            reader.peek_name() or script_class(reader))
                        names[key] = f"{reader.type.name} '{name}'" if name else reader.type.name
                except Exception:
                    pass
            return names[key]

        def walk(value, depth=0):
            if depth > 60:
                return value
            if isinstance(value, dict):
                if set(value) == {"m_FileID", "m_PathID"} and value["m_PathID"]:
                    target = label(value["m_FileID"], value["m_PathID"])
                    return {**value, "points_to": target} if target else value
                return {k: walk(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, depth + 1) for v in value]
            return value
        return walk(tree)

    def _path_names(self):
        """CRC32 path hash -> transform path, from every Avatar's table (animations name bones by hash)."""
        if self._paths is None:
            self._paths = {}
            for obj in self.env.objects:
                if obj.type.name != "Avatar":
                    continue
                try:
                    tos = obj.read_typetree().get("m_TOS") or []
                except Exception:
                    continue
                for entry in tos:
                    if isinstance(entry, (list, tuple)) and len(entry) == 2:
                        self._paths[int(entry[0]) & 0xFFFFFFFF] = entry[1]
        return self._paths

    def transforms(self):
        """Every Transform: key -> {"name", "parent" (key or None), "pos", "rot" (x,y,z,w), "scale"}.

        Read once (can take a while in big games). Used to name animated bones and to pose skeletons.
        """
        if self._transforms is None:
            started = time.time()
            nodes = {}
            with self.lock:
                for obj in self.env.objects:
                    if obj.type.name not in ("Transform", "RectTransform"):
                        continue
                    try:
                        t = obj.read()
                        go = t.m_GameObject.deref()
                        father = t.m_Father
                        parent = None
                        if father.path_id:
                            f = father.deref()
                            parent = obj_key(f.assets_file, f.path_id)
                        p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
                        nodes[obj_key(obj.assets_file, obj.path_id)] = {
                            "name": go.peek_name() or "", "parent": parent,
                            "go": obj_key(go.assets_file, go.path_id),
                            "pos": (p.x, p.y, p.z), "rot": (r.x, r.y, r.z, r.w), "scale": (s.x, s.y, s.z)}
                    except Exception:
                        continue
            self._transforms = nodes
            log.info("Read %d transforms in %.1fs", len(nodes), time.time() - started)
        return self._transforms

    def _hierarchy_paths(self):
        """CRC32 -> path for every transform path relative to each of its ancestors (as animations use them)."""
        from .unity_anim import path_hash
        out = {}
        nodes = self.transforms()
        for key in nodes:
            chain, k = [], key
            while k is not None and k in nodes and len(chain) < 64:
                chain.append(nodes[k]["name"])
                k = nodes[k]["parent"]
            chain.reverse()  # root ... this transform
            for start in range(1, len(chain)):
                path = "/".join(chain[start:])
                out.setdefault(path_hash(path), path)
        return out

    def _skin_info(self, smr_reader):
        """(bone transform keys, mesh transform key) of a SkinnedMeshRenderer (cached)."""
        key = obj_key(smr_reader.assets_file, smr_reader.path_id)
        if key not in self._skins:
            smr = smr_reader.read()
            bones = []
            for ptr in smr.m_Bones:
                try:
                    b = ptr.deref()
                    bones.append(obj_key(b.assets_file, b.path_id))
                except Exception:
                    bones.append(None)
            go = smr.m_GameObject.deref()
            go_key = obj_key(go.assets_file, go.path_id)
            mesh_node = next((k for k, n in self.transforms().items() if n.get("go") == go_key), None)
            self._skins[key] = (bones, mesh_node)
        return self._skins[key]

    def _bone_suffixes(self, bone_keys):
        nodes = self.transforms()
        out = set()
        for k in bone_keys:
            chain, j = [], k
            while j is not None and j in nodes and len(chain) < 64:
                chain.append(nodes[j]["name"])
                j = nodes[j]["parent"]
            chain.reverse()
            for i in range(len(chain)):
                out.add("/".join(chain[i:]))
        return out

    # ---- humanoid (muscle) clips
    MIN_HUMAN_BONES = 10  # skeleton bones an Avatar must share with a model to drive it

    def _human_rigs(self):
        """Every humanoid Avatar in the game (read once)."""
        if self._rigs is None:
            from .unity_humanoid import HumanRig
            rigs = []
            with self.lock:
                for obj in self.env.objects:
                    if obj.type.name != "Avatar":
                        continue
                    try:
                        rig = HumanRig(obj.read_typetree())
                    except Exception:
                        continue
                    if rig.valid:
                        rig.source_path = getattr(obj, "container", None) or ""
                        rigs.append(rig)
            self._rigs = rigs
            log.info("%d humanoid Avatar(s)", len(rigs))
        return self._rigs

    def _rig_for(self, mesh_key, bone_keys):
        """The humanoid Avatar whose skeleton fits these bones best (cached per model), or None."""
        if mesh_key not in self._model_rigs:
            suffixes = self._bone_suffixes([b for b in bone_keys if b is not None])
            model = self.by_key.get(mesh_key)
            best, best_score = None, (0,)
            for rig in self._human_rigs():
                shared = rig.human_paths & suffixes
                # Most bones in common; then the Avatar from the model's own file; then the fullest
                # paths (a rig whose paths are just 'Hips/Spine' fits every skeleton equally).
                score = (len(shared), _path_closeness(getattr(rig, "source_path", ""), model.path if model else ""),
                         sum(p.count("/") for p in shared))
                if score > best_score:
                    best, best_score = rig, score
            self._model_rigs[mesh_key] = best if best_score[0] >= self.MIN_HUMAN_BONES else None
        return self._model_rigs[mesh_key]

    def _skinned_models(self):
        """[(model Asset, bone keys)] of every skinned mesh."""
        self.finder._run_until(lambda: self.finder.indexed)
        with self.lock:
            skinned = dict(self.finder._skinned)
        out = []
        for mesh_key, smrs in skinned.items():
            model = self.by_key.get(mesh_key)
            if model is None:
                continue
            try:
                with self.lock:
                    bones, _node = self._skin_info(smrs[0])
            except Exception:
                continue
            out.append((model, bones))
        return out

    def animation_targets(self, asset):
        from .unity_humanoid import is_humanoid
        clip = self._clip(asset)
        if is_humanoid(clip):
            # Humanoid clips play on any humanoid character: list the models that have an Avatar.
            # The clip's own character first (same folder), then the most complete skeletons.
            models = [(m, len(bones)) for m, bones in self._skinned_models() if self._rig_for(m.key, bones) is not None]
            models.sort(key=lambda mb: (-_path_closeness(asset.path, mb[0].path), -mb[1], mb[0].name.lower()))
            return [m for m, _n in models[:200]]
        wanted = {c["path"] for c in clip["curves"]
                  if c["property"] in ("position", "rotation", "euler", "scale")}
        if not wanted:
            return []
        scored = []
        for model, bones in self._skinned_models():
            score = len(wanted & self._bone_suffixes([b for b in bones if b is not None]))
            if score:
                scored.append((score, _path_closeness(asset.path, model.path), model))
        # Creatures often share bone names: among equal matches, models from the clip's own file/folder first.
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].name.lower()))
        return [m for _s, _c, m in scored[:50]]

    def _skin_setup(self, model):
        """(MeshHandler, bone transform keys, mesh transform key, bind poses (B, 4, 4)) of a skinned model."""
        from UnityPy.helpers.MeshHelper import MeshHandler
        self.finder._run_until(lambda: self.finder.indexed)
        smrs = self.finder._skinned.get(model.key)
        if not smrs:
            raise ValueError("This model isn't skinned (no SkinnedMeshRenderer uses it).")
        with self.lock:
            bones, mesh_node = self._skin_info(smrs[0])
            mesh = model.ref.read()
            handler = MeshHandler(mesh)
            handler.process()
        if not handler.m_BoneWeights or not handler.m_BoneIndices:
            raise ValueError("This model has no bone weights.")
        bind_poses = bind_matrices(getattr(handler, "m_BindPose", None) or mesh.m_BindPose)
        n = len(bones)
        if len(bind_poses) < n:
            bind_poses = np.concatenate([bind_poses, np.tile(np.eye(4), (n - len(bind_poses), 1, 1))])
        return handler, bones, mesh_node, bind_poses[:n]

    def lod_siblings(self, asset):
        """The meshes of each level of the LODGroup this model's renderer belongs to (LOD0 first), or []."""
        if asset.kind != "model" or self._is_terrain(asset) or not self.finder.indexed:
            return []
        f = self.finder
        with self.lock:
            if self._lod_index is None:
                self._lod_index = {}  # renderer key -> (LODGroup reader, level, position in the level)
                for obj in self.env.objects:
                    if obj.type.name != "LODGroup":
                        continue
                    try:
                        for level, lod in enumerate(obj.read().m_LODs):
                            for pos, lr in enumerate(lod.renderers):
                                if lr.renderer.path_id:
                                    r = lr.renderer.deref()
                                    self._lod_index.setdefault(obj_key(r.assets_file, r.path_id), (obj, level, pos))
                    except Exception:
                        continue
            key = obj_key(asset.ref.assets_file, asset.ref.path_id)
            renderers = [f._renderers.get(obj_key(af, go)) for af, go in f._mesh_users.get(key, [])]
            renderers += f._skinned.get(key, [])
            group = next((self._lod_index[k] for k in (obj_key(r.assets_file, r.path_id) for r in renderers if r)
                          if k in self._lod_index), None)
            if group is None:
                return []
            out = []
            # A group often has several parts per level (base, barrel...): the same part in each level, by its name
            # without the LOD suffix, else by its position in the level.
            stem = _lod_stem(asset.name)
            pos = group[2]
            for lod in group[0].read().m_LODs:
                parts = list(lod.renderers)
                meshes = [self._renderer_mesh(lr.renderer) for lr in parts]
                same = [m for m in meshes if m is not None and _lod_stem(m.name) == stem]
                at = [meshes[pos]] if pos < len(meshes) and meshes[pos] is not None else []
                out.append(next(iter(same + at + [m for m in meshes if m is not None]), None))
        return out if sum(1 for m in out if m is not None) > 1 else []

    def _renderer_mesh(self, ptr):
        """Mesh Asset a renderer PPtr draws (its MeshFilter's, or a SkinnedMeshRenderer's own), or None."""
        try:
            reader = ptr.deref()
            if reader.type.name == "SkinnedMeshRenderer":
                mesh = reader.read().m_Mesh
            else:
                from .unity_scene import _components
                go = reader.read().m_GameObject.deref().read()
                mf = next((r for n, r in _components(go) if n == "MeshFilter"), None)
                mesh = mf.read().m_Mesh if mf is not None else None
            if mesh is None or not mesh.path_id:
                return None
            m = mesh.deref()
            return self.by_key.get(obj_key(m.assets_file, m.path_id))
        except Exception:
            return None

    def bones(self, asset):
        """Skeleton of a skinned mesh in its bind pose (where the shown mesh's bones are), or None.
        Reads only the bone transforms (not every transform in the game), so it's quick."""
        if asset.kind != "model" or self._is_terrain(asset) or not self.finder.indexed:
            return None
        smrs = self.finder._skinned.get(asset.key)
        if not smrs:
            return None
        with self.lock:
            readers = []
            for ptr in smrs[0].read().m_Bones or []:
                try:
                    readers.append(ptr.deref())
                except Exception:
                    readers.append(None)
            nodes = {}  # just the bones and their ancestors, like transforms() has them
            for reader in readers:
                hops = 0
                while reader is not None and obj_key(reader.assets_file, reader.path_id) not in nodes and hops < 64:
                    try:
                        t = reader.read()
                        father = t.m_Father.deref() if t.m_Father.path_id else None
                        name = t.m_GameObject.deref().peek_name() or "?"
                    except Exception:
                        break
                    nodes[obj_key(reader.assets_file, reader.path_id)] = {
                        "name": name, "parent": obj_key(father.assets_file, father.path_id) if father else None}
                    reader, hops = father, hops + 1
            keys = [obj_key(r.assets_file, r.path_id) if r is not None else None for r in readers]
            bind = bind_matrices(asset.ref.read().m_BindPose)
        return bones_from_bind(keys, bind, nodes)

    def animate(self, model, clip_asset):
        from .unity_skin import Animator
        handler, bones, mesh_node, bind_poses = self._skin_setup(model)
        animator = Animator(handler.m_Vertices, handler.m_BoneIndices, handler.m_BoneWeights, bind_poses,
                            bones, self.transforms(), mesh_node, self.clip_for(model, clip_asset, bones))
        if not animator.matched:
            raise ValueError("None of this animation's bones are in this model's skeleton.")
        return animator

    def skeleton(self, model, clip=None):
        from .unity_skin import export_animation, export_rig
        handler, bones, mesh_node, bind_poses = self._skin_setup(model)
        animator = self.animate(model, clip) if clip is not None else None
        rig, joint_of = export_rig(self.transforms(), bones, bind_poses, handler.m_BoneIndices,
                                   handler.m_BoneWeights, mesh_node, animator.root_node if animator else None)
        if len(rig["joints_0"]) != handler.m_VertexCount:
            raise ValueError("The bone weights don't match the mesh's vertices.")
        return rig, (export_animation(animator, joint_of, clip.name) if animator else None)

    def clip_for(self, model, clip_asset, bones=None):
        """Decoded clip with bone curves for `model`: humanoid clips are converted for its Avatar."""
        from .unity_humanoid import is_humanoid, place_hips, to_generic
        clip = self._clip(clip_asset)
        if not is_humanoid(clip):
            return clip
        if bones is None:
            smrs = self.finder._skinned.get(model.key)
            if not smrs:
                raise ValueError("This model isn't skinned (no SkinnedMeshRenderer uses it).")
            with self.lock:
                bones, _node = self._skin_info(smrs[0])
        rig = self._rig_for(model.key, bones)
        if rig is None:
            raise ValueError("This is a humanoid animation, and this model has no humanoid Avatar to play it with.")
        key = (clip_asset.key, id(rig))
        if key not in self._humanoid:
            self._humanoid[key] = to_generic(clip, rig)
            while len(self._humanoid) > 8:
                self._humanoid.pop(next(iter(self._humanoid)))
        clip = self._humanoid[key]
        parent = self._hips_parent(bones, clip["hips"]["path"])
        return place_hips(clip, parent) if parent is not None else clip

    def _hips_parent(self, bone_keys, hips_path):
        """Matrix of the model's hips parent relative to the animated root (the object `hips_path`,
        e.g. 'Armature/Hips', is relative to), from the model's own transforms; None if not found."""
        from .unity_skin import trs
        nodes = self.transforms()
        parts = hips_path.split("/")
        for key in bone_keys:
            chain, k = [], key
            while k is not None and k in nodes and len(chain) < len(parts):
                chain.append(k)
                k = nodes[k]["parent"]
            if len(chain) < len(parts) or [nodes[c]["name"] for c in reversed(chain)] != parts:
                continue
            m = np.eye(4)
            for c in reversed(chain[1:]):  # root's child ... the hips' parent
                n = nodes[c]
                m = m @ trs(n["pos"], n["rot"], n["scale"])
            return m
        return None

    # ---- sprites: sheets and flipbook animations
    def _sprite_sheets(self):
        """texture key -> [(sprite Asset, (x, y, w, h))] (y from the bottom), built on first use."""
        if self._sheets is None:
            sheets = {}
            started = time.time()
            with self.lock:
                for a in self.assets:
                    if a.kind != "sprite":
                        continue
                    try:
                        tree = a.ref.read_typetree()
                        rd = tree["m_RD"]
                        tex = rd["texture"]
                        if not tex.get("m_PathID"):
                            continue
                        target = self.finder._file(a.ref.assets_file, tex.get("m_FileID", 0))
                        if target is None:
                            continue
                        r = rd.get("textureRect") or tree["m_Rect"]
                        sheets.setdefault(obj_key(target, tex["m_PathID"]), []).append(
                            (a, (r["x"], r["y"], r["width"], r["height"])))
                    except Exception:
                        continue
            self._sheets = sheets
            log.info("Indexed which textures %d sprites come from in %.1fs",
                     sum(len(v) for v in sheets.values()), time.time() - started)
        return self._sheets

    # Property names animation curves can target, besides Transform ones; clips store a CRC32 of the name.
    COMMON_PROPERTIES = ["m_IsActive", "m_Enabled", "m_Color.r", "m_Color.g", "m_Color.b", "m_Color.a",
                         "m_Intensity", "m_Range", "m_SpotAngle", "m_Volume", "m_Pitch", "m_FieldOfView",
                         "m_SortingOrder", "m_FlipX", "m_FlipY", "m_Size.x", "m_Size.y", "m_Center.x", "m_Center.y",
                         "m_Center.z", "m_Radius", "m_Height", "m_AnchoredPosition.x", "m_AnchoredPosition.y",
                         "m_SizeDelta.x", "m_SizeDelta.y", "m_LocalPosition.x", "m_LocalPosition.y", "m_LocalPosition.z",
                         "m_Alpha", "m_fontSize", "m_fontColor.r", "m_fontColor.g", "m_fontColor.b", "m_fontColor.a",
                         "m_BlendShapeWeights.Array.data[0]"]

    def _property_names(self):
        """CRC32 -> property path, for the curves that name their property by hash."""
        import zlib
        if not hasattr(self, "_prop_hash") or self._prop_hash is None:
            self._prop_hash = {}
        names = set(self.COMMON_PROPERTIES) | getattr(self, "_seen_props", set())
        for uid, reader in list(self._material_readers.items()):
            try:
                d = read_material_details(reader.read())
            except Exception:
                continue
            for prop in d["floats"]:
                names.add(f"material.{prop}")
            for prop in d["colors"]:
                names.update(f"material.{prop}.{c}" for c in "rgba")
        for name in names:
            self._prop_hash.setdefault(zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF, name)
        return self._prop_hash

    def clip_export(self, asset):
        """For exporting an AnimationClip: {"name", "length", "sample_rate", "loop", "legacy", "events",
        "curves": [{"path", "type", "prop", "keys": [(t, v)]}], "object_curves": [{"path", "type", "prop",
        "keys": [(t, sprite uid)]}], "skipped"}, with Unity editor binding names."""
        from UnityPy.enums import ClassIDType
        info = self._clip(asset)
        hashes = self._property_names()
        transform = {"position": "m_LocalPosition", "rotation": "m_LocalRotation", "scale": "m_LocalScale",
                     "euler": "localEulerAnglesRaw"}
        curves, objects, skipped = [], [], 0
        mapping = None
        for c in info["curves"]:
            path, prop, class_id = c["path"], c["property"], c.get("class_id")
            if path.startswith("#"):
                skipped += 1
                continue
            if c.get("object_curve"):
                if mapping is None:
                    with self.lock:
                        mapping = list(asset.ref.read().m_ClipBindingConstant.pptrCurveMapping or [])
                keys = []
                for t, v in c["keys"]:
                    try:
                        sprite = mapping[int(v)].deref()
                        keys.append((t, self._asset_for(sprite, "Sprite").uid))
                    except Exception:
                        continue
                if keys:
                    objects.append({"path": path, "type": "SpriteRenderer", "prop": "m_Sprite", "keys": keys})
                continue
            if prop in transform and class_id in (4, None):
                curves.append({"path": path, "type": "Transform", "keys": c["keys"],
                               "prop": f"{transform[prop]}.{c['component']}" if c["component"] else transform[prop]})
                continue
            if class_id == 95:  # humanoid muscle / body curve on the Animator
                curves.append({"path": path, "type": "Animator", "prop": prop, "keys": c["keys"]})
                continue
            name = prop if class_id is None else hashes.get(c.get("attribute"))
            if not name:
                skipped += 1
                continue
            if class_id == 114:
                type_name = ""
                if c.get("script"):
                    try:
                        target = self.finder._file(asset.ref.assets_file, c["script"][0])
                        script = target.objects[c["script"][1]].read_typetree()
                        ns, cls = script.get("m_Namespace", ""), script.get("m_ClassName", "")
                        type_name = f"{ns}.{cls}" if ns else cls
                    except Exception:
                        type_name = ""
                if not type_name:
                    skipped += 1
                    continue
            else:
                try:
                    type_name = ClassIDType(class_id).name if class_id is not None else "GameObject"
                except ValueError:
                    skipped += 1
                    continue
            curves.append({"path": path, "type": type_name, "prop": name, "keys": c["keys"],
                           "script": class_id == 114})
        return {"name": info["name"], "length": info["length"], "sample_rate": info["sample_rate"] or 30,
                "loop": info.get("loop", False), "legacy": info["legacy"], "events": info["events"],
                "curves": curves, "object_curves": objects, "skipped": skipped}

    def controllers(self):
        """[(uid "controller:<file>:<id>", name, source file)] of the game's AnimatorControllers."""
        out = []
        with self.lock:
            for obj in self.env.objects:
                if obj.type.name == "AnimatorController":
                    uid = f"controller:{obj.assets_file.name}:{obj.path_id}"
                    self._controller_readers[uid] = obj
                    out.append((uid, obj.peek_name() or "Controller", obj.assets_file.name))
        return out

    def controller(self, asset):
        """An AnimatorController asset decoded (unity_controller.decode_controller), clips as their Assets."""
        from .unity_controller import decode_controller
        reader = asset.ref
        with self.lock:
            tree = reader.read_typetree()
            clips = []
            for ptr in tree.get("m_AnimationClips") or []:
                try:
                    target = self.finder._file(reader.assets_file, ptr.get("m_FileID", 0))
                    obj = target.objects.get(ptr.get("m_PathID")) if target is not None and ptr.get("m_PathID") else None
                    clips.append(self._asset_for(obj, "AnimationClip") if obj is not None else None)
                except Exception:
                    clips.append(None)
        return decode_controller(tree, lambda i: clips[i] if 0 <= i < len(clips) else None)

    def controller_export(self, uid):
        """unity_controller.decode_controller() of a controller from controllers(), clips as their asset uids."""
        from .unity_controller import decode_controller
        reader = self._controller_readers[uid]
        with self.lock:
            tree = reader.read_typetree()
            clips = []
            for ptr in tree.get("m_AnimationClips") or []:
                try:
                    target = self.finder._file(reader.assets_file, ptr.get("m_FileID", 0))
                    obj = target.objects.get(ptr.get("m_PathID")) if target is not None and ptr.get("m_PathID") else None
                    clips.append(self._asset_for(obj, "AnimationClip").uid if obj is not None else None)
                except Exception:
                    clips.append(None)
        return decode_controller(tree, lambda i: clips[i] if 0 <= i < len(clips) else None)

    def sprite_sheets(self):
        """For exporting: [{"texture": texture Asset, "sprites": [{"uid", "name", "rect" (x, y, w, h from the
        bottom), "pivot" (0..1), "border" (left, bottom, right, top), "ppu"}]}] - where each sprite sits in the
        texture it's drawn from (its own texture, or the atlas it was packed into)."""
        sheets = {}
        with self.lock:
            for a in self.assets:
                if a.kind != "sprite":
                    continue
                try:
                    tree = a.ref.read_typetree()
                    rd = tree["m_RD"]
                    tex = rd["texture"]
                    if not tex.get("m_PathID"):
                        continue
                    target = self.finder._file(a.ref.assets_file, tex.get("m_FileID", 0))
                    texture = self.by_key.get(obj_key(target, tex["m_PathID"])) if target is not None else None
                    if texture is None:
                        continue
                    packed = int(rd.get("settingsRaw", 0)) & 1
                    r = rd.get("textureRect") if packed and rd.get("textureRect") else tree["m_Rect"]
                    pivot = tree.get("m_Pivot") or {"x": 0.5, "y": 0.5}
                    border = tree.get("m_Border") or {}
                    sheets.setdefault(texture.key, {"texture": texture, "sprites": []})["sprites"].append({
                        "uid": a.uid, "name": a.name or "sprite",
                        "rect": [float(r["x"]), float(r["y"]), float(r["width"]), float(r["height"])],
                        "pivot": [float(pivot["x"]), float(pivot["y"])],
                        "border": [float(border.get(k, 0)) for k in ("x", "y", "z", "w")],
                        "ppu": float(tree.get("m_PixelsToUnits", 100.0))})
                except Exception as e:
                    log.debug("Sprite '%s': %s", a.name, e)
        return list(sheets.values())

    def sprite_rects(self, asset):
        if asset.kind != "texture":
            return []
        return [rect for _a, rect in self._sprite_sheets().get(asset.key, [])]

    def sprite_frames(self, asset):
        if asset.kind != "animation":
            return [], 0.0
        clip = self._clip(asset)
        length = clip.get("length") or 0.0
        with self.lock:
            obj = asset.ref.read()
            curves = [c for c in clip["curves"] if c.get("object_curve")]
            if curves:
                mapping = list(obj.m_ClipBindingConstant.pptrCurveMapping or [])
                for curve in sorted(curves, key=lambda c: c.get("class_id") != 212):
                    frames = []
                    for t, v in curve["keys"]:
                        i = int(round(v))
                        if not 0 <= i < len(mapping) or not mapping[i].path_id:
                            continue
                        try:
                            reader = mapping[i].deref()
                        except Exception:
                            continue
                        if reader.type.name in ("Sprite", "Texture2D"):
                            frames.append((t, self._asset_for(reader, reader.type.name)))
                    if frames:
                        return frames, max(length, frames[-1][0])
            for pc in getattr(obj, "m_PPtrCurves", None) or []:  # legacy clips
                if getattr(pc, "attribute", "") != "m_Sprite":
                    continue
                frames = []
                for key in pc.curve:
                    try:
                        reader = key.value.deref()
                        frames.append((key.time, self._asset_for(reader, reader.type.name)))
                    except Exception:
                        continue
                if frames:
                    return frames, max(length, frames[-1][0])
        return [], 0.0

    def _clip(self, asset):
        from .unity_anim import decode_clip
        with self.lock:
            tree = asset.ref.read_typetree()
        names = self._path_names()
        hashes = [b.get("path", 0) for b in (tree.get("m_ClipBindingConstant") or {}).get("genericBindings") or []]
        if any(h and h not in names for h in hashes) and not self._hierarchy_done:
            # Not in any Avatar's table: name them from the scene/prefab transform hierarchy.
            self._hierarchy_done = True
            for h, path in self._hierarchy_paths().items():
                names.setdefault(h, path)
        return decode_clip(tree, names)

    def _scripts(self):
        if self._script_reader is None:
            game_dir = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            if not os.path.isfile(os.path.join(game_dir, "GameAssembly.dll")) and not any(
                    n.endswith("_Data") for n in os.listdir(game_dir)):
                game_dir = os.path.dirname(game_dir)  # opened the _Data folder itself
            self._script_reader = ScriptReader(self.env, game_dir)
        return self._script_reader

    def _data(self, asset):
        with self.lock:
            if asset.ref.type.name in BUILTIN_DATA:
                tree, note = asset.ref.read_typetree(), ""
            else:
                tree, note = self._scripts().read(asset.ref)
        return json_safe(tree), note

    def video(self, asset):
        from UnityPy.helpers.ResourceReader import get_resource_data
        with self.lock:
            tree = asset.ref.read_typetree()
            res = tree.get("m_ExternalResources") or {}
            if not res.get("m_Size"):
                raise ValueError("This VideoClip has no video data in the game files.")
            data = get_resource_data(res["m_Source"], asset.ref.assets_file, res["m_Offset"], res["m_Size"])
        return bytes(data), asset.ext or "mp4"

    def audio(self, asset):
        clip = asset.ref.read()
        try:
            samples = clip.samples  # UnityPy converts through FMOD to WAV
        except OSError as e:
            # the clip names no resource file (e.g. a video's audio track): UnityPy opens the game folder itself
            raise ValueError("This AudioClip's sound data isn't in the game files.") from e
        if not samples:
            raise ValueError("This AudioClip has no sound data in the game files.")
        _name, data = next(iter(samples.items()))
        return bytes(data), "wav"

    def stats(self, asset):
        if self._is_terrain(asset):
            obj = asset.ref["obj"]
            if asset.ref["type"] == "terrain_tex":
                return {"size": 0, "info": "blended", "sort": 0}
            res = int(asset.ref.get("resolution") or 0)
            return {"size": obj.byte_size, "info": f"terrain {res}\u00d7{res}" if res else "terrain",
                    "sort": res * res * 2}
        if asset.kind == "scene":
            n = len(asset.ref.get("transforms", [])) if asset.ref["type"] == "scene" else 0
            return {"size": asset.size, "info": f"{n:,} objects" if n else "prefab", "sort": n}
        obj = asset.ref
        stats = {"size": obj.byte_size}
        if asset.kind == "data":
            with self.lock:
                cls = script_class(obj)
            return {"size": obj.byte_size, "info": cls.rsplit(".", 1)[-1], "sort": obj.byte_size}
        if asset.kind == "controller":
            try:
                c = self.controller(asset)
                n = sum(len(layer["states"]) for layer in c["layers"])
                return {"size": obj.byte_size, "info": f"{n} states, {len(c['parameters'])} parameters", "sort": n}
            except Exception:
                return {"size": obj.byte_size, "info": "controller", "sort": 0}
        if asset.kind in ("font", "video"):
            return {"size": obj.byte_size, "info": asset.ext.upper(), "sort": obj.byte_size}
        if asset.kind == "animation":
            tree = obj.read_typetree()
            muscle = tree.get("m_MuscleClip") or {}
            length = float(muscle.get("m_StopTime", 0) or 0) - float(muscle.get("m_StartTime", 0) or 0)
            stats.update(info=f"{length:.2f} s", sort=length)
            return stats
        if asset.kind == "audio":
            clip = obj.read()
            length = float(getattr(clip, "m_Length", 0) or 0)
            size = getattr(getattr(clip, "m_Resource", None), "m_Size", 0) or 0
            stats.update(size=obj.byte_size + size, info=f"{int(length // 60)}:{length % 60:04.1f}", sort=length)
            return stats
        if asset.kind == "text":
            stats.update(info="", sort=obj.byte_size)
            return stats
        data = obj.read()
        if asset.kind == "model":
            tris = triangle_count(data.m_SubMeshes)
            vertex_data = getattr(data, "m_VertexData", None)
            verts = vertex_data.m_VertexCount if vertex_data is not None else 0
            stats.update(tris=tris, verts=verts, info=f"{tris:,} tris", sort=tris)
        elif asset.kind == "texture":
            w, h = data.m_Width, data.m_Height
            stream = getattr(data, "m_StreamData", None)
            if stream is not None and stream.size:
                stats["size"] += stream.size  # pixel data lives in the .resS file
            stats.update(w=w, h=h, info=f"{w}×{h}", sort=w * h)
        elif asset.kind == "sprite":
            w, h = int(data.m_Rect.width), int(data.m_Rect.height)
            stats.update(w=w, h=h, info=f"{w}×{h}", sort=w * h)
        return stats

    def materials_ready(self):
        return self.finder.indexed

    def _info(self, asset):
        return self.finder.info(asset.ref)

    def materials(self, asset):
        if self._is_terrain(asset):
            return [self.terrain(asset.ref["obj"])[1]]
        if asset.kind == "scene":
            return list(self._scene(asset)[1])
        _names, mats, _n = self._info(asset)
        if not mats and asset.kind == "model":
            try:
                mats = self._data_materials(asset)
            except Exception:
                log.exception("Looking for the materials of '%s' in game data failed", asset.name)
        return [make_material(mat, self._texture_asset) for mat in mats]

    LIGHTMAP_HDR_FORMATS = (15, 16, 17, 18, 19, 20, 24, 25)  # half/float formats and BC6H

    def scene_settings(self, assets_file):
        """(RenderSettings type tree or None, [(lightmap texture Asset or None, mode)]) of a scene file (cached)."""
        key = id(assets_file)
        if key not in self._scene_settings:
            render, lightmaps = None, []
            for obj in assets_file.objects.values():
                name = obj.type.name
                try:
                    if name == "RenderSettings" and render is None:
                        render = obj.read_typetree()
                        render["_reader"] = obj
                    elif name == "LightmapSettings" and not lightmaps:
                        for entry in obj.read().m_Lightmaps or []:
                            ptr = getattr(entry, "m_Lightmap", None) or getattr(entry, "lightmap", None)
                            reader = ptr.deref() if ptr is not None and ptr.path_id else None
                            if reader is None or reader.type.name != "Texture2D":
                                lightmaps.append((None, ""))
                                continue
                            fmt = int(reader.read().m_TextureFormat)
                            mode = ("hdr" if fmt in self.LIGHTMAP_HDR_FORMATS else
                                    "rgbm" if fmt in (4, 5, 12, 13, 47, 48, 49, 50) else "dldr")
                            lightmaps.append((self._asset_for(reader, "Texture2D", reader.peek_name()), mode))
                except Exception as e:
                    log.debug("Scene settings of %s: %s", assets_file.name, e)
            self._scene_settings[key] = (render, lightmaps)
        return self._scene_settings[key]

    def _data_materials(self, asset):
        """Materials for a mesh no renderer uses, from a game data asset (ScriptableObject) in the same container
        that lists the mesh: a Material reference, or a material's name, next to it (games that put meshes on
        objects from code, e.g. Procelio's "MeshInfo" assets: {"defaultMaterial": "RailgunPart", "lods": [...]})."""
        if not asset.path:
            return []
        if self._container_scripts is None:
            self._container_scripts, self._material_names = {}, {}
            with self.lock:
                for obj in self.env.objects:
                    name = obj.type.name
                    if name == "MonoBehaviour" and getattr(obj, "container", None):
                        self._container_scripts.setdefault(obj.container, []).append(obj)
                    elif name == "Material":
                        try:
                            mat_name = obj.peek_name() or ""
                        except Exception:
                            continue
                        self._material_names.setdefault(mat_name, obj)
                        stem = os.path.splitext(os.path.basename(getattr(obj, "container", None) or ""))[0]
                        self._material_keys.append((_loose(mat_name), _loose(stem), len(mat_name), obj))
        target = (id(asset.ref.assets_file), asset.ref.path_id)
        for mb in self._container_scripts.get(asset.path, [])[:20]:
            with self.lock:
                tree, _note = self._scripts().read(mb)
            chain = self._path_to(tree, mb, target)
            # Only the entry that lists the mesh itself: higher up are other parts' and effects' materials.
            readers = self._materials_in(chain[-1], mb) if chain else []
            if readers:
                with self.lock:
                    return [read_material(r.read()) for r in readers]
        return []

    def _material_like(self, key):
        """The material a game's own name for it most likely means ("WING_DELTA" -> MAT_MOV_WING_DELTA): its
        name or .mat file name ends with the key (shortest wins), else contains it; None if nothing does."""
        k = _loose(key)
        k = k[3:] if k.startswith("mat") and len(k) > 8 else k
        if len(k) < 5:
            return None
        ends = [(n, r) for name, stem, n, r in self._material_keys if name.endswith(k) or stem.endswith(k)]
        if not ends and len(k) >= 6:
            ends = [(n, r) for name, stem, n, r in self._material_keys if k in name or k in stem]
        return min(ends, key=lambda e: e[0])[1] if ends else None

    def _ptr_target(self, ptr, owner):
        f = self.finder._file(owner.assets_file, ptr.get("m_FileID", 0))
        return f.objects.get(ptr["m_PathID"]) if f is not None else None

    def _path_to(self, node, owner, target, depth=0):
        """Dicts from `node` down to the one holding a PPtr to `target` ((id(file), path id)), or None."""
        if depth > 12:
            return None
        if isinstance(node, dict):
            if node.get("m_PathID"):
                f = self.finder._file(owner.assets_file, node.get("m_FileID", 0))
                return [] if f is not None and (id(f), node["m_PathID"]) == target else None
            for value in node.values():
                found = self._path_to(value, owner, target, depth + 1)
                if found is not None:
                    return [node] + found
        elif isinstance(node, list):
            for value in node[:4096]:
                found = self._path_to(value, owner, target, depth + 1)
                if found is not None:
                    return found
        return None

    def _materials_in(self, node, owner):
        """Material readers a data dict names directly (PPtrs or names, also in lists of them)."""
        out = []
        for value in node.values():
            for v in (value if isinstance(value, list) else [value])[:16]:
                reader = None
                if isinstance(v, dict) and v.get("m_PathID"):
                    reader = self._ptr_target(v, owner)
                    reader = reader if reader is not None and reader.type.name == "Material" else None
                elif isinstance(v, str) and v:
                    reader = self._material_names.get(v) or self._material_like(v)
                if reader is not None and reader not in out:
                    out.append(reader)
        return out

    def _texture_asset(self, reader, name):
        return self._asset_for(reader, "Texture2D", name)

    def describe(self, asset):
        if self._is_terrain(asset):
            rows = [("File", asset.source)]
            if asset.ref["type"] == "terrain":
                rows += self.terrain(asset.ref["obj"])[2]
            return rows
        if asset.kind == "scene":
            rows = [("File", asset.source)] + ([("Path", asset.path)] if asset.path else [])
            from .sdk import view_level
            built = self._scenes.get((asset.key, view_level("lod_level")))
            if built is not None:
                rows += built[2]
            return rows
        rows = [("File", asset.source)]
        if asset.path:
            rows.append(("Path", asset.path))
        if asset.kind == "model" and self.finder.indexed:
            try:
                users, _mats, n_users = self._info(asset)
            except Exception:
                users, n_users = [], 0
            if users:
                names = sorted(set(users))
                shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 or n_users > len(users) else "")
                rows.append((f"Used by {n_users:,} object(s)", shown))
        return rows

    def related(self, asset):
        if self._is_terrain(asset):
            return "", [], ""
        if asset.kind == "sprite":
            # A sprite is a piece of a texture (sprite sheet): link to it.
            links = []
            try:
                reader = asset.ref.read().m_RD.texture.deref()
                links.append(self._asset_for(reader, "Texture2D"))
            except Exception:
                pass
            return "Sprite sheet", links, "Couldn't find the texture this sprite comes from."
        if asset.kind == "texture":
            try:
                keys = self.finder.texture_users(asset.ref)
            except Exception:
                log.exception("Finding models that use %s failed", asset.name)
                keys = set()
            links = sorted((self.by_key[k] for k in keys if k in self.by_key), key=lambda a: a.name.lower())
            sprites = [sa for sa, _r in self._sprite_sheets().get(asset.key, [])]
            title = f"Used by {len(links)} model(s)"
            if sprites:
                title += f" · {len(sprites)} sprite(s) cut from this sheet"
                links = links + sorted(sprites, key=lambda a: a.name.lower())[:1000]
            return (title, links,
                    "No models found that use this texture (UI images and sprites often aren't on models).")
        return "", [], ""


class UnityPlugin(EnginePlugin):
    id = "unity"
    name = "Unity"
    version = "1.2"
    author = "UniView"
    description = "Unity games (.assets, level files, AssetBundles, .resS) via UnityPy."

    def detect(self, path):
        if os.path.isfile(path):
            return 90 if looks_like_unity_file(path) else 0
        if unity_data_dir(path):
            return 100
        # A loose folder of asset files / bundles: sniff a few likely-looking files, shallowly
        # (this runs for every game on the Projects page, so it must stay cheap on big folders).
        listed = sniffed = 0
        root_depth = path.rstrip("\\/").count(os.sep)
        for dirpath, dirs, files in os.walk(path):
            if dirpath.count(os.sep) - root_depth >= 3:
                dirs[:] = []
            for name in files:
                listed += 1
                if listed > 3000 or sniffed > 40:
                    return 0
                if os.path.splitext(name)[1].lower() not in SNIFF_EXTS:
                    continue
                sniffed += 1
                if looks_like_unity_file(os.path.join(dirpath, name)):
                    return 40
        return 0

    def game_info(self, path):
        return detect_unity_info(path)

    def count_files(self, path):
        return len(find_unity_files(path))

    def short_version(self, info):
        return unity_branch(info.get("engine_version"))

    @staticmethod
    def _ext(kind, obj):
        if kind == "font":
            try:
                data = obj.read().m_FontData
                return "otf" if bytes(data[:4]) == b"OTTO" else "ttf"
            except Exception:
                return "ttf"
        if kind == "video":
            try:
                path = obj.read_typetree().get("m_OriginalPath", "")
                return os.path.splitext(path)[1].lstrip(".").lower() or "mp4"
            except Exception:
                return "mp4"
        return {"text": "txt", "audio": "wav", "animation": "json", "data": "json", "controller": "json"}.get(kind, "")

    MAX_PREFAB_SCAN = 200_000

    @staticmethod
    def _add_terrain(session, obj):
        try:
            name = obj.peek_name() or f"Terrain #{obj.path_id % 100000:05d}"
        except Exception:
            name = "Terrain"
        key = ("terrain",) + obj_key(obj.assets_file, obj.path_id)
        session.assets.append(Asset("model", f"Terrain: {name}", key, uid=f"terrain:{obj.assets_file.name}:{obj.path_id}",
                                    size=obj.byte_size, path=getattr(obj, "container", None) or "",
                                    source=obj.assets_file.name, ref={"type": "terrain", "obj": obj}))

    def _scenes_and_prefabs(self, env, session, transforms_by_file, prefab_containers, renderer_readers):
        """Add 'scene' assets: one per level file, one per prefab (bundle container or root object)."""
        from .unity_scene import is_scene_file, scene_name, scene_path
        scene_paths = []
        for obj in env.objects:
            if obj.type.name == "BuildSettings":
                try:
                    tree = obj.read_typetree()
                    scene_paths = tree.get("scenes") or tree.get("m_Scenes") or []
                except Exception:
                    pass
                break
        added = 0
        for file_id, (assets_file, transforms) in transforms_by_file.items():
            if not is_scene_file(assets_file):
                continue
            name = scene_name(assets_file, scene_paths)
            key = ("scene", file_id)
            session.assets.append(Asset("scene", f"Scene: {name}", key, uid=f"scene:{assets_file.name}",
                                        size=sum(t.byte_size for t in transforms),
                                        path=scene_path(assets_file, scene_paths), source=assets_file.name,
                                        ref={"type": "scene", "transforms": transforms}))
            added += 1
        # Every bundle prefab: ones with nothing to draw in 3D (UI, sound, logic) open as a 2D picture or
        # structure view.
        for container, gos in prefab_containers.items():
            label = container[7:] if container.lower().startswith("assets/") else container
            label = label[:-7] if label.lower().endswith(".prefab") else label
            session.assets.append(Asset("scene", f"Prefab: {label}", ("prefab", container), uid=f"prefab:{container}",
                                        size=None, path=container, source=gos[0].assets_file.name,
                                        ref={"type": "prefab", "gameobjects": gos}))
            added += 1
        # Also prefabs outside bundle .prefab entries: builds without bundles, and games that mix bundles with
        # sharedassets/resources prefabs or keep prefabs as dependencies of other bundle entries.
        covered = {obj_key(g.assets_file, g.path_id) for gos in prefab_containers.values() for g in gos}
        added += self._classic_prefabs(session, transforms_by_file, renderer_readers, covered)
        log.info("Found %d scene(s)/prefab(s)", added)

    def _classic_prefabs(self, session, transforms_by_file, renderer_readers, covered=()):
        """Root objects outside the scene files that have a mesh under them (GameObjects in `covered` are
        already listed as bundle prefabs)."""
        from .unity_scene import is_scene_file
        files = [(f, ts) for f, ts in transforms_by_file.values() if not is_scene_file(f)]
        if sum(len(ts) for _f, ts in files) > self.MAX_PREFAB_SCAN:
            log.info("Too many objects to look for prefabs without bundles - skipped")
            return 0
        father, go_of, node = {}, {}, {}
        for _f, ts in files:
            for t in ts:
                try:
                    tr = t.read()
                    k = obj_key(t.assets_file, t.path_id)
                    node[k] = t
                    fp = tr.m_Father
                    father[k] = obj_key(fp.deref().assets_file, fp.path_id) if fp.path_id else None
                    go = tr.m_GameObject
                    go_of[obj_key(go.deref().assets_file, go.path_id)] = k
                except Exception:
                    continue
        roots = set()
        for r in renderer_readers:
            if is_scene_file(r.assets_file) or (getattr(r, "container", None) or "").lower().endswith(".prefab"):
                continue
            try:
                go = r.read().m_GameObject
                k = go_of.get(obj_key(go.deref().assets_file, go.path_id))
            except Exception:
                continue
            for _ in range(200):
                if k is None:
                    break
                if father.get(k) is None:
                    roots.add(k)
                    break
                k = father[k]
        if covered:
            roots -= {t for g, t in go_of.items() if g in covered}
        added = 0
        res_paths = getattr(session, "resource_paths", None) or {}
        for k in roots:
            t = node[k]
            path = ""
            try:
                go = t.read().m_GameObject.deref()
                name = go.peek_name() or "prefab"
                path = res_paths.get((go.assets_file.name.lower(), go.path_id), "")  # loaded with Resources.Load
            except Exception:
                name = "prefab"
            session.assets.append(Asset("scene", f"Prefab: {name}", ("root", k[1], id(t.assets_file)),
                                        uid=f"prefab:{t.assets_file.name}:{t.path_id}", size=None, path=path,
                                        source=t.assets_file.name, ref={"type": "root", "transform": t}))
            added += 1
        return added

    def open(self, path, progress):
        import UnityPy
        started = time.time()
        progress(f"Scanning {path} for Unity files ...")
        log.info("Scanning %s for Unity files", path)
        files = find_unity_files(path)
        if not files:
            raise FileNotFoundError(f"No Unity asset files found in:\n{path}")
        log.info("Found %d Unity files in %.1fs", len(files), time.time() - started)
        root = path if os.path.isdir(path) else os.path.dirname(path)
        env = UnityPy.Environment(path=root)
        failed = 0
        for n, fpath in enumerate(files, 1):
            rel = os.path.relpath(fpath, root)
            progress(f"Loading file {n}/{len(files)}: {rel}", n - 1, len(files))
            log.info("Loading %d/%d  %s  (%.1f MB)", n, len(files), rel, os.path.getsize(fpath) / 1e6)
            try:
                env.load_files([fpath])
            except Exception as e:
                failed += 1  # one broken/encrypted file shouldn't stop the rest
                log.warning("Could not load %s: %s: %s", rel, type(e).__name__, e)
        progress("Indexing objects ...")
        session = UnitySession(self, path, env, len(files))
        _warm_cpp2il(path)
        res_paths = resource_paths(env)
        session.resource_paths = res_paths
        transforms_by_file = {}   # id(assets file) -> (assets file, [Transform readers])
        prefab_containers = {}    # container path -> [GameObject readers]
        renderer_readers = []     # MeshFilter / SkinnedMeshRenderer (to find prefab roots in classic builds)
        for i, obj in enumerate(env.objects):
            type_name = obj.type.name
            if type_name in ("Transform", "RectTransform"):
                entry = transforms_by_file.setdefault(id(obj.assets_file), (obj.assets_file, []))
                entry[1].append(obj)
                continue
            if type_name == "GameObject":
                container = getattr(obj, "container", None)
                if container and container.lower().endswith(".prefab"):
                    prefab_containers.setdefault(container, []).append(obj)
                continue
            if type_name in ("MeshFilter", "SkinnedMeshRenderer"):
                renderer_readers.append(obj)
            if type_name == "TerrainData":
                self._add_terrain(session, obj)
                continue
            kind = TYPE_KINDS.get(type_name)
            if kind is None:
                continue
            try:
                name = obj.peek_name()
            except Exception:
                name = None
            if kind == "data" and not name:
                continue  # only named MonoBehaviours: data assets (ScriptableObjects), not components
            if kind == "font":
                try:
                    if not obj.read().m_FontData:
                        continue  # a reference to a built-in/OS font: nothing to show or export
                except Exception:
                    continue
            container = getattr(obj, "container", None) or res_paths.get((obj.assets_file.name.lower(), obj.path_id))
            if not name:
                # Unnamed (common for combined/prefab meshes): name it after where it lives.
                stem = os.path.splitext(os.path.basename(container))[0] if container else type_name
                name = f"{stem} #{obj.path_id % 100000:05d}"
            key = obj_key(obj.assets_file, obj.path_id)
            asset = Asset(kind, name, key, uid=f"{obj.assets_file.name}:{obj.path_id}", size=obj.byte_size,
                          path=container or "", source=obj.assets_file.name, ref=obj,
                          ext=self._ext(kind, obj))
            session.assets.append(asset)
            session.by_key[key] = asset
            if i % 2000 == 0:
                progress(f"Indexing objects ... {i}")
        try:
            progress("Finding scenes and prefabs ...")
            self._scenes_and_prefabs(env, session, transforms_by_file, prefab_containers, renderer_readers)
        except Exception:
            log.exception("Listing scenes and prefabs failed")
        if failed:
            session.warnings.append(f"{failed} file(s) could not be loaded (see the console).")
        version = next((v for v in (getattr(f, "unity_version", "") for f in getattr(env, "assets", []))
                        if v and UNITY_VERSION_RE.fullmatch(v.encode()) and not v.startswith("0.0")), "")
        session.engine_version = version
        log.info("Unity load done in %.1fs (%d file(s) failed)", time.time() - started, failed)
        return session


PLUGIN = UnityPlugin()
