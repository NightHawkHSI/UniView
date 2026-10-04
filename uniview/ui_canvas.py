"""Unity UI (uGUI) prefabs as a flat list of things to draw: works out where every Image, RawImage and text
lands from the RectTransform layout, the way Unity does it. No Qt here - uniview/ui/canvas_render.py paints it.

Space: canvas units, y up, origin at the centre of the screen the prefab is placed on."""

import re

import numpy as np

from engines.unity_skin import trs

SCREEN = (1920.0, 1080.0)  # when the prefab has no CanvasScaler saying what it was made for
IMAGE = "UnityEngine.UI.Image"
RAW_IMAGE = "UnityEngine.UI.RawImage"
TEXT = "UnityEngine.UI.Text"
TMP_TEXTS = ("TMPro.TextMeshProUGUI", "TMPro.TextMeshPro")
MASKS = ("UnityEngine.UI.Mask", "UnityEngine.UI.RectMask2D", "Coffee.UISoftMask.SoftMask")
SHAPE_MASKS = ("UnityEngine.UI.Mask", "Coffee.UISoftMask.SoftMask")  # cut by their graphic's shape, not the rect
EFFECTS = ("UnityEngine.UI.Shadow", "UnityEngine.UI.Outline")
RICH_TAG = re.compile(r"<[^<>]{1,80}>")


def props_of(component):
    """{property path: flattened entry} of a hierarchy() component."""
    return {e["p"]: e for e in component.get("props") or []}


def _num(props, path, default=0.0):
    e = props.get(path)
    return e["v"] if e is not None and "v" in e else default


def _color(props, base, default=(1.0, 1.0, 1.0, 1.0)):
    if not any(f"{base}.{c}" in props for c in "rgba"):
        return default
    return tuple(float(_num(props, f"{base}.{c}", d)) for c, d in zip("rgba", default))


def _scripted(node):
    """[(class name, props)] of a node's components (scripts by their class, built-ins by their type)."""
    return [(c.get("script") or c["type"], props_of(c)) for c in node.get("components") or []]


def screen_size(nodes):
    """Reference resolution of the prefab's CanvasScaler, else 1920x1080."""
    for n in nodes:
        for cls, props in _scripted(n):
            if cls == "UnityEngine.UI.CanvasScaler":
                w, h = _num(props, "m_ReferenceResolution.x"), _num(props, "m_ReferenceResolution.y")
                if w > 0 and h > 0:
                    return float(w), float(h)
    return SCREEN


def affine(pos, rot, scale):
    """2D (3x3) local-to-parent matrix of a transform seen straight on (orthographic, looking down -z)."""
    m = trs((pos[0], pos[1], 0.0), rot, scale)
    return m[np.ix_([0, 1, 3], [0, 1, 3])]


def rect_layout(r, parent_rect):
    """(local position of the pivot in the parent, own rect (x0, y0, w, h) around the pivot) of a UI object's
    RectTransform values r (hierarchy() "rect"), given the parent's rect (x0, y0, w, h) in the parent's space."""
    px0, py0, pw, ph = parent_rect
    (amin_x, amin_y), (amax_x, amax_y) = r["anchor_min"], r["anchor_max"]
    piv_x, piv_y = r["pivot"]
    w = pw * (amax_x - amin_x) + r["size"][0]
    h = ph * (amax_y - amin_y) + r["size"][1]
    x = px0 + pw * (amin_x + (amax_x - amin_x) * piv_x) + r["pos"][0]
    y = py0 + ph * (amin_y + (amax_y - amin_y) * piv_y) + r["pos"][1]
    return (x, y), (-w * piv_x, -h * piv_y, w, h)


# ---------------------------------------------------------------------------- layout groups
# Layout groups place their children while the game runs; prefabs often save those children at size 0.

GRID = "UnityEngine.UI.GridLayoutGroup"
HORIZONTAL = "UnityEngine.UI.HorizontalLayoutGroup"
VERTICAL = "UnityEngine.UI.VerticalLayoutGroup"
FITTER = "UnityEngine.UI.ContentSizeFitter"
LAYOUT_ELEMENT = "UnityEngine.UI.LayoutElement"


def _padding(props):
    return tuple(float(_num(props, f"m_Padding.m_{k}", 0)) for k in ("Left", "Right", "Top", "Bottom"))


def _element(node):
    """Props of the node's enabled LayoutElement, or None."""
    return next((pr for cls, pr in _scripted(node) if cls == LAYOUT_ELEMENT and _num(pr, "m_Enabled", 1)), None)


def _layout_children(nodes, children):
    """Children a layout group arranges: active ones not marked LayoutElement.ignoreLayout."""
    out = []
    for j in children:
        if not nodes[j].get("active", True) or "rect" not in nodes[j]:
            continue
        element = _element(nodes[j])
        if element is not None and _num(element, "m_IgnoreLayout", 0):
            continue
        out.append(j)
    return out


def _preferred(node):
    """(width, height) a child asks a layout group for: LayoutElement preferred/min size, else its own size."""
    r = node["rect"]
    size = [r["size"][a] if r["anchor_min"][a] == r["anchor_max"][a] else 0.0 for a in (0, 1)]
    element = _element(node)
    if element is not None:
        for axis, key in ((0, "Width"), (1, "Height")):
            pref, low = _num(element, f"m_Preferred{key}", -1), _num(element, f"m_Min{key}", -1)
            if pref >= 0 or low >= 0:
                size[axis] = float(max(pref, low))
    return size


def _placed(child_rect, x, y, w, h):
    """Child RectTransform values putting it at (x, y) from the parent's top-left corner (y down), size w x h."""
    px, py = child_rect["pivot"]
    return {"anchor_min": [0.0, 1.0], "anchor_max": [0.0, 1.0], "pivot": [px, py],
            "size": [w, h], "pos": [x + w * px, -(y + h * (1 - py))]}


def _grid(props, nodes, kids, width, height):
    """(preferred (w, h), {child: rect values}) of a GridLayoutGroup."""
    left, right, top, bottom = _padding(props)
    cw, ch = float(_num(props, "m_CellSize.x", 100)), float(_num(props, "m_CellSize.y", 100))
    sx, sy = float(_num(props, "m_Spacing.x", 0)), float(_num(props, "m_Spacing.y", 0))
    n = len(kids)
    constraint, count = int(_num(props, "m_Constraint", 0)), max(1, int(_num(props, "m_ConstraintCount", 2)))
    vertical = int(_num(props, "m_StartAxis", 0)) == 1
    if constraint == 1:
        cols = count
        rows = -(-n // cols)
    elif constraint == 2:
        rows = count
        cols = -(-n // rows)
    elif vertical:
        rows = max(1, int((height - top - bottom + sy + 1e-3) // (ch + sy))) if ch + sy > 0 else 1
        cols = -(-n // rows)
    else:
        cols = max(1, int((width - left - right + sx + 1e-3) // (cw + sx))) if cw + sx > 0 else 1
        rows = -(-n // cols)
    cols, rows = max(cols, 1), max(rows, 1)
    need_w, need_h = cols * cw + (cols - 1) * sx, rows * ch + (rows - 1) * sy
    pref = (left + right + need_w, top + bottom + need_h)
    align = int(_num(props, "m_ChildAlignment", 0))
    off_x = left + (width - left - right - need_w) * (align % 3) / 2
    off_y = top + (height - top - bottom - need_h) * (align // 3) / 2
    corner = int(_num(props, "m_StartCorner", 0))
    out = {}
    for k, j in enumerate(kids):
        cx, cy = (k // rows, k % rows) if vertical else (k % cols, k // cols)
        if corner % 2:
            cx = cols - 1 - cx
        if corner // 2:
            cy = rows - 1 - cy
        out[j] = _placed(nodes[j]["rect"], off_x + cx * (cw + sx), off_y + cy * (ch + sy), cw, ch)
    return pref, out


def _line(props, nodes, kids, width, height, horizontal):
    """(preferred (w, h), {child: rect values}) of a Horizontal/VerticalLayoutGroup."""
    left, right, top, bottom = _padding(props)
    spacing = float(_num(props, "m_Spacing", 0))
    axis, cross = (0, 1) if horizontal else (1, 0)
    inner = (width - left - right, height - top - bottom)
    control = [bool(_num(props, f"m_ChildControl{k}", 1)) for k in ("Width", "Height")]
    expand = [bool(_num(props, f"m_ChildForceExpand{k}", 1)) for k in ("Width", "Height")]
    sizes = [_preferred(nodes[j]) for j in kids]
    total = sum(sz[axis] for sz in sizes) + spacing * max(len(kids) - 1, 0)
    pref = [0.0, 0.0]
    pref[axis] = total
    pref[cross] = max((sz[cross] for sz in sizes), default=0.0)
    pref = (pref[0] + left + right, pref[1] + top + bottom)
    extra = inner[axis] - total
    share = extra / len(kids) if kids and extra > 0 and expand[axis] else 0.0
    grow, gap = (share, 0.0) if control[axis] else (0.0, share)
    align = int(_num(props, "m_ChildAlignment", 0))
    frac = ((align % 3) / 2, (align // 3) / 2)
    at = 0.0 if share or extra <= 0 else extra * frac[axis]
    if _num(props, "m_ReverseArrangement", 0):
        kids, sizes = kids[::-1], sizes[::-1]
    out = {}
    for j, sz in zip(kids, sizes):
        along = sz[axis] + grow
        across = inner[cross] if control[cross] and expand[cross] else sz[cross]
        cross_at = (inner[cross] - across) * frac[cross]
        if horizontal:
            out[j] = _placed(nodes[j]["rect"], left + at + gap / 2, top + cross_at, along, across)
        else:
            out[j] = _placed(nodes[j]["rect"], left + cross_at, top + at + gap / 2, across, along)
        at += along + gap + spacing
    return pref, out


def _group(comps, nodes, kids, width, height):
    """(preferred size, child placements) of the node's layout group, or None if it has none."""
    for cls, props in comps:
        if not _num(props, "m_Enabled", 1):
            continue
        if cls == GRID:
            return _grid(props, nodes, kids, width, height)
        if cls in (HORIZONTAL, VERTICAL):
            return _line(props, nodes, kids, width, height, cls == HORIZONTAL)
    return None


def _strip_rich(text):
    return RICH_TAG.sub("", text or "")


def _tmp_align(props):
    """(horizontal 'left'|'center'|'right', vertical 'top'|'middle'|'bottom') of a TextMeshPro text."""
    h, v = int(_num(props, "m_HorizontalAlignment", 0)), int(_num(props, "m_VerticalAlignment", 0))
    old = int(_num(props, "m_textAlignment", 65535))
    if (not h or not v) and old != 65535:  # older TMP: one combined value (TopLeft = 257...)
        h, v = old & 0xFF, old & 0xFF00
    hor = "right" if h & 4 else "center" if h & (2 | 16 | 32) else "left"
    ver = "bottom" if v & 1024 else "middle" if v & (512 | 4096) else "top"
    return hor, ver


def _text_align(anchor):
    """Legacy Text TextAnchor (UpperLeft = 0 ... LowerRight = 8)."""
    return ("left", "center", "right")[anchor % 3], ("top", "middle", "bottom")[min(anchor // 3, 2)]


def _graphic(cls, props):
    """Draw item fields of a graphic component, or None if it's not one we draw."""
    if not _num(props, "m_Enabled", 1):
        return None
    if cls == IMAGE:
        sprite = props.get("m_Sprite")
        return {"kind": "image", "color": _color(props, "m_Color"),
                "sprite": sprite.get("asset") if sprite else None,
                "type": int(_num(props, "m_Type", 0)), "preserve_aspect": bool(_num(props, "m_PreserveAspect", 0)),
                "fill_center": bool(_num(props, "m_FillCenter", 1)),
                "fill_method": int(_num(props, "m_FillMethod", 4)), "fill_amount": float(_num(props, "m_FillAmount", 1)),
                "fill_clockwise": bool(_num(props, "m_FillClockwise", 1)),
                "fill_origin": int(_num(props, "m_FillOrigin", 0)),
                "ppu_multiplier": float(_num(props, "m_PixelsPerUnitMultiplier", 1.0)) or 1.0}
    if cls == RAW_IMAGE:
        tex = props.get("m_Texture")
        return {"kind": "raw", "color": _color(props, "m_Color"), "texture": tex.get("asset") if tex else None,
                "uv": tuple(float(_num(props, f"m_UVRect.{k}", d)) for k, d in
                            (("x", 0), ("y", 0), ("width", 1), ("height", 1)))}
    if cls in TMP_TEXTS:
        hor, ver = _tmp_align(props)
        text = props.get("m_text")
        style = int(_num(props, "m_fontStyle", 0))
        font = props.get("m_fontAsset")
        return {"kind": "text", "text": _strip_rich(text["s"] if text else ""),
                "color": _color(props, "m_fontColor", _color(props, "m_Color")),
                "size": float(_num(props, "m_fontSize", 36.0)), "bold": bool(style & 1), "italic": bool(style & 2),
                "lower": bool(style & 8), "upper": bool(style & 16), "align": (hor, ver),
                "wrap": bool(_num(props, "m_enableWordWrapping", _num(props, "m_TextWrappingMode", 1))),
                "font": font.get("asset") if font else None, "tmp": True,
                "material": (props.get("m_sharedMaterial") or {}).get("asset"),
                "spacing": float(_num(props, "m_characterSpacing", 0.0)),
                "word_spacing": float(_num(props, "m_wordSpacing", 0.0)),
                "line_spacing": float(_num(props, "m_lineSpacing", 0.0)),
                "margin": tuple(float(_num(props, f"m_margin.{k}", 0.0)) for k in "xyzw")}
    if cls == TEXT:
        text = props.get("m_Text")
        style = int(_num(props, "m_FontData.m_FontStyle", 0))
        font = props.get("m_FontData.m_Font")
        return {"kind": "text", "text": _strip_rich(text["s"] if text else ""), "color": _color(props, "m_Color"),
                "size": float(_num(props, "m_FontData.m_FontSize", 14)), "bold": style in (1, 3),
                "italic": style in (2, 3), "align": _text_align(int(_num(props, "m_FontData.m_Alignment", 0))),
                "wrap": bool(_num(props, "m_FontData.m_HorizontalOverflow", 0) == 0),
                "font": font.get("asset") if font else None, "tmp": False}
    return None


def effects_of(comps):
    """[(dx, dy, (r, g, b, a), use graphic alpha)] copies a node's Shadow / Outline components draw under it."""
    out = []
    for cls, props in comps:
        if cls not in EFFECTS or not _num(props, "m_Enabled", 1):
            continue
        dx, dy = float(_num(props, "m_EffectDistance.x", 1.0)), float(_num(props, "m_EffectDistance.y", -1.0))
        color = _color(props, "m_EffectColor", (0.0, 0.0, 0.0, 0.5))
        use_alpha = bool(_num(props, "m_UseGraphicAlpha", 1))
        offsets = [(dx, dy), (dx, -dy), (-dx, dy), (-dx, -dy)] if cls.endswith("Outline") else [(dx, dy)]
        out += [(x, y, color, use_alpha) for x, y in offsets]
    return out


def corners(matrix, rect):
    """The 4 canvas-space corners of a local rect (x0, y0, w, h)."""
    x0, y0, w, h = rect
    pts = np.array([[x0, y0, 1], [x0 + w, y0, 1], [x0 + w, y0 + h, 1], [x0, y0 + h, 1]], float)
    return (pts @ matrix.T)[:, :2]


def layout(nodes, visible=None, screen=None, boxes=None):
    """Draw items in hierarchy order (later ones on top): {"node", "matrix" (3x3 local -> canvas), "rect"
    (x0, y0, w, h local), "clip" ([corner arrays] to intersect, or []), "alpha" (CanvasGroup), plus the graphic's
    own fields from _graphic()}. visible: per-node flags (skip hidden objects), or None to draw everything.
    boxes: optional dict filled with {node: canvas-space corners of its rect} (to outline the selected object)."""
    sw, sh = screen or screen_size(nodes)
    root_rect = (-sw / 2, -sh / 2, sw, sh)
    state = []   # per node: (matrix, rect, clip list, alpha, shape masks (node indices) it is inside)
    items = []
    children = [[] for _ in nodes]
    for i, n in enumerate(nodes):
        if 0 <= n.get("parent", -1) < len(nodes):
            children[n["parent"]].append(i)
    placed = {}  # node -> RectTransform values its parent's layout group gave it
    for i, n in enumerate(nodes):
        parent = n.get("parent", -1)
        if 0 <= parent < len(state):
            p_matrix, p_rect, p_clip, p_alpha, p_masks = state[parent]
        else:
            p_matrix, p_rect, p_clip, p_alpha, p_masks = np.eye(3), root_rect, [], 1.0, ()
        comps = _scripted(n)
        is_canvas = any(cls == "Canvas" and int(_num(pr, "m_RenderMode", 0)) in (0, 1) for cls, pr in comps)
        if "rect" in n and is_canvas and parent < 0:
            # A screen-space canvas fills the screen whatever its saved size says.
            matrix, rect = p_matrix, root_rect
        elif "rect" in n:
            r = placed.get(i, n["rect"])
            kids = _layout_children(nodes, children[i])
            fitter = next((pr for cls, pr in comps if cls == FITTER and _num(pr, "m_Enabled", 1)), None)
            if fitter is not None and kids:
                group = _group(comps, nodes, kids, 0.0, 0.0)
                if group is not None:
                    size = list(r["size"])
                    for axis, key in ((0, "m_HorizontalFit"), (1, "m_VerticalFit")):
                        if _num(fitter, key, 0) and r["anchor_min"][axis] == r["anchor_max"][axis]:
                            size[axis] = group[0][axis]
                    r = {**r, "size": size}
            pos, rect = rect_layout(r, p_rect)
            matrix = p_matrix @ affine(pos, n.get("rot") or (0, 0, 0, 1), n.get("scale") or (1, 1, 1))
            if kids:
                group = _group(comps, nodes, kids, rect[2], rect[3])
                if group is not None:
                    placed.update(group[1])
        else:
            p = n.get("pos") or (0, 0, 0)
            matrix = p_matrix @ affine(p[:2], n.get("rot") or (0, 0, 0, 1), n.get("scale") or (1, 1, 1))
            rect = (0.0, 0.0, 0.0, 0.0)
        alpha = p_alpha
        for cls, props in comps:
            if cls == "CanvasGroup" and _num(props, "m_Enabled", 1):
                alpha *= float(_num(props, "m_Alpha", 1.0))
        mask = next(((cls, props) for cls, props in comps if cls in MASKS and _num(props, "m_Enabled", 1)), None)
        clip = p_clip + [corners(matrix, rect)] if mask is not None else p_clip
        masks = p_masks + (i,) if mask is not None and mask[0] in SHAPE_MASKS else p_masks
        state.append((matrix, rect, clip, alpha, masks))
        if boxes is not None and "rect" in n:
            boxes[i] = corners(matrix, rect)
        if visible is not None and not visible[i]:
            continue
        hide_graphic = mask is not None and mask[0] != "UnityEngine.UI.RectMask2D" and not _num(
            mask[1], "m_ShowMaskGraphic", 1)
        effects = effects_of(comps)
        for cls, props in comps:
            g = _graphic(cls, props)
            if g is None or rect[2] <= 0 or rect[3] <= 0:
                continue
            item = {"node": i, "matrix": matrix, "rect": rect, "clip": p_clip, "alpha": alpha, "masks": p_masks,
                    "effects": [] if g.get("tmp") else effects, **g}
            if mask is not None and mask[0] in SHAPE_MASKS:
                item["mask_of"] = i  # its shape cuts the children
                item["mask_kind"] = mask[0]
            if hide_graphic:
                item["mask_only"] = True  # not drawn itself
            items.append(item)
    return items


def bounds(items, clip_to=None):
    """(x0, y0, x1, y1) around every drawn item's corners in canvas space, or None."""
    pts = [corners(it["matrix"], it["rect"]) for it in items if not it.get("mask_only")]
    if not pts:
        return None
    pts = np.concatenate(pts)
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    if clip_to is not None:
        x0, y0 = max(x0, clip_to[0]), max(y0, clip_to[1])
        x1, y1 = min(x1, clip_to[2]), min(y1, clip_to[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return float(x0), float(y0), float(x1), float(y1)


def slice_borders(rect_w, rect_h, border, sprite_ppu, multiplier, reference_ppu=100.0):
    """9-slice borders (left, bottom, right, top) in rect units, shrunk like Unity when the rect is too small."""
    ppu = sprite_ppu / reference_ppu * multiplier or 1.0
    left, bottom, right, top = (b / ppu for b in border)
    if left + right > rect_w and left + right > 0:
        k = rect_w / (left + right)
        left, right = left * k, right * k
    if bottom + top > rect_h and bottom + top > 0:
        k = rect_h / (bottom + top)
        bottom, top = bottom * k, top * k
    return left, bottom, right, top
