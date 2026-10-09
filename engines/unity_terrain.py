"""Unity terrains: TerrainData heightmap -> mesh, terrain layers blended by their splat maps -> texture."""

import math

import numpy as np
from PIL import Image

from .sdk import MeshData, orient_to_normals

MAX_GRID = 513          # vertices per side shown (big heightmaps are sampled down)
MAX_TEXTURE = 2048      # blended texture size
FALLBACK_COLORS = ((104, 132, 72), (128, 108, 80), (132, 132, 128), (196, 178, 128),
                   (78, 104, 60), (160, 140, 110), (96, 96, 100), (220, 220, 225))  # grass, dirt, rock, sand...


def _deref(session, owner, ptr):
    """ObjectReader a typetree PPtr dict points to (relative to `owner`'s file), or None."""
    if not ptr or not ptr.get("m_PathID"):
        return None
    target = session.finder._file(owner.assets_file, ptr.get("m_FileID", 0))
    return target.objects.get(ptr["m_PathID"]) if target is not None else None


def terrain_mesh(tree):
    """MeshData of a TerrainData typetree, in the terrain's own space (x flipped like other Unity meshes)."""
    hm = tree["m_Heightmap"]
    res = hm.get("m_Resolution") or hm.get("m_Width")
    scale = hm["m_Scale"]
    heights = np.asarray(hm["m_Heights"], dtype=np.float32).reshape(res, res) / 32766.0 * scale["y"]
    step = max(1, math.ceil((res - 1) / (MAX_GRID - 1)))
    rows = np.arange(0, res, step)
    if rows[-1] != res - 1:
        rows = np.append(rows, res - 1)
    grid = heights[np.ix_(rows, rows)]
    n = len(rows)
    z, x = np.meshgrid(rows * scale["z"], rows * scale["x"], indexing="ij")
    points = np.stack([-x.ravel(), grid.ravel(), z.ravel()], axis=1).astype(np.float32)
    u, v = np.meshgrid(rows / (res - 1), rows / (res - 1), indexing="xy")
    uv = np.stack([u.ravel(), v.ravel()], axis=1).astype(np.float32)
    r, c = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing="ij")
    a = (r * n + c).ravel()
    tris = np.concatenate([np.stack([a, a + n, a + 1], 1), np.stack([a + 1, a + n, a + n + 1], 1)])
    holes = hm.get("m_Holes") or []
    if len(holes) == (res - 1) ** 2:
        solid = np.asarray(holes, dtype=bool).reshape(res - 1, res - 1)[np.ix_(rows[:-1], rows[:-1])].ravel()
        if not solid.all() and solid.any():
            tris = np.concatenate([tris[:len(a)][solid], tris[len(a):][solid]])
    tris = orient_to_normals(points, tris, np.tile([0.0, 1.0, 0.0], (len(points), 1)))
    size = ((res - 1) * scale["x"], (res - 1) * scale["z"])
    return MeshData(points, [tris], uvs={"UV0": uv}, name=""), size


def terrain_texture(session, obj, tree, size):
    """Terrain layers tiled over the terrain and blended by the splat (alpha) maps."""
    from .unity import asset_image
    splat = tree.get("m_SplatDatabase") or {}
    layers = []  # (diffuse image, tile size x, tile size z)
    for ptr in splat.get("m_TerrainLayers") or []:
        reader = _deref(session, obj, ptr)
        if reader is None:
            layers.append(None)
            continue
        layer = reader.read_typetree()
        tex = _deref(session, reader, layer.get("m_DiffuseTexture"))
        tile = layer.get("m_TileSize") or {"x": 15, "y": 15}
        try:
            img = asset_image(tex.read()) if tex is not None else None
        except Exception:
            img = None
        layers.append((img, tile.get("x", 15), tile.get("y", 15)) if img is not None else None)
    if not layers:  # Unity 2018.2 and older: splat prototypes
        for proto in splat.get("m_Splats") or []:
            tex = _deref(session, obj, proto.get("texture"))
            tile = proto.get("tileSize") or {"x": 15, "y": 15}
            try:
                layers.append((asset_image(tex.read()), tile["x"], tile["y"]) if tex is not None else None)
            except Exception:
                layers.append(None)
    alphas = []
    for ptr in splat.get("m_AlphaTextures") or []:
        reader = _deref(session, obj, ptr)
        try:
            alphas.append(asset_image(reader.read()).convert("RGBA") if reader is not None else None)
        except Exception:
            alphas.append(None)
    side = min(MAX_TEXTURE, max([a.width for a in alphas if a is not None] or [512]) * 2)
    total = np.zeros((side, side, 3), np.float32)
    weight_sum = np.zeros((side, side, 1), np.float32)
    for i, layer in enumerate(layers):
        alpha = alphas[i // 4] if i // 4 < len(alphas) else None
        if alpha is None:
            continue
        # One channel at a time: resizing RGBA would premultiply by alpha (the 4th layer's weight).
        channel = alpha.getchannel(i % 4).resize((side, side), Image.BILINEAR)
        weight = np.asarray(channel, np.float32)[..., None] / 255.0
        if layer is None:  # layer or its texture not in the build: a plain color per layer
            color = np.full((side, side, 3), FALLBACK_COLORS[i % len(FALLBACK_COLORS)], np.float32)
        else:
            img, tile_x, tile_z = layer
            repeats_x = max(size[0] / max(tile_x, 0.01), 1.0)
            repeats_z = max(size[1] / max(tile_z, 0.01), 1.0)
            tw = max(4, round(side / repeats_x))
            th = max(4, round(side / repeats_z))
            tile = np.asarray(img.convert("RGB").resize((tw, th), Image.BILINEAR), np.float32)
            color = np.tile(tile, (math.ceil(side / th), math.ceil(side / tw), 1))[:side, :side]
        total += color * weight
        weight_sum += weight
    if not weight_sum.any():
        return None
    blended = np.where(weight_sum > 1e-3, total / np.maximum(weight_sum, 1e-3), 128.0)
    return Image.fromarray(np.clip(blended, 0, 255).astype(np.uint8), "RGB")


# --------------------------------------------------------------------------- export (rebuilt by the Unity editor)

def _color(c, default=(1.0, 1.0, 1.0, 1.0)):
    return [float(c.get(k, d)) for k, d in zip("rgba", default)] if isinstance(c, dict) else [float(d) for d in default]


def _vec(v, keys="xy", default=0.0):
    return [float(v.get(k, default)) for k in keys] if isinstance(v, dict) else [default] * len(keys)


def _rgba32(c):
    """ColorRGBA32 ({"rgba": packed}) -> the packed uint32 (r in the low byte)."""
    return int(c.get("rgba", 0xFFFFFFFF)) & 0xFFFFFFFF if isinstance(c, dict) else 0xFFFFFFFF


def _alphamaps(session, obj, splat, count):
    """(layers, res, res) uint8 splat weights, row 0 = the terrain's z = 0 edge (like TerrainData.GetAlphamaps)."""
    from .unity import asset_image
    res = int(splat.get("m_AlphamapResolution") or 0)
    textures = []
    for ptr in splat.get("m_AlphaTextures") or []:
        reader = _deref(session, obj, ptr)
        try:
            img = asset_image(reader.read()).convert("RGBA") if reader is not None else None
        except Exception:
            img = None
        textures.append(img)
    if not res:
        res = max([t.width for t in textures if t is not None] or [0])
    if not res or not count or not any(t is not None for t in textures):
        return None
    out = np.zeros((count, res, res), np.uint8)
    for i in range(count):
        img = textures[i // 4] if i // 4 < len(textures) else None
        if img is None:
            continue
        if img.size != (res, res):
            img = img.resize((res, res), Image.BILINEAR)
        out[i] = np.flipud(np.asarray(img.getchannel(i % 4), np.uint8))  # images are top row first
    return out


def _details(dd, layers):
    """(layers, res, res) uint8 detail map (counts, or coverage 0-255 in coverage mode), row = z, or None.
    Patches are m_PatchCount x m_PatchCount, each m_PatchSamples square, one block per layer it uses."""
    count, samples = int(dd.get("m_PatchCount") or 0), int(dd.get("m_PatchSamples") or 0)
    patches = dd.get("m_Patches") or []
    if not layers or not count or not samples or len(patches) != count * count:
        return None
    res = count * samples
    out = np.zeros((layers, res, res), np.uint8)
    block = samples * samples
    for index, patch in enumerate(patches):
        values = patch.get("numberOfObjects") or patch.get("coverage") or []
        py, px = divmod(index, count)
        for k, layer in enumerate(patch.get("layerIndices") or []):
            chunk = values[k * block:(k + 1) * block]
            if 0 <= layer < layers and len(chunk) == block:
                out[layer, py * samples:(py + 1) * samples, px * samples:(px + 1) * samples] = \
                    np.asarray(chunk, np.uint8).reshape(samples, samples)
    return out if out.any() else None


def _blank_layer(uid, name):
    return {"uid": uid, "name": name, "diffuse": "", "normal": "", "mask": "", "tileSize": [15.0, 15.0],
            "tileOffset": [0.0, 0.0], "specular": [0.0] * 4, "metallic": 0.0, "smoothness": 0.0, "normalScale": 1.0,
            "diffuseRemapMin": [0.0] * 4, "diffuseRemapMax": [1.0] * 4,
            "maskRemapMin": [0.0] * 4, "maskRemapMax": [1.0] * 4}


def terrain_export(session, obj):
    """Everything a Unity editor needs to rebuild a TerrainData: {"name", "resolution", "size", "heights"
    ((res, res) uint16, row = z), "holes" ((res-1, res-1) bool, True = solid, or None), "alphamaps",
    "layers", "trees", "tree_instances", "details", "detail_map", ...}. Textures are asset uids,
    prefabs "go:<file>:<id>" keys."""
    tree = obj.read_typetree()
    hm = tree["m_Heightmap"]
    res = int(hm.get("m_Resolution") or hm.get("m_Width"))
    scale = hm["m_Scale"]
    heights = np.asarray(hm["m_Heights"], dtype=np.int32).reshape(res, res).clip(0, 65535).astype(np.uint16)
    holes = hm.get("m_Holes") or []
    holes = np.asarray(holes, bool).reshape(res - 1, res - 1) if len(holes) == (res - 1) ** 2 else None
    if holes is not None and holes.all():
        holes = None

    def texture(owner, ptr):
        reader = _deref(session, owner, ptr)
        return session._asset_for(reader, "Texture2D").uid if reader is not None else ""

    def prefab(ptr):
        reader = _deref(session, obj, ptr)
        return f"go:{reader.assets_file.name}:{reader.path_id}" if reader is not None else ""

    splat = tree.get("m_SplatDatabase") or {}
    layers = []
    for i, ptr in enumerate(splat.get("m_TerrainLayers") or []):
        reader = _deref(session, obj, ptr)
        if reader is None:  # not in the build: keep the slot so the splat channels still line up
            layers.append(_blank_layer(f"terrainlayer:{obj.assets_file.name}:{obj.path_id}:{i}", f"Missing layer {i}"))
            continue
        t = reader.read_typetree()
        layers.append({
            **_blank_layer(f"terrainlayer:{reader.assets_file.name}:{reader.path_id}", t.get("m_Name") or f"Layer {i}"),
            "diffuse": texture(reader, t.get("m_DiffuseTexture")), "normal": texture(reader, t.get("m_NormalMapTexture")),
            "mask": texture(reader, t.get("m_MaskMapTexture")),
            "tileSize": _vec(t.get("m_TileSize"), default=15.0), "tileOffset": _vec(t.get("m_TileOffset")),
            "specular": _color(t.get("m_Specular"), (0, 0, 0, 0)), "metallic": float(t.get("m_Metallic", 0.0)),
            "smoothness": float(t.get("m_Smoothness", 0.0)), "normalScale": float(t.get("m_NormalScale", 1.0)),
            "diffuseRemapMin": _color(t.get("m_DiffuseRemapMin"), (0, 0, 0, 0)),
            "diffuseRemapMax": _color(t.get("m_DiffuseRemapMax")),
            "maskRemapMin": _color(t.get("m_MaskMapRemapMin"), (0, 0, 0, 0)),
            "maskRemapMax": _color(t.get("m_MaskMapRemapMax"))})
    if not layers:  # Unity 2018.2 and older: splat prototypes
        for i, p in enumerate(splat.get("m_Splats") or []):
            spec = _color(p.get("specularMetallic"), (0, 0, 0, 0))
            layers.append({
                **_blank_layer(f"terrainlayer:{obj.assets_file.name}:{obj.path_id}:{i}", f"Layer {i}"),
                "diffuse": texture(obj, p.get("texture")), "normal": texture(obj, p.get("normalMap")),
                "tileSize": _vec(p.get("tileSize"), default=15.0), "tileOffset": _vec(p.get("tileOffset")),
                "specular": spec[:3] + [1.0], "metallic": spec[3], "smoothness": float(p.get("smoothness", 0.0))})

    dd = tree.get("m_DetailDatabase") or {}
    trees = [{"prefab": prefab(p.get("prefab")), "bendFactor": float(p.get("bendFactor", 0.0)),
              "navMeshLod": int(p.get("navMeshLod", 2147483647))} for p in dd.get("m_TreePrototypes") or []]
    instances = [(*_vec(t.get("position"), "xyz"), float(t.get("widthScale", 1.0)), float(t.get("heightScale", 1.0)),
                  float(t.get("rotation", 0.0)), _rgba32(t.get("color")), _rgba32(t.get("lightmapColor")),
                  int(t.get("index", 0))) for t in dd.get("m_TreeInstances") or []]
    details = [{
        "prefab": prefab(p.get("prototype")), "texture": texture(obj, p.get("prototypeTexture")),
        "minWidth": float(p.get("minWidth", 1.0)), "maxWidth": float(p.get("maxWidth", 2.0)),
        "minHeight": float(p.get("minHeight", 1.0)), "maxHeight": float(p.get("maxHeight", 2.0)),
        "noiseSeed": int(p.get("noiseSeed", 0)), "noiseSpread": float(p.get("noiseSpread", 0.1)),
        "holeEdgePadding": float(p.get("holeTestRadius", 0.0)), "density": float(p.get("density", 1.0)),
        "healthyColor": _color(p.get("healthyColor")), "dryColor": _color(p.get("dryColor")),
        "renderMode": int(p.get("renderMode", 2)), "usePrototypeMesh": bool(p.get("usePrototypeMesh", 0)),
        "useInstancing": bool(p.get("useInstancing", 0)), "useDensityScaling": bool(p.get("useDensityScaling", 0)),
        "alignToGround": float(p.get("alignToGround", 0.0)), "positionJitter": float(p.get("positionJitter", 0.0)),
        "targetCoverage": float(p.get("targetCoverage", 1.0))} for p in dd.get("m_DetailPrototypes") or []]
    return {
        "name": tree.get("m_Name") or "Terrain", "resolution": res,
        "size": [(res - 1) * float(scale["x"]), float(scale["y"]), (res - 1) * float(scale["z"])],
        "heights": heights, "holes": holes,
        "alphamaps": _alphamaps(session, obj, splat, len(layers)),
        "baseMapResolution": int(splat.get("m_BaseMapResolution") or 1024), "layers": layers,
        "trees": trees, "tree_instances": instances,
        "details": details, "detail_map": _details(dd, len(details)),
        "detailPatch": int(dd.get("m_PatchSamples") or 8), "coverage": int(dd.get("m_DetailScatterMode", 0)) == 1,
        "grassTint": _color(dd.get("WavingGrassTint")),
        "grass": [float(dd.get(k, d)) for k, d in (("m_WavingGrassStrength", 0.5), ("m_WavingGrassAmount", 0.5),
                                                   ("m_WavingGrassSpeed", 0.5))]}
