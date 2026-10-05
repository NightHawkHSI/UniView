"""Put an imported model (meshimport.ImportedMesh) into a Unity Mesh's typetree, for Mod Maker.

The mesh keeps the game's vertex layout (same channels, formats and streams); only the vertex count
changes. Channels the model brings (positions, normals, UVs, colors) are encoded into it; tangents and
normals the model lacks are computed; everything else (lightmap UVs, bone weights of skinned meshes...)
is copied from the nearest vertex of the original mesh, so a reshaped character keeps its rig.
Unity 5 and newer, uncompressed meshes, little-endian (PC) files.
"""

import numpy as np

# channel index -> meaning, by Unity version
CHANNELS_2018 = ("position", "normal", "tangent", "color", "uv0", "uv1", "uv2", "uv3", "uv4", "uv5", "uv6",
                 "uv7", "weights", "bones")
CHANNELS_5 = ("position", "normal", "color", "uv0", "uv1", "uv2", "uv3", "tangent")
# format code -> (numpy dtype, kind): f float, un/sn normalized unsigned/signed, u/s integer
FORMATS_2019 = {0: ("<f4", "f"), 1: ("<f2", "f"), 2: ("u1", "un"), 3: ("i1", "sn"), 4: ("<u2", "un"),
                5: ("<i2", "sn"), 6: ("u1", "u"), 7: ("i1", "s"), 8: ("<u2", "u"), 9: ("<i2", "s"),
                10: ("<u4", "u"), 11: ("<i4", "s")}
FORMATS_2017 = {0: ("<f4", "f"), 1: ("<f2", "f"), 2: ("u1", "un"), 3: ("u1", "un"), 4: ("i1", "sn"),
                5: ("<u2", "un"), 6: ("<i2", "sn"), 7: ("u1", "u"), 8: ("i1", "s"), 9: ("<u2", "u"),
                10: ("<i2", "s"), 11: ("<u4", "u"), 12: ("<i4", "s")}
FORMATS_5 = {0: ("<f4", "f"), 1: ("<f2", "f"), 2: ("u1", "un"), 3: ("u1", "u"), 4: ("<u4", "u")}


class MeshWriteError(ValueError):
    pass


def _formats(version):
    return FORMATS_2019 if version >= (2019,) else FORMATS_2017 if version >= (2017,) else FORMATS_5


def supported(tree, version):
    """Why this mesh can't be replaced, or None."""
    vd = tree.get("m_VertexData") or {}
    if version < (5,) or "m_Streams" in vd or "m_Channels" not in vd:
        return "Meshes from Unity 4 and older can't be replaced yet."
    if tree.get("m_MeshCompression"):
        return "The game stores this mesh compressed (Mesh Compression), which Mod Maker can't write yet."
    return None


def _layout(channels, version, count):
    """[(channel index, meaning, dtype, kind, dimension, stream, offset)], [(stream offset, stride)]"""
    names = CHANNELS_2018 if version >= (2018,) else CHANNELS_5
    formats = _formats(version)
    out = []
    for i, ch in enumerate(channels):
        dim = int(ch["dimension"]) & 0xF
        if dim == 0:
            continue
        if ch["format"] not in formats:
            raise MeshWriteError(f"Unknown vertex format {ch['format']} in this mesh.")
        dtype, kind = formats[ch["format"]]
        if version < (2018,) and i == 2 and ch["format"] == 2:
            dim = 4  # old Color format: 4 bytes whatever the dimension says
        out.append((i, names[i] if i < len(names) else f"channel{i}", np.dtype(dtype), kind, dim,
                    int(ch["stream"]), int(ch["offset"])))
    streams, offset = [], 0
    for s in range(1 + max((c[5] for c in out), default=0)):
        stride = sum(c[2].itemsize * c[4] for c in out if c[5] == s)
        streams.append((offset, stride))
        offset += count * stride
        offset = (offset + 15) & ~15
    return out, streams, offset


def _rows(data, count, stream, ch):
    """(count, bytes per vertex) uint8 view of one channel."""
    offset, stride = stream
    size = ch[2].itemsize * ch[4]
    start = offset + ch[6]
    if not count:
        return np.zeros((0, size), np.uint8)
    raw = np.frombuffer(data, np.uint8, (count - 1) * stride + size, start)
    return np.lib.stride_tricks.as_strided(raw, shape=(count, size), strides=(stride, 1))


def _decode(rows, ch):
    values = np.ascontiguousarray(rows).view(ch[2]).reshape(len(rows), ch[4]).astype(np.float64)
    if ch[3] in ("un", "sn"):
        values /= float(np.iinfo(ch[2]).max)
    return values


def _encode(values, ch):
    values = np.asarray(values, np.float64)
    if values.shape[1] < ch[4]:
        values = np.hstack([values, np.zeros((len(values), ch[4] - values.shape[1]))])
    values = values[:, :ch[4]]
    kind, dtype = ch[3], ch[2]
    if kind in ("un", "sn"):
        top = float(np.iinfo(dtype).max)
        values = np.round(np.clip(values, 0 if kind == "un" else -1, 1) * top)
    elif kind in ("u", "s"):
        values = np.round(values)
    return np.ascontiguousarray(values.astype(dtype)).view(np.uint8).reshape(len(values), -1)


def compute_normals(points, submeshes):
    """Smooth per-vertex normals of counter-clockwise triangles."""
    normals = np.zeros_like(points, dtype=np.float64)
    for tris in submeshes:
        if not len(tris):
            continue
        a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
        face = np.cross(b - a, c - a)
        for k in range(3):
            np.add.at(normals, tris[:, k], face)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return np.where(length > 1e-12, normals / np.maximum(length, 1e-12), [0.0, 1.0, 0.0])


def compute_tangents(points, normals, uv, submeshes):
    """(N, 4) tangents with handedness in w (bitangent = cross(normal, tangent) * w), as Unity uses them."""
    t_acc = np.zeros((len(points), 3))
    b_acc = np.zeros((len(points), 3))
    for tris in submeshes:
        if not len(tris):
            continue
        p0, p1, p2 = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
        w0, w1, w2 = uv[tris[:, 0]], uv[tris[:, 1]], uv[tris[:, 2]]
        e1, e2 = p1 - p0, p2 - p0
        d1, d2 = w1 - w0, w2 - w0
        det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
        r = np.where(np.abs(det) > 1e-20, 1.0 / np.where(det == 0, 1, det), 0.0)[:, None]
        sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * r
        tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * r
        for k in range(3):
            np.add.at(t_acc, tris[:, k], sdir)
            np.add.at(b_acc, tris[:, k], tdir)
    t = t_acc - normals * np.sum(normals * t_acc, axis=1, keepdims=True)
    length = np.linalg.norm(t, axis=1, keepdims=True)
    fallback = np.cross(normals, np.where(np.abs(normals[:, 1:2]) < 0.99, [[0, 1, 0]], [[1, 0, 0]]))
    t = np.where(length > 1e-12, t / np.maximum(length, 1e-12), fallback)
    w = np.where(np.sum(np.cross(normals, t) * b_acc, axis=1) < 0, -1.0, 1.0)
    return np.column_stack([t, w])


def nearest(original, points):
    """Index of the nearest original vertex for each point."""
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkStaticPointLocator
    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(np.ascontiguousarray(original, np.float64), deep=True))
    data = vtkPolyData()
    data.SetPoints(pts)
    locator = vtkStaticPointLocator()
    locator.SetDataSet(data)
    locator.BuildLocator()
    find = locator.FindClosestPoint
    return np.fromiter((find(p) for p in np.asarray(points, np.float64).tolist()), np.int64, len(points))


def _aabb(points):
    if not len(points):
        lo = hi = np.zeros(3)
    else:
        lo, hi = points.min(axis=0), points.max(axis=0)
    c, e = (lo + hi) / 2, (hi - lo) / 2
    return {"m_Center": dict(zip("xyz", map(float, c))), "m_Extent": dict(zip("xyz", map(float, e)))}


def replace_mesh(tree, version, model, vertex_data):
    """Rewrite `tree` (a Mesh typetree, edited in place) with `model`. vertex_data: the original vertex bytes
    (m_VertexData.m_DataSize, or the .resS bytes m_StreamData points at). Returns notes for the user."""
    why = supported(tree, version)
    if why:
        raise MeshWriteError(why)
    notes = []
    vd = tree["m_VertexData"]
    old_count = int(vd["m_VertexCount"])
    channels, old_streams, old_size = _layout(vd["m_Channels"], version, old_count)
    if len(vertex_data) < old_size - 15:
        raise MeshWriteError("This mesh's vertex data is missing from the game files.")

    # The model in Unity's space: x mirrored, so the winding flips too (the inverse of UniView's export).
    flip = np.array([-1.0, 1.0, 1.0])
    points = model.points.astype(np.float64) * flip
    file_normals = model.normals.astype(np.float64) if model.normals is not None else \
        compute_normals(model.points.astype(np.float64), model.submeshes)
    normals = file_normals * flip
    submeshes = [np.asarray(t, np.int64)[:, ::-1] for t in model.submeshes]
    count = len(points)

    old_subs = tree.get("m_SubMeshes") or []
    if len(old_subs) and len(submeshes) > len(old_subs):
        notes.append(f"The model has {len(submeshes)} parts (materials) but the game's mesh has {len(old_subs)}: "
                     "the extra parts use the last material.")
        submeshes = submeshes[:len(old_subs) - 1] + [np.vstack(submeshes[len(old_subs) - 1:])]
    elif len(submeshes) < len(old_subs):
        notes.append(f"The model has {len(submeshes)} part(s) (materials) but the game's mesh has "
                     f"{len(old_subs)}: the rest are left empty.")

    pos_ch = next((c for c in channels if c[1] == "position"), None)
    if pos_ch is None or not old_count:
        raise MeshWriteError("This mesh has no vertices in the game files (it's empty or built while the game runs).")
    original_points = _decode(_rows(vertex_data, old_count, old_streams[pos_ch[5]], pos_ch), pos_ch)[:, :3]
    near = None

    def nearest_rows(ch):
        nonlocal near
        if near is None:
            near = nearest(original_points, points)
        return np.ascontiguousarray(_rows(vertex_data, old_count, old_streams[ch[5]], ch))[near]

    streams, offset = [], 0
    for s in range(len(old_streams)):
        stride = old_streams[s][1]
        streams.append((offset, stride))
        offset += count * stride
        offset = (offset + 15) & ~15
    out = np.zeros(offset, np.uint8)
    copied = []
    for ch in channels:
        meaning = ch[1]
        if meaning == "position":
            rows = _encode(points, ch)
        elif meaning == "normal":
            rows = _encode(normals, ch)
        elif meaning == "tangent" and "UV0" in model.uvs:
            rows = _encode(compute_tangents(points, normals, model.uvs["UV0"].astype(np.float64), submeshes), ch)
        elif meaning == "color" and model.colors is not None:
            rows = _encode(model.colors, ch)
        elif meaning.startswith("uv") and f"UV{meaning[2:]}" in model.uvs:
            rows = _encode(model.uvs[f"UV{meaning[2:]}"], ch)
        else:
            rows = nearest_rows(ch)
            copied.append(meaning)
        start, stride = streams[ch[5]]
        view = np.lib.stride_tricks.as_strided(out[start + ch[6]:], shape=(count, rows.shape[1]),
                                               strides=(stride, 1), writeable=True) if count else None
        if view is not None:
            view[:] = rows
    if tree.get("m_Skin"):  # bone weights of older Unity versions
        if near is None:
            near = nearest(original_points, points)
        tree["m_Skin"] = [tree["m_Skin"][i] for i in near]
        copied.append("bone weights")
    if copied:
        notes.append("Copied from the nearest original vertex: " + ", ".join(copied) + ".")
    vd["m_VertexCount"] = count
    vd["m_DataSize"] = out.tobytes()
    if "m_StreamData" in tree:
        tree["m_StreamData"] = {"offset": 0, "size": 0, "path": ""}

    # indices and submeshes
    wide = count > 65535
    if "m_IndexFormat" in tree:
        tree["m_IndexFormat"] = 1 if wide else 0
    elif "m_Use16BitIndices" in tree:
        tree["m_Use16BitIndices"] = 0 if wide else 1
    elif wide:
        raise MeshWriteError(f"The model has {count:,} vertices; this Unity version allows 65,535 per mesh.")
    dtype = np.dtype("<u4" if wide else "<u2")
    buf, subs = bytearray(), []
    templates = old_subs or [{"firstByte": 0, "indexCount": 0, "topology": 0, "baseVertex": 0, "firstVertex": 0,
                              "vertexCount": 0, "localAABB": _aabb(points)}]
    for i in range(max(len(old_subs), len(submeshes))):
        sub = dict(templates[min(i, len(templates) - 1)])
        tris = submeshes[i] if i < len(submeshes) else np.zeros((0, 3), np.int64)
        used = np.unique(tris) if len(tris) else np.zeros(0, np.int64)
        values = {"firstByte": len(buf), "indexCount": int(tris.size), "topology": 0, "baseVertex": 0,
                  "firstVertex": int(used.min()) if len(used) else 0,
                  "vertexCount": int(used.max() - used.min() + 1) if len(used) else 0,
                  "triangleCount": len(tris), "isTriStrip": 0, "localAABB": _aabb(points[used])}
        for key, value in values.items():
            if key in sub:
                sub[key] = value
        subs.append(sub)
        buf += tris.astype(dtype).tobytes()
    tree["m_SubMeshes"] = subs
    tree["m_IndexBuffer"] = bytes(buf)
    tree["m_LocalAABB"] = _aabb(points)

    shapes = tree.get("m_Shapes") or {}
    if shapes.get("shapes"):
        notes.append(f"The game's mesh had {len(shapes['shapes'])} blend shape(s) (face expressions etc.); they "
                     "were removed because they belong to the old vertices.")
        for key in ("vertices", "shapes", "channels", "fullWeights"):
            if key in shapes:
                shapes[key] = []
    variable = tree.get("m_VariableBoneCountWeights") or {}
    if variable.get("m_Data"):
        variable["m_Data"] = []
        notes.append("The mesh's extra (more than 4 per vertex) bone weights were dropped.")
    if tree.get("m_BakedConvexCollisionMesh") or tree.get("m_BakedTriangleCollisionMesh"):
        notes.append("The mesh's pre-built collision shape is kept, so collisions still follow the old shape.")
    return notes
