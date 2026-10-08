"""What the 3D view shows for a model: textured parts, the texture images, the info rows. No Qt here."""

from html import escape as html_escape

import numpy as np

from uniview.constants import log
from uniview import pbr
from uniview.imaging import shrink
from uniview.export import material_for


def texture_groups(md, materials):
    """Submeshes grouped by what they show: [(texture Asset or None, (r, g, b) or None, triangles, alpha,
    effect, uv)]. The color is the plain material color, or for a texture the tint it's multiplied with (None =
    white). alpha: None (opaque), ("mask", cutoff), ("blend", opacity) or ("add", opacity) - see
    Material.alpha_mode. effect: still particle stand-ins (MeshData.effect_mask), kept apart so playback can hide
    them. uv: the texture's (scale, offset) tiling when it isn't 1/0, else None. lightmap: (texture Asset, mode)
    of the baked light multiplied on in a scene (Material.lightmap), else None.

    One entry per texture (or plain material color), so each can be drawn as one part."""
    groups = {}  # (texture key, color, alpha, effect) -> (texture Asset, color, [triangle arrays], alpha, effect)
    effect_mask = getattr(md, "effect_mask", None)
    if materials:
        for j, tris in enumerate(md.submeshes):
            mat = material_for(materials, md, j)
            main, color, alpha, uv = material_look(mat)
            lm = getattr(mat, "lightmap", None)
            lightmap = (lm, mat.lightmap_mode) if lm is not None else None
            if effect_mask is not None:
                fx = effect_mask[tris[:, 0]]
                pieces = [(tris[~fx], False), (tris[fx], True)]
            else:
                pieces = [(tris, False)]
            for piece, effect in pieces:
                if not len(piece):
                    continue
                key = (main.key if main is not None else None, color, alpha, effect, uv,
                       (lightmap[0].key, lightmap[1]) if lightmap else None)
                groups.setdefault(key, (main, color, [], alpha, effect, uv, lightmap))[2].append(piece)
    return [(tex_asset, color, np.concatenate(tris_list) if len(tris_list) > 1 else tris_list[0], alpha, effect, uv,
             lightmap) for tex_asset, color, tris_list, alpha, effect, uv, lightmap in groups.values()]


# ---------------------------------------------------------------------------- skyboxes
# Cube faces in Unity's order (+X, -X, +Y, -Y, +Z, -Z), each image's first row on top, following the usual
# cube map rule: a face pixel at s, t (-1..1, left to right / top to bottom) looks along these directions.
_FACE_DIRS = (
    lambda s, t: (np.ones_like(s), -t, -s), lambda s, t: (-np.ones_like(s), -t, s),
    lambda s, t: (s, np.ones_like(s), t), lambda s, t: (s, -np.ones_like(s), -t),
    lambda s, t: (s, -t, np.ones_like(s)), lambda s, t: (-s, -t, -np.ones_like(s)))


def _face_grid(size):
    c = (np.arange(size) + 0.5) / size * 2 - 1
    return np.meshgrid(c, c)  # s (columns), t (rows)


def _rotate_y(x, y, z, degrees):
    """Unity skybox _Rotation: the sky turned around the vertical axis (the shader rotates the view direction)."""
    a = np.radians(degrees)
    return np.cos(a) * x - np.sin(a) * z, y, np.sin(a) * x + np.cos(a) * z


def _cube_lookup(faces, x, y, z):
    """Colors of Unity-order cube faces (arrays) along directions x, y, z."""
    ax, ay, az = np.abs(x), np.abs(y), np.abs(z)
    out = np.zeros(x.shape + (faces[0].shape[2],), faces[0].dtype)
    n = faces[0].shape[0]
    for face, (pick, s, t) in enumerate((
            ((ax >= ay) & (ax >= az) & (x > 0), -z / ax, -y / ax), ((ax >= ay) & (ax >= az) & (x <= 0), z / ax, -y / ax),
            ((ay > ax) & (ay >= az) & (y > 0), x / ay, z / ay), ((ay > ax) & (ay >= az) & (y <= 0), x / ay, -z / ay),
            ((az > ax) & (az > ay) & (z > 0), x / az, -y / az), ((az > ax) & (az > ay) & (z <= 0), -x / az, -y / az))):
        if pick.any():
            col = np.clip(((s[pick] + 1) / 2 * n).astype(int), 0, n - 1)
            row = np.clip(((t[pick] + 1) / 2 * n).astype(int), 0, n - 1)
            out[pick] = faces[face][row, col]
    return out


def _gradient_rgb(h, stops):
    """Colours (0-255) at heights h (-1 down .. 1 up) of a sky given as [(height, rgb 0-1)] stops."""
    stops = sorted(stops, key=lambda st: st[0])
    at = np.array([float(st[0]) for st in stops])
    rgb = np.array([st[1][:3] for st in stops], np.float32) * 255
    return np.stack([np.interp(h, at, rgb[:, k]) for k in range(3)], axis=-1)


def gradient_clouds(faces, sky, cloud_alpha=1.0, stops=None):
    """Unity-order faces of a gradient sky ((top, horizon, bottom) colours by height, or `stops`: [(height -1..1,
    rgb)]) with a cloud cubemap blended over it by the clouds' alpha (else brightness)."""
    if stops is None:
        top, horizon, bottom = sky
        stops = [(-1.0, bottom), (0.0, horizon), (1.0, top)]
    out = []
    for i, face in enumerate(faces):
        n = face.shape[0]
        s, t = _face_grid(n)
        x, y, z = _FACE_DIRS[i](s, t)
        h = y / np.sqrt(x * x + y * y + z * z)  # -1 down .. 1 up
        sky_rgb = _gradient_rgb(h, stops)
        cloud = face.astype(np.float32)
        a = (cloud[..., 3:4] if cloud.shape[2] == 4 else cloud[..., :3].max(axis=2, keepdims=True)) / 255 * cloud_alpha
        out.append(np.clip(sky_rgb * (1 - a) + cloud[..., :3] * a, 0, 255).astype(np.uint8))
    return out


def gradient_faces(stops, size=64):
    """Unity-order faces of a plain gradient sky ([(height -1..1, rgb)] stops) - no clouds."""
    return gradient_clouds([np.zeros((size, size, 4), np.uint8)] * 6, None, 0.0, stops)


def sky_faces(kind, images, rotation=0.0, size=512):
    """A skybox as 6 Unity-order cube faces (uint8 RGB arrays): "cube"/"six" faces as they are (resampled when
    the sky is rotated), "pano" (an equirectangular image) sampled the way Unity's Panoramic skybox reads it."""
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind in ("cube", "six"):
            faces = [np.asarray(img.convert("RGB")) for img in images]
            if kind == "six":
                n = max(f.shape[0] for f in faces)
                faces = [np.asarray(img.convert("RGB").resize((n, n))) for img in images]
            if not rotation:
                return faces
            size = faces[0].shape[0]
        else:
            pano = np.asarray(images[0].convert("RGB"))
        s, t = _face_grid(size)
        out = []
        for face_dir in _FACE_DIRS:
            x, y, z = _rotate_y(*face_dir(s, t), rotation)
            if kind == "pano":
                length = np.sqrt(x * x + y * y + z * z)
                u = 0.5 - np.arctan2(z, x) / (2 * np.pi)                 # Unity's ToRadialCoords
                v = np.arccos(np.clip(y / length, -1, 1)) / np.pi        # 0 = top row
                h, w = pano.shape[:2]
                out.append(pano[np.clip((v * h).astype(int), 0, h - 1), np.clip((u * w).astype(int) % w, 0, w - 1)])
            else:
                out.append(_cube_lookup(faces, x, y, z))
        return out


# UniView space is Unity mirrored on x, and VTK reads cube maps with z the other way: together a half turn around
# y, so VTK cube slot i shows Unity face VTK_CUBE_SLOTS[i]; the up/down faces turn half way round with it.
VTK_CUBE_SLOTS = (1, 0, 2, 3, 5, 4)


def vtk_cube_faces(faces):
    """Unity-order faces -> the 6 images for a VTK cube map texture (slot order, first row on top)."""
    return [np.ascontiguousarray(faces[f][::-1, ::-1] if f in (2, 3) else faces[f]) for f in VTK_CUBE_SLOTS]


def fog_shader(fog):
    """GLSL to splice into a VTK fragment shader (after //VTK::Light::Impl) that blends towards the scene's fog
    colour with distance, like Unity's fog modes. The view depth is 1 / gl_FragCoord.w (perspective views)."""
    mode = int(fog.get("mode", 3))
    if mode == 1:
        span = max(fog.get("end", 300.0) - fog.get("start", 0.0), 1e-3)
        visible = f"clamp(({fog.get('end', 300.0):.6f} - d) / {span:.6f}, 0.0, 1.0)"
    elif mode == 2:
        visible = f"exp(-{fog.get('density', 0.01):.8f} * d)"
    else:
        visible = f"exp(-pow({fog.get('density', 0.01):.8f} * d, 2.0))"
    r, g, b = (min(1.0, max(0.0, float(c))) for c in fog.get("color", (0.5, 0.5, 0.5))[:3])
    return ("//VTK::Light::Impl\n  {\n    float d = 1.0 / gl_FragCoord.w;\n"
            f"    gl_FragData[0].rgb = mix(vec3({r:.5f}, {g:.5f}, {b:.5f}), gl_FragData[0].rgb, {visible});\n"
            "  }\n")


def lightmap_image(img, mode):
    """A decoded lightmap as an 8-bit RGB multiplier for display (sRGB textures x this): HDR lightmaps hold linear
    light (gamma-corrected here), RGBM is rgb x 5 x alpha, dLDR rgb x 2; values above 1 are clipped."""
    from PIL import Image
    arr = np.asarray(img.convert("RGBA"), np.float32) / 255.0
    rgb = arr[..., :3]
    if mode == "rgbm":
        rgb = rgb * arr[..., 3:4] * 5.0
    elif mode == "dldr":
        rgb = rgb * 2.0
    else:
        rgb = np.power(np.clip(rgb, 0.0, 1.0), 1 / 2.2)
    return Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255 + 0.5).astype(np.uint8))


def material_look(mat):
    """(texture Asset or None, color or None, alpha, (uv scale, uv offset) or None) of a material, as
    texture_groups describes them."""
    main = pbr.base_texture(mat)
    color = mat.color if mat is not None else None
    if main is not None and color is not None and (all(c >= 0.999 for c in color[:3]) or not any(color[:3])):
        color = None  # white tint changes nothing; all black is an unused color property
    alpha = material_alpha(mat, color)
    if color is not None:
        color = tuple(min(1.0, max(0.0, c)) for c in color[:3])
    uv = (main.uv_scale, main.uv_offset) if main is not None and getattr(main, "tiled", False) else None
    return (main.asset if main is not None else None), color, alpha, uv


def emitter_looks(md, materials, trails=False):
    """[(texture Asset or None, tint or None, alpha)] for each of md.emitters (particle playback); trails=True:
    the looks of their trail materials (None for emitters without trails)."""
    looks = []
    for e in getattr(md, "emitters", None) or []:
        index = getattr(e, "trail_material", None) if trails else e.material
        if trails and (index is None or getattr(e, "trails", None) is None):
            looks.append(None)
            continue
        mat = materials[index] if materials and index is not None and index < len(materials) else None
        looks.append(material_look(mat)[:3])
    return looks


def material_alpha(mat, color=None):
    """How a part with this material is see-through: None, ("mask", cutoff), ("blend", opacity) or
    ("add", opacity). A blended part takes its opacity from the color's alpha (times the texture's)."""
    mode = getattr(mat, "alpha_mode", "opaque") if mat is not None else "opaque"
    if mode == "mask":
        return "mask", round(float(mat.alpha_cutoff), 3)
    if mode in ("blend", "add"):
        opacity = float(color[3]) if color is not None and len(color) > 3 else 1.0
        return mode, round(min(1.0, max(0.05, opacity)), 3)
    return None


def texture_loader(session, n_textures):
    """image_of(texture Asset) -> PIL image or None, cached. Big scenes (maps) use hundreds of
    textures, so they're loaded smaller to keep memory in check."""
    max_side = 256 if n_textures > 32 else 1024
    images = {}  # texture asset key -> display image (or None if it failed)

    def image_of(tex_asset):
        if tex_asset is None:
            return None
        if tex_asset.key not in images:
            try:
                img = shrink(session.image(tex_asset), max_side)  # keeps the colour under alpha 0
                images[tex_asset.key] = img
            except Exception as e:
                log.debug("Texture '%s' failed: %s", tex_asset.name, e)
                images[tex_asset.key] = None
        return images[tex_asset.key]

    return image_of


def is_place(asset):
    """Scenes, maps and terrains are walked through (fly camera); single models are orbited."""
    return (asset.kind == "scene" or asset.name.startswith("Terrain:")
            or (isinstance(asset.ref, str) and asset.ref.lower().endswith(".bsp")))


def model_info_rows(session, asset, md, n_points, n_cells, uv_channels, favorite=False):
    """HTML lines for the model info panel."""
    rows = [
        f"<b>Vertices:</b> {n_points:,}",
        f"<b>Triangles:</b> {n_cells:,}",
        f"<b>Submeshes:</b> {len(md.submeshes)}",
        f"<b>UV sets:</b> {', '.join(uv_channels) if uv_channels else 'none'}",
    ]
    try:
        rows += [f"<b>{html_escape(label)}:</b> {html_escape(value)}" for label, value in session.describe(asset)]
    except Exception:
        log.exception("describe() failed for '%s'", asset.name)
    if favorite:
        rows.insert(0, "<span style='color:#f4c542'>★ Favorite</span>")
    return rows


LAYER_STEP = 5e-4        # depth between stacked pieces of a flat mesh, x its size (beats the depth buffer)
MAX_LAYERS = 400


def layer_offsets(points, triangles):
    """Flat meshes built from stacked pieces (2D sprite characters: a quad per body part, all at z = 0) are drawn
    by Unity in triangle order with no depth test, later pieces on top. Drawn with depth, overlapping pieces
    z-fight - worst while an animation swings an arm across the body. Returns (N, 3) offsets that move each
    connected piece towards the viewer (Unity's -z for meshes in the xy plane) by its draw order, or None for
    meshes that aren't flat or are one piece."""
    pts = np.asarray(points, float)
    tris = np.asarray(triangles, np.int64).reshape(-1, 3)
    if len(pts) < 4 or len(tris) < 2:
        return None
    extent = pts.max(0) - pts.min(0)
    size = float(extent.max())
    axis = int(np.argmin(extent))
    if size <= 0 or extent[axis] > 0.01 * size:
        return None
    parent = np.arange(len(pts))  # connected pieces: union-find over each triangle's vertices

    def find(i):
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root
    for a, b, c in tris:
        ra, rb, rc = find(a), find(b), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    roots = np.array([find(i) for i in range(len(pts))])
    first = {}
    for t, (a, _b, _c) in enumerate(tris):
        first.setdefault(roots[a], t)  # a piece is drawn when its first triangle is
    if len(first) < 2 or len(first) > MAX_LAYERS:
        return None
    order = {root: rank for rank, (root, _t) in enumerate(sorted(first.items(), key=lambda kv: kv[1]))}
    rank = np.array([order.get(r, 0) for r in roots], float)
    out = np.zeros_like(pts)
    out[:, axis] = -rank * LAYER_STEP * size
    return out.astype(np.float32)
