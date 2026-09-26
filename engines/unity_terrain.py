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
