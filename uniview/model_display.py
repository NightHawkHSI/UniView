"""What the 3D view shows for a model: textured parts, the texture images, the info rows. No Qt here."""

from html import escape as html_escape

import numpy as np

from uniview.constants import log
from uniview.export import material_for


def texture_groups(md, materials):
    """Submeshes grouped by what they show: [(texture Asset or None, (r, g, b) or None, triangles)].

    One entry per texture (or plain material color), so each can be drawn as one part."""
    groups = {}  # (texture key, color) -> (texture Asset, color, [triangle arrays])
    if materials:
        for j, tris in enumerate(md.submeshes):
            mat = material_for(materials, md, j)
            main = mat.main_texture() if mat is not None else None
            color = mat.color if (mat is not None and main is None and mat.color is not None) else None
            if color is not None:
                color = tuple(min(1.0, max(0.0, c)) for c in color[:3])
            key = (main.asset.key if main is not None else None, color)
            groups.setdefault(key, (main.asset if main is not None else None, color, []))[2].append(tris)
    return [(tex_asset, color, np.concatenate(tris_list) if len(tris_list) > 1 else tris_list[0])
            for tex_asset, color, tris_list in groups.values()]


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
                img = session.image(tex_asset)
                if max(img.size) > max_side:
                    img = img.copy()
                    img.thumbnail((max_side, max_side))
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
