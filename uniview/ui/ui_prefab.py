"""UI prefabs drawn as 2D pictures - shared by the prefab view and the grid thumbnails."""

import weakref

from engines.sdk import THUMB_KINDS, VIEW_OPTIONS
from uniview import prefab_info, ui_canvas, ui_text
from uniview.constants import log
from uniview.ui import canvas_render
from uniview.unity_project import is_prefab

_uid_maps = weakref.WeakKeyDictionary()  # session -> (asset count, {Asset.uid: Asset})


def uid_map(session):
    """{Asset.uid: Asset} of a session (cached; rebuilt if its asset list changed)."""
    cached = _uid_maps.get(session)
    if cached is None or cached[0] != len(session.assets):
        cached = (len(session.assets), {a.uid: a for a in session.assets})
        _uid_maps[session] = cached
    return cached[1]


def has_thumbnail(asset):
    """Assets the grid makes thumbnails for: models, textures, sprites and prefabs (not whole scenes)."""
    return asset.kind in THUMB_KINDS or is_prefab(asset)


def ui_text_font(session, asset):
    """What a UI text draws with (see canvas_render.render): a TextMeshPro font asset, else font file bytes."""
    if asset is None:
        return None
    if asset.kind == "font":
        return session.raw(asset)
    if asset.kind == "data" and hasattr(session, "tmp_font"):
        font = session.tmp_font(asset)
        if font is not None and not ui_text.has_glyphs(font) and font.get("font_file") is not None:
            return session.raw(font["font_file"])  # filled in while the game runs: use the font it's made from
        return font
    return None


def ui_picture(session, nodes, by_uid=None, max_side=canvas_render.MAX_SIDE):
    """(QImage, {node: canvas corners}, QTransform canvas -> image) of a UI prefab's hierarchy() nodes, or
    None when nothing shows. Call with the session lock held."""
    by_uid = by_uid if by_uid is not None else uid_map(session)
    visible = prefab_info.visible_flags(nodes) if VIEW_OPTIONS.get("hide_inactive", True) else None
    screen = ui_canvas.screen_size(nodes)
    boxes = {}
    items = ui_canvas.layout(nodes, visible, screen, boxes)

    def load(uid):
        asset = by_uid.get(uid)
        return session.image(asset) if asset is not None else None

    def sprite_info(uid):
        asset = by_uid.get(uid)
        return session.sprite_info(asset) if asset is not None and hasattr(session, "sprite_info") else None

    try:
        img, to_image = canvas_render.render(
            items, load, sprite_info, screen, max_side=max_side,
            text_font=lambda uid: ui_text_font(session, by_uid.get(uid)),
            material=session.material_details if hasattr(session, "material_details") else None)
    except Exception:
        log.exception("Drawing the UI prefab failed")
        return None
    if img is None:
        return None
    return img, boxes, to_image


def ui_thumbnail(session, asset, size):
    """PIL image of a UI prefab for the grid, or None if it isn't one (or shows nothing)."""
    with session.lock:
        if not (hasattr(session, "is_ui") and session.is_ui(asset)):
            return None
        picture = ui_picture(session, session.hierarchy(asset), max_side=max(size * 2, 64))
    return canvas_render.to_pil(picture[0]) if picture is not None else None
