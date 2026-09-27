"""Exporting models: GLB (with skeleton/animation) and OBJ + MTL, plus UV layout images."""

import io
import json
import os
import struct

import numpy as np
from PIL import Image, ImageDraw

from engines.sdk import KIND_LABELS, NORMAL, main_texture
from uniview import __version__
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
        mat = material_for(materials, md, j)
        tex = mat.main_texture() if mat is not None else None
        if tex is not None:
            return tex.asset
    return main_texture(materials)

def write_glb(session, md, materials, path, rig=None, animation=None):
    """Binary glTF: positions, normals, UVs, vertex colors and embedded base-color textures.

    One primitive per submesh, each with its material. Opens directly in Blender.
    rig / animation (see GameSession.skeleton): adds the skeleton, the skin weights and an animation.
    """
    count = len(md.points)
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
    for mat in materials:
        entry = {"name": mat.name or "material", "doubleSided": True,
                 "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        if mat.color is not None:
            entry["pbrMetallicRoughness"]["baseColorFactor"] = [min(1.0, max(0.0, float(c)))
                                                                for c in (list(mat.color) + [1.0])[:4]]
        tex = mat.main_texture()
        if tex is not None:
            key = tex.asset.key
            if key not in texture_index:
                texture_index[key] = None
                try:
                    png = io.BytesIO()
                    with session.lock:
                        img = session.image(tex.asset)
                    img.convert("RGBA").save(png, "PNG")
                    images.append({"bufferView": add_view(png.getvalue()), "mimeType": "image/png",
                                   "name": tex.name or "texture"})
                    textures.append({"source": len(images) - 1, "sampler": 0})
                    texture_index[key] = len(textures) - 1
                except Exception as e:
                    log.warning("Could not embed a texture in %s: %s", os.path.basename(path), e)
            if texture_index[key] is not None:
                entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_index[key]}
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
    return [path]

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
    """<name>.obj + <name>.mtl + PNG textures, so it opens textured in Blender."""
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    written, mtl, mat_names = [path], [], {}
    for i, mat in enumerate(materials):
        mat_name = f"{safe_filename(mat.name or 'material')}_{i}"
        mat_names[id(mat)] = mat_name
        kd = " ".join(f"{min(1.0, max(0.0, c)):.4f}" for c in mat.color[:3]) if mat.color is not None else "1 1 1"
        mtl += [f"newmtl {mat_name}", f"Kd {kd}"]
        have_diffuse = False
        for tex in mat.textures:
            png = f"{stem}_{safe_filename(tex.name)}.png"
            png_path = os.path.join(folder, png)
            try:
                if png_path not in written:
                    with session.lock:
                        img = session.image(tex.asset)
                    img.save(png_path)
                    written.append(png_path)
            except Exception as e:
                log.warning("Could not save the texture %s: %s", png, e)
                continue
            if tex.role == NORMAL:
                mtl.append(f"map_Bump {png}")
            elif not have_diffuse:
                mtl.append(f"map_Kd {png}")
                have_diffuse = True
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
