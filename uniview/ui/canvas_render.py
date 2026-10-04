"""Paints uniview.ui_canvas draw items into a QImage: sprites (simple, sliced, tiled, filled), raw images, text."""

import numpy as np
from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPainterPath, QPolygonF, QTransform

from uniview import ui_canvas, ui_text

MAX_SIDE = 2048
MAX_TILES = 2000
# Radial fill start angles (Qt degrees, counter-clockwise from 3 o'clock) for a clockwise fill, per m_FillOrigin.
RADIAL_START = {2: {0: 90, 1: 0, 2: -90, 3: 180},      # Radial90: from the corner (bottom-left, top-left...)
                3: {0: 180, 1: 90, 2: 0, 3: -90},      # Radial180: from the edge (bottom, left, top, right)
                4: {0: -90, 1: 0, 2: 90, 3: 180}}      # Radial360: bottom, right, top, left
RADIAL_SWEEP = {2: 90, 3: 180, 4: 360}


def qtransform(m):
    """QTransform of a 3x3 column-vector affine matrix."""
    return QTransform(m[0, 0], m[1, 0], m[0, 1], m[1, 1], m[0, 2], m[1, 2])


def tinted(img, color, alpha):
    """QImage of a PIL image multiplied by an RGBA color (and extra alpha)."""
    a = np.asarray(img.convert("RGBA"), np.float32)
    a *= np.array([color[0], color[1], color[2], color[3] * alpha], np.float32)
    a = np.clip(a, 0, 255).astype(np.uint8)
    h, w = a.shape[:2]
    return QImage(a.tobytes(), w, h, 4 * w, QImage.Format_RGBA8888).copy()


def _fill_clip(item, rect):
    """Clip path (flipped local coords, y down) for a partly filled Image, or None when fully filled."""
    amount = max(0.0, min(1.0, item["fill_amount"]))
    if item["type"] != 3 or amount >= 1.0:
        return None
    x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    method, origin, path = item["fill_method"], item["fill_origin"], QPainterPath()
    if method == 0:  # horizontal: from the left (0) or right (1)
        path.addRect(QRectF(x0 if origin == 0 else x0 + w * (1 - amount), y0, w * amount, h))
    elif method == 1:  # vertical: from the bottom (0) or top (1)
        path.addRect(QRectF(x0, y0 + h * (1 - amount) if origin == 0 else y0, w, h * amount))
    else:
        if method == 2:
            center = {0: (x0, y0 + h), 1: (x0, y0), 2: (x0 + w, y0), 3: (x0 + w, y0 + h)}.get(origin, (x0, y0 + h))
        elif method == 3:
            center = {0: (x0 + w / 2, y0 + h), 1: (x0, y0 + h / 2), 2: (x0 + w / 2, y0),
                      3: (x0 + w, y0 + h / 2)}.get(origin, (x0 + w / 2, y0 + h))
        else:
            center = (x0 + w / 2, y0 + h / 2)
        full = RADIAL_SWEEP.get(method, 360)
        start = RADIAL_START.get(method, RADIAL_START[4]).get(origin, 0)
        # Clockwise runs from start down to start - full; counter-clockwise from the other end back up.
        start, span = (start, -full * amount) if item["fill_clockwise"] else (start - full, full * amount)
        radius = 2 * (w + h)
        path.moveTo(*center)
        path.arcTo(QRectF(center[0] - radius, center[1] - radius, 2 * radius, 2 * radius), start, span)
        path.closeSubpath()
    return path


def trimmed_rect(rect, info):
    """Where the (trimmed) sprite image goes inside the full sprite rect drawn at rect (y down)."""
    size, trim = info.get("size"), info.get("trim")
    if not size or not trim or size[0] <= 0 or size[1] <= 0 or (trim[0], trim[1], trim[2], trim[3]) == (
            0, 0, size[0], size[1]):
        return rect
    sx, sy = rect.width() / size[0], rect.height() / size[1]
    return QRectF(rect.x() + trim[0] * sx, rect.y() + (size[1] - trim[1] - trim[3]) * sy, trim[2] * sx, trim[3] * sy)


def padded(img, info):
    """The trimmed sprite image put back into its full rect (transparent around it)."""
    size, trim = info.get("size"), info.get("trim")
    if not size or not trim or (round(trim[2]), round(trim[3])) == (round(size[0]), round(size[1])):
        return img
    w, h = max(1, round(size[0])), max(1, round(size[1]))
    if w * h > 4096 * 4096:
        return img
    out = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.drawImage(QRectF(trim[0], h - trim[1] - trim[3], trim[2], trim[3]), img)
    p.end()
    return out


def _draw_sliced(painter, img, rect, borders_px, borders):
    """9-slice: corners keep their size, edges and centre stretch."""
    iw, ih = img.width(), img.height()
    l_px, b_px, r_px, t_px = borders_px
    left, bottom, right, top = borders
    xs_dst = [rect.x(), rect.x() + left, rect.right() - right, rect.right()]
    ys_dst = [rect.y(), rect.y() + top, rect.bottom() - bottom, rect.bottom()]
    xs_src = [0, l_px, iw - r_px, iw]
    ys_src = [0, t_px, ih - b_px, ih]
    for row in range(3):
        for col in range(3):
            dst = QRectF(xs_dst[col], ys_dst[row], xs_dst[col + 1] - xs_dst[col], ys_dst[row + 1] - ys_dst[row])
            src = QRectF(xs_src[col], ys_src[row], xs_src[col + 1] - xs_src[col], ys_src[row + 1] - ys_src[row])
            if dst.width() > 0 and dst.height() > 0 and src.width() > 0 and src.height() > 0:
                painter.drawImage(dst, img, src)


def _draw_tiled(painter, img, rect, tile_w, tile_h):
    if tile_w <= 0 or tile_h <= 0:
        painter.drawImage(rect, img)
        return
    nx = int(np.ceil(rect.width() / tile_w))
    ny = int(np.ceil(rect.height() / tile_h))
    if nx * ny > MAX_TILES:
        painter.drawImage(rect, img)
        return
    painter.save()
    painter.setClipRect(rect, Qt.IntersectClip)
    for j in range(ny):
        for i in range(nx):
            # Tiles start at the bottom-left like Unity's.
            painter.drawImage(QRectF(rect.x() + i * tile_w, rect.bottom() - (j + 1) * tile_h, tile_w, tile_h), img)
    painter.restore()


_families = {}  # hash of font file bytes -> Qt font family (registered once per run)


def font_family(data):
    """Qt family name of a TTF/OTF font file (registered with Qt the first time), or None."""
    key = hash(data)
    if key not in _families:
        fid = QFontDatabase.addApplicationFontFromData(data)
        families = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        _families[key] = families[0] if families else None
    return _families[key]


def sdf_coverage(field, color, alpha, edge, band, sdf):
    """QImage of (a piece of) a font atlas as tinted glyph coverage: a distance field (uint8 array) thresholded
    at edge with a soft band (in field units) for anti-aliasing, or a plain coverage atlas used as is."""
    d = field.astype(np.float32) / 255.0
    cov = np.clip((d - edge) / band + 0.5, 0.0, 1.0) if sdf else d
    a = np.empty(d.shape + (4,), np.float32)
    a[..., 0], a[..., 1], a[..., 2] = color[0], color[1], color[2]
    a[..., 3] = cov * color[3] * alpha
    a = (np.clip(a, 0, 1) * 255).astype(np.uint8)
    h, w = d.shape
    return QImage(a.tobytes(), w, h, 4 * w, QImage.Format_RGBA8888).copy()


def tmp_passes(item, material, gradient):
    """What a TextMeshPro SDF material draws, back to front: [(color, edge, extra softness, (dx, dy) offset in
    atlas pixels)] - underlay (drop shadow), outline, face. Edges are in distance-field units (0.5 = the glyph
    outline); material: {"floats", "colors", "keywords"} of the text's material, or None."""
    floats = (material or {}).get("floats") or {}
    colors = (material or {}).get("colors") or {}
    keywords = set((material or {}).get("keywords") or [])
    base = item["color"]

    def times(c):
        return tuple(a * b for a, b in zip(base, c))
    dilate = floats.get("_FaceDilate", 0.0) * 0.5
    width = max(floats.get("_OutlineWidth", 0.0), 0.0) * 0.5
    bold = 0.5 * 0.12 if item.get("bold") else 0.0
    face = 0.5 - dilate - bold
    passes = []
    if keywords & {"UNDERLAY_ON", "UNDERLAY_INNER"}:
        under = colors.get("_UnderlayColor", (0.0, 0.0, 0.0, 0.5))
        offset = (floats.get("_UnderlayOffsetX", 0.0) * gradient, floats.get("_UnderlayOffsetY", 0.0) * gradient)
        passes.append(((under[0], under[1], under[2], under[3] * base[3]),
                       0.5 - floats.get("_UnderlayDilate", 0.0) * 0.5 - width / 2 - bold,
                       floats.get("_UnderlaySoftness", 0.0) * 0.5, offset))
    if width > 0:
        passes.append((times(colors.get("_OutlineColor", (0.0, 0.0, 0.0, 1.0))), face - width / 2, 0.0, (0.0, 0.0)))
        face += width / 2
    passes.append((times(colors.get("_FaceColor", (1.0, 1.0, 1.0, 1.0))), face, 0.0, (0.0, 0.0)))
    return passes


def _draw_tmp(painter, item, rect, font, cache, material=None):
    """TextMeshPro text with the game's font atlas. False if the font can't draw it (nothing in its atlas)."""
    laid = ui_text.layout_text(item, font, rect.width(), rect.height())
    if not laid["glyphs"]:
        return not item["text"].strip()
    m = painter.transform()
    device = max(abs(m.m11() * m.m22() - m.m12() * m.m21()) ** 0.5, 1e-6)  # screen pixels per local unit
    shear = font["italic_style"] * 0.01 if item.get("italic") else 0.0
    gradient = ((material or {}).get("floats") or {}).get("_GradientScale") or (font["padding"] + 1) or 10.0
    for color, edge, soft, (ox, oy) in tmp_passes(item, material, gradient):
        _draw_glyphs(painter, item, rect, laid, cache, m, device, shear, color, edge, soft, ox, oy, gradient)
    return True


def _draw_glyphs(painter, item, rect, laid, cache, m, device, shear, color, edge, soft, ox, oy, gradient):
    """One pass of a TMP text: every glyph's distance field cut at edge, in color, moved by (ox, oy) atlas px."""
    for g in laid["glyphs"]:
        f = g["font"]
        atlas = f["images"][g["atlas"]]
        if id(atlas) not in cache:
            cache[id(atlas)] = (atlas, np.asarray(atlas, np.uint8))  # keep the image alive with its id
        field = cache[id(atlas)][1]
        sx, sy, sw, sh = (int(round(v)) for v in g["src"])
        x0, y0 = max(sx, 0), max(sy, 0)
        x1, y1 = min(sx + sw, field.shape[1]), min(sy + sh, field.shape[0])
        if x1 <= x0 or y1 <= y0:
            continue
        band = 1.0 / (2 * gradient * max(g["scale"] * device, 1e-3))  # one screen pixel of the field
        band = min(float(2 ** (round(np.log2(max(band, 1e-4)) * 2) / 2)), 1.0) + soft  # half-octave steps
        key = (id(atlas), x0, y0, x1, y1, color, item["alpha"], round(edge, 4), band)
        if key not in cache:
            cache[key] = sdf_coverage(field[y0:y1, x0:x1], color, item["alpha"], edge, band, f["sdf"])
        x, y, w, h = g["dst"]
        x, y = x + ox * g["scale"], y - oy * g["scale"]  # underlay offset: atlas px -> local units, y up
        # The part of the padded glyph rect that's inside the atlas.
        target = QRectF(rect.x() + x + (x0 - sx) / sw * w, rect.y() + y + (y0 - sy) / sh * h,
                        (x1 - x0) / sw * w, (y1 - y0) / sh * h)
        if shear:
            painter.save()
            base_y = rect.y() + g["baseline"]
            painter.setTransform(QTransform.fromTranslate(0, -base_y) * QTransform(1, 0, -shear, 1, 0, 0)
                                 * QTransform.fromTranslate(0, base_y) * m)
            painter.drawImage(target, cache[key])
            painter.restore()
        else:
            painter.drawImage(target, cache[key])


def _draw_text(painter, item, rect, family=None):
    font = QFont(family) if family else QFont()
    font.setPixelSize(max(1, round(item["size"])))
    font.setBold(item["bold"])
    font.setItalic(item["italic"])
    painter.setFont(font)
    c = item["color"]
    painter.setPen(QColor.fromRgbF(*(max(0.0, min(1.0, v)) for v in (c[0], c[1], c[2], c[3] * item["alpha"]))))
    hor, ver = item["align"]
    flags = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}[hor]
    flags |= {"top": Qt.AlignTop, "middle": Qt.AlignVCenter, "bottom": Qt.AlignBottom}[ver]
    if item["wrap"]:
        flags |= Qt.TextWordWrap
    painter.drawText(rect, int(flags), item["text"])


def render(items, load_image, sprite_info=None, screen=None, max_side=MAX_SIDE, text_font=None, material=None):
    """(QImage, QTransform canvas -> image pixels) of the draw items, or (None, None) when nothing shows.
    load_image(uid) -> PIL image or None; sprite_info(uid) -> {"border", "ppu"} or None; text_font(uid) -> a
    TMP font (engine tmp_font()), font file bytes, or None (a stand-in font is used); material(uid) ->
    {"floats", "colors", "keywords"} of a TMP text's material (outline, underlay...) or None."""
    sw, sh = screen or ui_canvas.SCREEN
    box = ui_canvas.bounds(items, clip_to=(-sw, -sh, sw, sh))
    if box is None:
        return None, None
    x0, y0, x1, y1 = box
    scale = min(max_side / max(x1 - x0, y1 - y0), 2.0)
    width, height = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
    base = QTransform(scale, 0, 0, -scale, -x0 * scale, y1 * scale)  # y up -> image rows down
    out = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    pils, tints, infos, fonts, glyph_cache, materials = {}, {}, {}, {}, {}, {}

    def material_of(uid):
        if uid not in materials:
            try:
                materials[uid] = material(uid) if material is not None and uid else None
            except Exception:
                materials[uid] = None
        return materials[uid]

    def font_of(uid):
        if uid not in fonts:
            try:
                fonts[uid] = text_font(uid) if text_font is not None and uid else None
            except Exception:
                fonts[uid] = None
        return fonts[uid]

    def pil(uid):
        if uid not in pils:
            try:
                pils[uid] = load_image(uid)
            except Exception:
                pils[uid] = None
        return pils[uid]

    def image(uid, color, alpha):
        key = (uid, tuple(round(v, 4) for v in color), round(alpha, 4))
        if key not in tints:
            src = pil(uid)
            tints[key] = tinted(src, color, alpha) if src is not None else None
        return tints[key]

    def draw(painter, item, color=None, offset=(0.0, 0.0)):
        """One item; color replaces its own (Shadow / Outline copies), offset moves it (local units, y up)."""
        if color is not None:
            item = {**item, "color": color}
        painter.save()
        if item["clip"]:
            painter.setTransform(base)
            path = None
            for pts in item["clip"]:
                p = QPainterPath()
                p.addPolygon(QPolygonF([QPointF(float(x), float(y)) for x, y in pts]))
                p.closeSubpath()
                path = p if path is None else path.intersected(p)
            painter.setClipPath(path)
        rx, ry, rw, rh = item["rect"]
        flip = QTransform(1, 0, 0, -1, 0, 2 * ry + rh)  # draw upright: local rect with y going down
        painter.setTransform(QTransform.fromTranslate(offset[0], -offset[1]) * flip * qtransform(item["matrix"]) * base)
        rect = QRectF(rx, ry, rw, rh)
        kind = item["kind"]
        if kind == "text":
            font = font_of(item.get("font"))
            if isinstance(font, dict) and ui_text.has_glyphs(font) and _draw_tmp(
                    painter, item, rect, font, glyph_cache, material_of(item.get("material"))):
                pass
            else:
                _draw_text(painter, item, rect, font_family(font) if isinstance(font, bytes) else None)
        else:
            uid = item.get("sprite") if kind == "image" else item.get("texture")
            img = image(uid, item["color"], item["alpha"]) if uid else None
            if img is None:
                c = item["color"]
                painter.fillRect(rect, QColor.fromRgbF(*(max(0.0, min(1.0, v)) for v in
                                                         (c[0], c[1], c[2], c[3] * item["alpha"]))))
            elif kind == "raw":
                u, v, uw, vh = item["uv"]
                iw, ih = img.width(), img.height()
                painter.drawImage(rect, img, QRectF(u * iw, (1 - v - vh) * ih, uw * iw, vh * ih))
            else:
                if uid not in infos:
                    try:
                        infos[uid] = sprite_info(uid) if sprite_info is not None else None
                    except Exception:
                        infos[uid] = None
                info = infos[uid] or {"border": [0, 0, 0, 0], "ppu": 100.0}
                size = info.get("size") or (img.width(), img.height())
                if item["preserve_aspect"] and item["type"] in (0, 3) and size[1] and rh:
                    aspect = size[0] / size[1]
                    if rw / rh > aspect:
                        rect = QRectF(rx + (rw - rh * aspect) / 2, ry, rh * aspect, rh)
                    else:
                        rect = QRectF(rx, ry + (rh - rw / aspect) / 2, rw, rw / aspect)
                clip = _fill_clip(item, rect)
                if clip is not None:
                    painter.setClipPath(clip, Qt.IntersectClip)
                if item["type"] == 1 and any(info["border"]):
                    borders = ui_canvas.slice_borders(rw, rh, info["border"], info["ppu"], item["ppu_multiplier"])
                    _draw_sliced(painter, padded(img, info), rect, info["border"], borders)
                elif item["type"] == 2:
                    img = padded(img, info)
                    ppu = info["ppu"] / 100.0 * item["ppu_multiplier"] or 1.0
                    _draw_tiled(painter, img, rect, img.width() / ppu, img.height() / ppu)
                else:
                    painter.drawImage(trimmed_rect(rect, info), img)
        painter.restore()

    def draw_with_effects(painter, item):
        for dx, dy, color, use_alpha in item.get("effects") or ():
            draw(painter, item, (color[0], color[1], color[2], color[3] * (item["color"][3] if use_alpha else 1.0)),
                 (dx, dy))
        draw(painter, item)

    # Masks (Mask, SoftMask) cut their children by the shape of their own graphic: the children are drawn on a
    # layer that the mask graphic's alpha is then applied to.
    mask_items = {it["mask_of"]: it for it in items if "mask_of" in it}
    mask_images = {}

    def mask_image(node):
        if node not in mask_images:
            img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            draw(p, {**mask_items[node], "color": (1.0, 1.0, 1.0, 1.0), "effects": []})
            p.end()
            if mask_items[node].get("mask_kind") == "UnityEngine.UI.Mask":
                # A Mask is a stencil: every pixel its graphic covers at all lets the children through fully.
                a = np.frombuffer(img.constBits(), np.uint8).reshape(height, img.bytesPerLine() // 4, 4)
                hard = np.where(a[:, :width, 3:4] > 2, 255, 0).astype(np.uint8)
                img = QImage(np.ascontiguousarray(np.broadcast_to(hard, (height, width, 4))).tobytes(), width,
                             height, 4 * width, QImage.Format_RGBA8888_Premultiplied).copy()
            mask_images[node] = img
        return mask_images[node]

    painter = QPainter(out)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
    current, layer, layer_painter = (), None, None

    def flush():
        if layer is None:
            return
        layer_painter.end()
        p = QPainter(layer)
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        for node in current:
            p.drawImage(0, 0, mask_image(node))
        p.end()
        painter.drawImage(0, 0, layer)

    for item in items:
        if item.get("mask_only"):
            continue
        masks = tuple(m for m in item.get("masks") or () if m in mask_items)
        if masks != current:
            flush()
            current, layer, layer_painter = masks, None, None
            if masks:
                layer = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
                layer.fill(Qt.transparent)
                layer_painter = QPainter(layer)
                layer_painter.setRenderHints(painter.renderHints())
        draw_with_effects(layer_painter or painter, item)
    flush()
    painter.end()
    # Rects are often much bigger than what's drawn in them (padded sprites): crop to the visible pixels.
    box = to_pil(out).getchannel("A").getbbox()
    if box is None:
        return out, base
    margin = 8
    x0, y0 = max(0, box[0] - margin), max(0, box[1] - margin)
    x1, y1 = min(width, box[2] + margin), min(height, box[3] + margin)
    return out.copy(x0, y0, x1 - x0, y1 - y0), base * QTransform.fromTranslate(-x0, -y0)


def to_pil(qimage):
    """PIL RGBA copy of a QImage."""
    img = qimage.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombytes("RGBA", (img.width(), img.height()), bytes(img.constBits()))
