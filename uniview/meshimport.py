"""Read OBJ / glTF (.glb, .gltf) models for Mod Maker's mesh replacement.

Everything comes back in the file's own (right-handed, Y-up, counter-clockwise) space, merged into one
vertex list: what UniView's own OBJ/GLB export writes, so an exported model edited in Blender comes back
lined up. Submeshes follow the materials (OBJ `usemtl`, glTF primitive materials) in order of appearance.
"""

import base64
import json
import os
import struct

import numpy as np


class ImportedMesh:
    """points (N,3) float32; submeshes [ (M,3) int64 ]; normals/colors (N,3)/(N,4) or None;
    uvs {channel: (N,2)} with OBJ's/Unity's origin (bottom-left); submesh_names [str]."""

    def __init__(self, points, submeshes, normals=None, uvs=None, colors=None, submesh_names=None):
        self.points = np.asarray(points, np.float32).reshape(-1, 3)
        self.submeshes = [np.asarray(s, np.int64).reshape(-1, 3) for s in submeshes]
        self.normals = None if normals is None else np.asarray(normals, np.float32).reshape(-1, 3)
        self.uvs = {k: np.asarray(v, np.float32).reshape(-1, 2) for k, v in (uvs or {}).items()}
        self.colors = None if colors is None else np.asarray(colors, np.float32).reshape(-1, 4)
        self.submesh_names = list(submesh_names or [f"part{i}" for i in range(len(self.submeshes))])

    @property
    def triangle_count(self):
        return sum(len(s) for s in self.submeshes)


def load(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        mesh = read_obj(path)
    elif ext in (".glb", ".gltf"):
        mesh = read_gltf(path)
    else:
        raise ValueError("Use an .obj, .glb or .gltf model.")
    mesh.submeshes = [s for s in mesh.submeshes if len(s)] or mesh.submeshes
    if not len(mesh.points) or not mesh.triangle_count:
        raise ValueError("The model has no triangles.")
    return mesh


# --------------------------------------------------------------------------- OBJ

def read_obj(path):
    vs, vts, vns = [], [], []
    groups, order = {}, []    # (group, material) -> [faces of (v, vt, vn) index triples]
    group, material = None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            if tag == "v":
                vs.append([float(x) for x in parts[1:4]])
            elif tag == "vt":
                vts.append([float(x) for x in (parts[1:3] + ["0"])[:2]])
            elif tag == "vn":
                vns.append([float(x) for x in parts[1:4]])
            elif tag == "usemtl":
                material = " ".join(parts[1:]) or "material"
            elif tag == "g":  # UniView's export writes one group per submesh (materials can repeat)
                group = " ".join(parts[1:]) or None
            elif tag == "f":
                corners = []
                for p in parts[1:]:
                    idx = (p.split("/") + ["", ""])[:3]
                    corner = []
                    for i, table in zip(idx, (vs, vts, vns)):
                        if not i:
                            corner.append(-1)
                            continue
                        n = int(i)
                        corner.append(n - 1 if n > 0 else len(table) + n)
                    corners.append(tuple(corner))
                name = (group, material)
                if name not in groups:
                    groups[name] = []
                    order.append(name)
                for k in range(1, len(corners) - 1):  # fan-triangulate polygons
                    groups[name].append((corners[0], corners[k], corners[k + 1]))
    has_uv = bool(vts) and all(c[1] >= 0 for faces in groups.values() for tri in faces for c in tri)
    has_n = bool(vns) and all(c[2] >= 0 for faces in groups.values() for tri in faces for c in tri)
    vertex_of, points, uvs, normals, submeshes = {}, [], [], [], []
    for name in order:
        tris = []
        for tri in groups[name]:
            out = []
            for v, vt, vn in tri:
                key = (v, vt if has_uv else -1, vn if has_n else -1)
                index = vertex_of.get(key)
                if index is None:
                    index = vertex_of[key] = len(points)
                    points.append(vs[v])
                    if has_uv:
                        uvs.append(vts[vt])
                    if has_n:
                        normals.append(vns[vn])
                out.append(index)
            tris.append(out)
        submeshes.append(tris)
    names = [m or g or "default" for g, m in order]
    return ImportedMesh(points, submeshes, normals if has_n else None, {"UV0": uvs} if has_uv else None,
                        submesh_names=names)


# --------------------------------------------------------------------------- glTF

COMPONENTS = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _gltf_parts(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"glTF":
        _magic, _version, _length = struct.unpack_from("<III", data)
        pos, doc, binary = 12, None, b""
        while pos + 8 <= len(data):
            size, kind = struct.unpack_from("<II", data, pos)
            chunk = data[pos + 8:pos + 8 + size]
            if kind == 0x4E4F534A:
                doc = json.loads(chunk.decode("utf-8"))
            elif kind == 0x004E4942:
                binary = chunk
            pos += 8 + size
        if doc is None:
            raise ValueError("This .glb has no JSON chunk.")
        return doc, binary
    return json.loads(data.decode("utf-8")), b""


def _buffers(doc, binary, folder):
    out = []
    for i, buf in enumerate(doc.get("buffers", [])):
        uri = buf.get("uri")
        if uri is None:
            out.append(binary if i == 0 else b"")
        elif uri.startswith("data:"):
            out.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            with open(os.path.join(folder, uri), "rb") as f:
                out.append(f.read())
    return out


def _accessor(doc, buffers, index):
    acc = doc["accessors"][index]
    if "sparse" in acc:
        raise ValueError("Sparse glTF accessors aren't supported (export without them).")
    dtype = np.dtype(COMPONENTS[acc["componentType"]]).newbyteorder("<")
    width, count = WIDTHS[acc["type"]], acc["count"]
    if "bufferView" not in acc:
        arr = np.zeros((count, width), dtype)
    else:
        view = doc["bufferViews"][acc["bufferView"]]
        data = buffers[view["buffer"]]
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        element = dtype.itemsize * width
        stride = view.get("byteStride") or element
        if not count:
            arr = np.zeros((0, width), dtype)
        elif stride == element:
            arr = np.frombuffer(data, dtype, count * width, start).reshape(count, width)
        else:  # interleaved vertex data
            raw = np.frombuffer(data, np.uint8, (count - 1) * stride + element, start)
            rows = np.lib.stride_tricks.as_strided(raw, shape=(count, element), strides=(stride, 1))
            arr = np.ascontiguousarray(rows).view(dtype).reshape(count, width)
    if acc.get("normalized") and acc["componentType"] != 5126:
        arr = arr.astype(np.float64) / float(np.iinfo(COMPONENTS[acc["componentType"]]).max)
    return arr


def _node_matrix(node):
    if "matrix" in node:
        return np.asarray(node["matrix"], np.float64).reshape(4, 4).T
    t = np.asarray(node.get("translation", [0, 0, 0]), np.float64)
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = np.asarray(node.get("scale", [1, 1, 1]), np.float64)
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * s
    m[:3, 3] = t
    return m


def read_gltf(path):
    doc, binary = _gltf_parts(path)
    buffers = _buffers(doc, binary, os.path.dirname(path))
    nodes = doc.get("nodes", [])
    scene = doc.get("scenes", [{}])[doc.get("scene", 0)] if doc.get("scenes") else {"nodes": list(range(len(nodes)))}
    placed = []  # (mesh index, world matrix)

    def walk(i, parent):
        node = nodes[i]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            # A skinned mesh's vertices are already in the skeleton's space: glTF ignores its node transform.
            placed.append((node["mesh"], np.eye(4) if "skin" in node else world))
        for child in node.get("children", []):
            walk(child, world)

    for i in scene.get("nodes", []):
        walk(i, np.eye(4))
    if not placed and doc.get("meshes"):
        placed = [(i, np.eye(4)) for i in range(len(doc["meshes"]))]

    points, normals, uvs, uv1s, colors = [], [], [], [], []
    have = {"n": True, "uv": True, "uv1": True, "c": True}
    parts, order = {}, []
    shared = {}  # (attribute accessors, matrix) -> base vertex (primitives sharing one vertex list)
    single = len(placed) == 1  # several objects: their parts are joined by material
    count = 0
    for mesh_index, world in placed:
        for prim in doc["meshes"][mesh_index].get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue  # lines/points
            attrs = prim["attributes"]
            key = (tuple(sorted(attrs.items())), world.tobytes())
            base = shared.get(key)
            pos = _accessor(doc, buffers, attrs["POSITION"])[:, :3].astype(np.float64)
            if base is None:
                base = shared[key] = count
                p = pos @ world[:3, :3].T + world[:3, 3]
                points.append(p)
                if "NORMAL" in attrs:
                    nm = np.linalg.inv(world[:3, :3]).T
                    n = _accessor(doc, buffers, attrs["NORMAL"])[:, :3] @ nm.T
                    normals.append(n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12))
                else:
                    have["n"] = False
                for name, out, flag in (("TEXCOORD_0", uvs, "uv"), ("TEXCOORD_1", uv1s, "uv1")):
                    if name in attrs:
                        uv = _accessor(doc, buffers, attrs[name])[:, :2].astype(np.float64)
                        out.append(np.column_stack([uv[:, 0], 1.0 - uv[:, 1]]))  # glTF's origin is top-left
                    else:
                        have[flag] = False
                if "COLOR_0" in attrs:
                    c = _accessor(doc, buffers, attrs["COLOR_0"]).astype(np.float64)
                    colors.append(np.column_stack([c, np.ones(len(c))]) if c.shape[1] == 3 else c)
                else:
                    have["c"] = False
                count += len(pos)
            if "indices" in prim:
                idx = _accessor(doc, buffers, prim["indices"]).astype(np.int64).ravel()
            else:
                idx = np.arange(len(pos), dtype=np.int64)
            tris = idx[: len(idx) // 3 * 3].reshape(-1, 3) + base
            if np.linalg.det(world[:3, :3]) < 0:
                tris = tris[:, ::-1]  # a mirrored node flips the winding
            mat = prim.get("material")
            name = doc["materials"][mat].get("name", f"material{mat}") if mat is not None and doc.get("materials") \
                else "default"
            if single:  # one mesh: each primitive is a part (two parts may share a material)
                name = (len(order), name)
            if name not in parts:
                parts[name] = []
                order.append(name)
            parts[name].append(tris)
    if not points:
        raise ValueError("The glTF file has no triangle meshes.")
    uv_sets = {}
    if have["uv"] and uvs:
        uv_sets["UV0"] = np.vstack(uvs)
    if have["uv1"] and uv1s:
        uv_sets["UV1"] = np.vstack(uv1s)
    return ImportedMesh(np.vstack(points), [np.vstack(parts[n]) for n in order],
                        np.vstack(normals) if have["n"] and normals else None, uv_sets,
                        np.vstack(colors) if have["c"] and colors else None,
                        [n[1] if isinstance(n, tuple) else n for n in order])
