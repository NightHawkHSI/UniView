"""Exporting models: GLB (with skeleton/animation) and OBJ + MTL, plus UV layout images."""

import io
import json
import os
import struct

import numpy as np
from PIL import Image, ImageDraw

from engines.sdk import IMAGE_KINDS, KIND_LABELS, MODEL_KINDS
from uniview import __version__, pbr
from uniview.constants import APP_SHORT, log
from uniview.util import safe_filename


def export_subfolder(asset):
    """Folder that mirrors where the asset lives in the game (its path, else source file + kind)."""
    if asset.path:
        parts = asset.path.replace("\\", "/").strip("/").split("/")
        stem = os.path.splitext(parts[-1])[0]
        base = os.path.basename(asset.name.replace("\\", "/"))
        folder = parts[:-1] + ([stem] if stem.lower() != base.lower() else [])
    else:
        folder = [asset.source or "unknown", KIND_LABELS.get(asset.kind, asset.kind)]
    return os.path.join(*[safe_filename(part)[:80] for part in folder if part] or ["."])

def material_for(materials, md, index):
    """Material of submesh `index`, or None."""
    if not materials:
        return None
    slot = md.material_slots[index] if index < len(md.material_slots) else index
    return materials[min(slot, len(materials) - 1)]

def display_texture(md, materials):
    """Texture Asset to show on the model: the base texture of the material covering the most triangles."""
    for j in sorted(range(len(md.submeshes)), key=lambda j: -len(md.submeshes[j])):
        tex = pbr.base_texture(material_for(materials, md, j))
        if tex is not None:
            return tex.asset
    return next((t.asset for t in map(pbr.base_texture, materials) if t is not None), None)

GLOSS_SCALE = ("_GlossMapScale", "_Smoothness", "_SmoothnessRemapMax")
GLOSS = ("_Glossiness", "_Smoothness")
EMISSION_COLOR = ("_EmissionColor", "_EmissiveColor", "_GlowColor")
EXTRA_MAPS = (pbr.HEIGHT, pbr.SPECULAR, pbr.DETAIL_ALBEDO, pbr.DETAIL_NORMAL, pbr.DETAIL_MASK)  # no glTF slot


def _normal_image(session, tex):
    return pbr.unpack_normal(session.image(tex.asset))


def base_texture(mat, maps):
    """The albedo TextureRef, else the material's main texture unless it's known to be another map."""
    return maps[pbr.ALBEDO] if pbr.ALBEDO in maps else pbr.base_texture(mat)


def metal_rough_sources(maps):
    """(channel, TextureRef, second (channel, TextureRef) or None) of the maps holding metallic/roughness, or None."""
    for ch in (pbr.METAL_GLOSS, pbr.MASK, pbr.ORM):
        if ch in maps:
            return ch, maps[ch], None
    metal = (pbr.METALLIC, maps[pbr.METALLIC]) if pbr.METALLIC in maps else None
    rough = next(((ch, maps[ch]) for ch in (pbr.ROUGHNESS, pbr.SMOOTHNESS) if ch in maps), None)
    first = metal or rough
    if first is None:
        return None
    return first[0], first[1], rough if metal and rough else None


def uv_transform(mat, maps):
    """(scale, offset) tiling of a material's textures, or None: its base texture's, which Unity's Standard
    shaders use for every map (else the first tiled texture's)."""
    base = base_texture(mat, maps)
    tiled = base if base is not None and getattr(base, "tiled", False) else next(
        (t for t in mat.textures if getattr(t, "tiled", False)), None)
    return (tiled.uv_scale, tiled.uv_offset) if tiled is not None else None


def gltf_texture_info(index, transform):
    """A glTF textureInfo, with KHR_texture_transform for the material's tiling. glTF's V runs top-down, so the
    Unity offset (bottom-up) becomes 1 - scale - offset."""
    info = {"index": index}
    if transform is not None:
        (sx, sy), (ox, oy) = transform
        info["extensions"] = {"KHR_texture_transform": {"offset": [ox, 1.0 - sy - oy], "scale": [sx, sy]}}
    return info


def _gltf_maps(mat, maps, texture):
    """glTF material fields (base color / normal / metallicRoughness / occlusion / emissive textures and factors)
    for a Material whose textures pbr.assign() sorted into `maps`. texture(TextureRef, convert=None) -> index."""
    out = {}
    st = uv_transform(mat, maps)
    base = base_texture(mat, maps)
    if base is not None and (i := texture(base)) is not None:
        out["baseColorTexture"] = gltf_texture_info(i, st)
    if pbr.NORMAL in maps and (i := texture(maps[pbr.NORMAL], ("normal", _normal_image))) is not None:
        out["normalTexture"] = gltf_texture_info(i, st)
        scale = pbr.property_float(mat, ("_BumpScale", "_NormalScale"))
        if scale is not None and scale != 1:
            out["normalTexture"]["scale"] = scale

    metallic = pbr.property_float(mat, ("_Metallic",))
    smooth = pbr.property_float(mat, GLOSS)
    out["metallicFactor"] = min(1.0, max(0.0, metallic)) if metallic is not None else 0.0
    out["roughnessFactor"] = min(1.0, max(0.0, 1 - smooth)) if smooth is not None else 1.0
    found = metal_rough_sources(maps)
    if found is not None:
        ch, tex, second = found
        if ch == pbr.ORM:
            i = texture(tex)  # already glTF's layout
        else:
            scale = pbr.property_float(mat, GLOSS_SCALE)
            scale = 1.0 if scale is None else min(1.0, max(0.0, scale))

            def repack(session, t, ch=ch, second=second, scale=scale):
                other = (second[0], session.image(second[1].asset)) if second else None
                return pbr.gltf_metal_rough(ch, session.image(t.asset), scale, other)

            tag = f"mr:{ch}:{scale}" + (f":{second[0]}:{second[1].asset.key}" if second else "")
            i = texture(tex, (tag, repack))
        if i is not None:
            out["metallicRoughnessTexture"] = gltf_texture_info(i, st)
            channels = {ch} | ({second[0]} if second else set())
            if channels & {pbr.METAL_GLOSS, pbr.MASK, pbr.ORM, pbr.METALLIC}:
                out["metallicFactor"] = 1.0
            if channels & {pbr.METAL_GLOSS, pbr.MASK, pbr.ORM, pbr.ROUGHNESS, pbr.SMOOTHNESS}:
                out["roughnessFactor"] = 1.0

    occlusion = None
    if pbr.AO in maps:
        occlusion = texture(maps[pbr.AO])
    elif pbr.ORM in maps:
        occlusion = texture(maps[pbr.ORM])
    elif pbr.MASK in maps:
        occlusion = texture(maps[pbr.MASK], ("ao", lambda session, t: pbr.occlusion(pbr.MASK, session.image(t.asset))))
    if occlusion is not None:
        out["occlusionTexture"] = gltf_texture_info(occlusion, st)
        strength = pbr.property_float(mat, ("_OcclusionStrength", "_AORemapMax"))
        if strength is not None and strength != 1:
            out["occlusionTexture"]["strength"] = min(1.0, max(0.0, strength))

    color = pbr.property_color(mat, EMISSION_COLOR)
    if pbr.EMISSION in maps and (color is None or max(color) > 0.001):
        if (i := texture(maps[pbr.EMISSION])) is not None:
            out["emissiveTexture"] = gltf_texture_info(i, st)
            out["emissiveFactor"] = list(color or (1.0, 1.0, 1.0))
    elif color is not None and max(color) > 0.001:
        out["emissiveFactor"] = list(color)
    return out


def _save_extra_maps(session, maps, folder, stem, already):
    """PNGs next to a GLB for the maps glTF has no slot for (height, specular, detail). Returns the new paths."""
    written = []
    for ch in EXTRA_MAPS:
        tex = maps.get(ch)
        if tex is None:
            continue
        target = os.path.join(folder, f"{stem}_{safe_filename(tex.name or ch)}_{ch}.png")
        if target in already or target in written:
            continue
        try:
            with session.lock:
                img = _normal_image(session, tex) if ch == pbr.DETAIL_NORMAL else session.image(tex.asset)
            img.save(target)
            written.append(target)
        except Exception as e:
            log.warning("Could not save the %s map %s: %s", ch, os.path.basename(target), e)
    return written


def write_glb(session, md, materials, path, rig=None, animation=None, image_uri=None):
    """Binary glTF: positions, normals, UVs, vertex colors and the materials' textures (base color, normal,
    metallic/roughness, occlusion, emission - see _gltf_maps).

    One primitive per submesh, each with its material. Opens directly in Blender.
    rig / animation (see GameSession.skeleton): adds the skeleton, the skin weights and an animation.
    image_uri(texture Asset) -> relative path/URI: reference that image file instead of embedding a copy
    (None -> embed as usual, and save maps glTF has no slot for - height, specular, detail - as PNGs next to it).
    """
    count = len(md.points)
    extras_folder = os.path.dirname(path) if image_uri is None and materials else None
    extras_stem = os.path.splitext(os.path.basename(path))[0]
    extra_files = []
    buf, views, accessors = bytearray(), [], []

    def add_view(data, target=None):
        while len(buf) % 4:
            buf.append(0)
        view = {"buffer": 0, "byteOffset": len(buf), "byteLength": len(data)}
        if target:
            view["target"] = target
        buf.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(arr, kind, component=5126, target=34962, bounds=False):
        arr = np.ascontiguousarray(arr)
        acc = {"bufferView": add_view(arr.tobytes(), target), "componentType": component,
               "count": len(arr), "type": kind}
        if bounds:
            acc["min"] = np.atleast_1d(arr.min(axis=0)).tolist()
            acc["max"] = np.atleast_1d(arr.max(axis=0)).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    attributes = {"POSITION": add_accessor(md.points, "VEC3", bounds=True)}
    if md.normals is not None:
        normals = md.normals.astype(np.float32)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.where(lengths > 1e-8, normals / np.maximum(lengths, 1e-8), [0, 1, 0]).astype(np.float32)
        attributes["NORMAL"] = add_accessor(normals, "VEC3")
    if md.uvs:
        uv = next(iter(md.uvs.values())).copy()
        uv[:, 1] = 1.0 - uv[:, 1]  # glTF's UV origin is top-left
        attributes["TEXCOORD_0"] = add_accessor(uv, "VEC2")
    if md.colors is not None and len(md.colors) == count:
        attributes["COLOR_0"] = add_accessor(np.clip(md.colors, 0, 1).astype(np.float32), "VEC4")
    if rig is not None:
        # Vertices point at skin bones; the skin lists every joint, so map bones -> joints.
        skin_joints = np.asarray(rig["skin_joints"], np.int64)
        attributes["JOINTS_0"] = add_accessor(skin_joints[rig["joints_0"]].astype(np.uint16), "VEC4", 5123)
        attributes["WEIGHTS_0"] = add_accessor(np.asarray(rig["weights_0"], np.float32), "VEC4")

    gl_materials, images, textures, texture_index, mat_index = [], [], [], {}, {}

    def texture(tex, convert=None):
        """glTF texture index for a TextureRef (referenced or embedded once), or None if it can't be read.
        convert: (tag, fn(session, tex) -> PIL image) - embed a converted copy instead (e.g. a repacked map)."""
        key = (tex.asset.key, convert[0] if convert else None)
        if key not in texture_index:
            texture_index[key] = None
            # Referenced files are what the project export wrote: plain images, normal maps already unpacked.
            uri = image_uri(tex.asset) if image_uri is not None and (convert is None or convert[0] == "normal") else None
            if uri:  # an image file next to the model: reference it
                images.append({"uri": uri, "name": tex.name or "texture"})
            elif image_uri is not None and convert is not None:
                return None  # project export: no converted copies inside the model
            else:
                try:
                    png = io.BytesIO()
                    with session.lock:
                        img = convert[1](session, tex) if convert else session.image(tex.asset)
                    img.convert("RGBA").save(png, "PNG")
                    images.append({"bufferView": add_view(png.getvalue()), "mimeType": "image/png",
                                   "name": tex.name or "texture"})
                except Exception as e:
                    log.warning("Could not embed a texture in %s: %s", os.path.basename(path), e)
            if len(images) > len(textures):
                textures.append({"source": len(images) - 1, "sampler": 0})
                texture_index[key] = len(textures) - 1
        return texture_index[key]

    for mat in materials:
        entry = {"name": mat.name or "material", "doubleSided": True,
                 "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        pbr_entry = entry["pbrMetallicRoughness"]
        if mat.color is not None:
            pbr_entry["baseColorFactor"] = [min(1.0, max(0.0, float(c))) for c in (list(mat.color) + [1.0])[:4]]
        maps = pbr.assign(mat.textures)
        for key, value in _gltf_maps(mat, maps, texture).items():
            (pbr_entry if key in ("baseColorTexture", "metallicRoughnessTexture", "metallicFactor", "roughnessFactor")
             else entry)[key] = value
        if extras_folder is not None:
            extra_files.extend(_save_extra_maps(session, maps, extras_folder, extras_stem, extra_files))
        mat_index[id(mat)] = len(gl_materials)
        gl_materials.append(entry)

    primitives = []
    for j, tris in enumerate(md.submeshes):
        if not len(tris):
            continue
        indices = np.asarray(tris, dtype=np.uint32).ravel()
        prim = {"attributes": attributes, "mode": 4,
                "indices": add_accessor(indices, "SCALAR", 5125, 34963)}
        mat = material_for(materials, md, j)
        if mat is not None:
            prim["material"] = mat_index[id(mat)]
        primitives.append(prim)
    if not primitives:
        raise ValueError("Mesh has no triangles to export.")

    name = md.name or os.path.splitext(os.path.basename(path))[0]
    gltf = {
        "asset": {"version": "2.0", "generator": f"{APP_SHORT} {__version__}"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": primitives}],
        "accessors": accessors, "bufferViews": views,
    }
    if rig is not None:
        _add_skeleton(gltf, rig, animation, add_accessor)
    if gl_materials:
        gltf["materials"] = gl_materials
    if '"KHR_texture_transform"' in json.dumps(gl_materials):
        gltf["extensionsUsed"] = ["KHR_texture_transform"]
    if images:
        gltf.update(images=images, textures=textures,
                    samplers=[{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}])
    while len(buf) % 4:
        buf.append(0)
    gltf["buffers"] = [{"byteLength": len(buf)}]
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(buf)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(buf), 0x004E4942))
        f.write(buf)
    return [path] + extra_files

def _add_skeleton(gltf, rig, animation, add_accessor):
    """Joint nodes, the skin on node 0 (the mesh) and, if given, the animation."""
    joints = rig["joints"]
    first = len(gltf["nodes"])
    nodes = [{"name": j["name"], "translation": j["translation"], "rotation": j["rotation"], "scale": j["scale"]}
             for j in joints]
    for i, j in enumerate(joints):
        if j["parent"] >= 0:
            nodes[j["parent"]].setdefault("children", []).append(first + i)
    gltf["nodes"] += nodes
    roots = [first + i for i, j in enumerate(joints) if j["parent"] < 0]
    gltf["scenes"][0]["nodes"] += roots
    # Inverse bind matrices: the skin bones' own; other joints get their rest pose's inverse.
    world = []
    for j in joints:
        m = np.eye(4)
        x, y, z, w = j["rotation"]
        m[:3, :3] = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                              [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                              [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]) * j["scale"]
        m[:3, 3] = j["translation"]
        world.append(world[j["parent"]] @ m if j["parent"] >= 0 else m)
    inverse = [np.linalg.inv(m) for m in world]
    for bone, joint in enumerate(rig["skin_joints"]):
        if bone < len(rig["inverse_bind"]):
            inverse[joint] = rig["inverse_bind"][bone]
    ibm = np.stack(inverse).transpose(0, 2, 1).astype(np.float32)  # glTF matrices are column-major
    gltf["skins"] = [{"joints": list(range(first, first + len(joints))), "skeleton": roots[0],
                      "inverseBindMatrices": add_accessor(ibm, "MAT4", target=None)}]
    gltf["nodes"][0]["skin"] = 0
    if not animation or not animation["channels"]:
        return
    times = add_accessor(np.asarray(animation["times"], np.float32), "SCALAR", target=None, bounds=True)
    samplers, channels = [], []
    for ch in animation["channels"]:
        values = np.asarray(ch["values"], np.float32)
        samplers.append({"input": times, "interpolation": "LINEAR",
                         "output": add_accessor(values, "VEC4" if values.shape[1] == 4 else "VEC3", target=None)})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": first + ch["joint"], "path": ch["path"]}})
    gltf["animations"] = [{"name": animation["name"] or "animation", "samplers": samplers, "channels": channels}]

def write_obj(session, md, materials, path):
    """<name>.obj + <name>.mtl + PNG textures, so it opens textured in Blender. The MTL links the base color,
    normal (map_Bump), metallic (map_Pm), roughness (map_Pr), emission (map_Ke) and height (disp) maps; the
    material's other textures are saved next to it too."""
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    written, mtl, mat_names = [path], [], {}

    def save(tex, suffix="", make=None):
        """File name of a texture (converted by make(session, tex) if given) saved next to the OBJ, or None."""
        png = f"{stem}_{safe_filename(tex.name)}{suffix}.png"
        png_path = os.path.join(folder, png)
        if png_path in written:
            return png
        try:
            with session.lock:
                img = make(session, tex) if make else session.image(tex.asset)
            img.save(png_path)
        except Exception as e:
            log.warning("Could not save the texture %s: %s", png, e)
            return None
        written.append(png_path)
        return png

    for i, mat in enumerate(materials):
        mat_name = f"{safe_filename(mat.name or 'material')}_{i}"
        mat_names[id(mat)] = mat_name
        kd = " ".join(f"{min(1.0, max(0.0, c)):.4f}" for c in mat.color[:3]) if mat.color is not None else "1 1 1"
        mtl += [f"newmtl {mat_name}", f"Kd {kd}"]
        maps = pbr.assign(mat.textures)
        used = set()
        st = uv_transform(mat, maps)
        opt = (f"-s {st[0][0]:g} {st[0][1]:g} 1 -o {st[1][0]:g} {st[1][1]:g} 0 " if st else "")  # MTL tiling
        base = base_texture(mat, maps)
        if base is not None and (png := save(base)):
            mtl.append(f"map_Kd {opt}{png}")
            used.add(id(base))
        if pbr.NORMAL in maps and (png := save(maps[pbr.NORMAL], make=_normal_image)):
            mtl.append(f"map_Bump {opt}{png}")
            used.add(id(maps[pbr.NORMAL]))
        metallic = pbr.property_float(mat, ("_Metallic",))
        smooth = pbr.property_float(mat, GLOSS)
        found = metal_rough_sources(maps)
        sources = {}
        if found is not None:
            sources[found[0]] = found[1]
            if found[2]:
                sources[found[2][0]] = found[2][1]
        for want, key, channels in ((pbr.METALLIC, "Pm", (pbr.METALLIC, pbr.METAL_GLOSS, pbr.MASK, pbr.ORM)),
                                    (pbr.ROUGHNESS, "Pr", (pbr.ROUGHNESS, pbr.SMOOTHNESS, pbr.METAL_GLOSS, pbr.MASK,
                                                           pbr.ORM))):
            ch = next((c for c in channels if c in sources), None)
            if ch is not None:
                tex = sources[ch]
                png = save(tex, f"_{want}", lambda session, t, ch=ch, want=want:
                           pbr.single(ch, session.image(t.asset), want))
                if png:
                    mtl.append(f"map_{key} {opt}{png}")
                    used.add(id(tex))
                    continue
            value = metallic if want == pbr.METALLIC else (1 - smooth if smooth is not None else None)
            if value is not None:
                mtl.append(f"{key} {min(1.0, max(0.0, value)):.4f}")
        if pbr.EMISSION in maps and (png := save(maps[pbr.EMISSION])):
            color = pbr.property_color(mat, EMISSION_COLOR) or (1.0, 1.0, 1.0)
            mtl += ["Ke " + " ".join(f"{c:.4f}" for c in color), f"map_Ke {opt}{png}"]
            used.add(id(maps[pbr.EMISSION]))
        if pbr.HEIGHT in maps and (png := save(maps[pbr.HEIGHT])):
            mtl.append(f"disp {opt}{png}")
            used.add(id(maps[pbr.HEIGHT]))
        for tex in mat.textures:  # occlusion, detail maps, masks...: saved next to it
            if id(tex) not in used:
                save(tex, make=_normal_image if tex is maps.get(pbr.DETAIL_NORMAL) else None)
        mtl.append("")

    uv = next(iter(md.uvs.values()), None)
    lines = [f"# {APP_SHORT} {__version__}"]
    if materials:
        lines.append(f"mtllib {stem}.mtl")
    lines.append(f"o {safe_filename(md.name or stem)}")
    lines += [f"v {x:.6g} {y:.6g} {z:.6g}" for x, y, z in md.points.tolist()]
    if uv is not None:
        lines += [f"vt {u:.6g} {v:.6g}" for u, v in uv.tolist()]
    if md.normals is not None:
        lines += [f"vn {x:.6g} {y:.6g} {z:.6g}" for x, y, z in md.normals.tolist()]
    if uv is not None:
        fmt = "{0}/{0}/{0}" if md.normals is not None else "{0}/{0}"
    else:
        fmt = "{0}//{0}" if md.normals is not None else "{0}"
    for j, tris in enumerate(md.submeshes):
        mat = material_for(materials, md, j)
        lines.append(f"g part{j}")
        if mat is not None:
            lines.append(f"usemtl {mat_names[id(mat)]}")
        for a, b, c in (np.asarray(tris) + 1).tolist():
            lines.append(f"f {fmt.format(a)} {fmt.format(b)} {fmt.format(c)}")
    if materials:
        mtl_path = os.path.join(folder, stem + ".mtl")
        with open(mtl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(mtl))
        written.append(mtl_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return written

def render_uv_layout(poly, channel, texture_img, max_tris=80000):
    """Draw a mesh's UV triangles over its texture (or a grid) to check layouts / lightmap UVs."""
    uv = np.asarray(poly.point_data[channel])
    faces = poly.faces.reshape(-1, 4)[:, 1:][:max_tris]
    if texture_img is not None:
        base = texture_img.convert("RGBA")
        if max(base.size) < 1024:
            factor = max(1, 1024 // max(base.size))
            base = base.resize((base.width * factor, base.height * factor), Image.NEAREST)
        else:
            base = base.copy()
    else:
        base = Image.new("RGBA", (1024, 1024), (38, 38, 38, 255))
        grid = ImageDraw.Draw(base)
        for i in range(0, 1025, 128):
            grid.line([(i, 0), (i, 1024)], fill=(70, 70, 70, 255))
            grid.line([(0, i), (1024, i)], fill=(70, 70, 70, 255))
    w, h = base.size
    pts = np.column_stack([uv[:, 0] * w, (1.0 - uv[:, 1]) * h])
    draw = ImageDraw.Draw(base, "RGBA")
    for tri in faces:
        a, b, c = pts[tri]
        draw.line([tuple(a), tuple(b), tuple(c), tuple(a)], fill=(0, 255, 140, 210), width=1)
    return base


# --------------------------------------------------------------------------- saving assets (no UI)

def session_materials(session, asset):
    """Materials of a model, or [] (logged) if the plugin fails."""
    try:
        with session.lock:
            return session.materials(asset)
    except Exception:
        log.exception("Finding the materials of '%s' failed", asset.name)
        return []


def export_ext(asset, model_format="obj"):
    """File extension an asset is saved with."""
    if asset.kind in MODEL_KINDS:
        return model_format
    if asset.kind in IMAGE_KINDS:
        return "png"
    if asset.kind == "audio" and asset.ext in ("vsnd_c", ""):
        return "wav"  # decoded on save (the real extension is used if it differs)
    return asset.ext or ("txt" if asset.kind == "text" else "bin")


def export_stem(asset, ext=None):
    """File name (without extension) for saving an asset: its base name, minus a duplicate extension."""
    stem = os.path.basename(asset.name.replace("\\", "/"))
    if ext and asset.kind in ("text", "file", "audio") and stem.lower().endswith("." + ext.lower()):
        stem = stem[: -len(ext) - 1]
    return safe_filename(stem)


def rig_for_export(session, asset, md):
    """Skeleton for a rigged GLB of a skinned model, or None (static export)."""
    if asset.kind != "model":
        return None
    try:
        rig, _anim = session.skeleton(asset)
    except (NotImplementedError, ValueError):
        return None
    except Exception:
        log.exception("Reading the skeleton of '%s' failed; saving it without bones", asset.name)
        return None
    return rig if len(rig["joints_0"]) == len(md.points) else None


def write_asset(session, asset, path):
    """Save one asset to `path` (.obj/.glb for models); returns the list of files written."""
    with session.lock:
        if asset.kind in MODEL_KINDS:
            md = session.mesh(asset)
            materials = session_materials(session, asset)  # waits for background indexing if needed
            if path.lower().endswith(".glb"):
                return write_glb(session, md, materials, path, rig=rig_for_export(session, asset, md))
            return write_obj(session, md, materials, path)
        if asset.kind in IMAGE_KINDS:
            session.image(asset).save(path)
            return [path]
        if asset.kind == "audio":
            try:
                data, ext = session.audio(asset)
                path = os.path.splitext(path)[0] + "." + ext
                with open(path, "wb") as f:
                    f.write(data)
                return [path]
            except NotImplementedError:
                pass  # not decodable: save the stored file below
        try:
            data = session.raw(asset)
        except NotImplementedError:
            text = session.text(asset)
            data = text.encode("utf-8") if isinstance(text, str) else text
        with open(path, "wb") as f:
            f.write(data)
        return [path]


def write_animated_glb(session, model, clip, path):
    """`model` with its skeleton and `clip` as a GLB. Returns (files written, rig)."""
    with session.lock:
        md = session.mesh(model)
        rig, animation = session.skeleton(model, clip)
        materials = session_materials(session, model)
        return write_glb(session, md, materials, path, rig=rig, animation=animation), rig


def plan_export(items, folder, model_format="obj", keep_structure=False):
    """[(asset, target path)] for a bulk export: optionally mirrored game folders, unique file names."""
    plan, used = [], set()
    for asset in items:
        ext = export_ext(asset, model_format)
        sub = export_subfolder(asset) if keep_structure else ""
        target = os.path.normpath(os.path.join(folder, sub))
        base = export_stem(asset, ext)
        fname, i = base, 1
        while (target.lower(), fname.lower()) in used:
            i += 1
            fname = f"{base}_{i}"
        used.add((target.lower(), fname.lower()))
        plan.append((asset, os.path.join(target, f"{fname}.{ext}")))
    return plan
