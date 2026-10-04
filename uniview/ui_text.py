"""TextMeshPro text laid out with the game's own font asset (engines.unity_tmp.parse_font() + "fallback_fonts"):
where each glyph of the font atlas goes inside the text's rect. No Qt here - canvas_render draws the result.

Coordinates: the text rect's top-left corner is (0, 0), y goes DOWN."""

ALIGN_X = {"left": 0.0, "center": 0.5, "right": 1.0}


def find_glyph(font, cp, depth=0):
    """(font, glyph) for a code point, looking through the fallback fonts too, or (None, None)."""
    glyph = font["chars"].get(cp)
    if glyph is not None and font.get("images") and glyph["atlas"] < len(font["images"]) and \
            font["images"][glyph["atlas"]] is not None:
        return font, glyph
    if depth < 4:
        for fallback in font.get("fallback_fonts") or []:
            found = find_glyph(fallback, cp, depth + 1)
            if found[0] is not None:
                return found
    return None, None


def has_glyphs(font):
    """True if the font can draw anything (its atlas is in the game files, not filled in while the game runs)."""
    return any(img is not None for img in font.get("images") or []) and bool(font["chars"])


def _cased(text, item):
    if item.get("upper"):
        return text.upper()
    if item.get("lower"):
        return text.lower()
    return text


def layout_text(item, font, width, height):
    """Glyphs of a text draw item in its rect (width x height): {"glyphs": [{"font", "atlas", "src" (x, y, w, h
    in atlas pixels, y from the top), "dst" (x, y, w, h), "baseline", "scale"}], "missing": characters the font
    can't draw}."""
    size = float(item["size"])
    em = size * 0.01  # TMP spacing values are in hundredths of an em

    def scale_of(f):
        return size / (f["point_size"] or 36.0) * (f["scale"] or 1.0)

    s0 = scale_of(font)
    bold = item.get("bold", False)
    spacing = (item.get("spacing", 0.0) + (font["bold_spacing"] if bold else 0.0)) * em
    word_spacing = item.get("word_spacing", 0.0) * em
    margin = item.get("margin") or (0.0, 0.0, 0.0, 0.0)  # left, top, right, bottom
    avail_w = width - margin[0] - margin[2]
    avail_h = height - margin[1] - margin[3]
    text = _cased(item.get("text") or "", item).replace("\t", "    ").replace("\r", "")

    def advance(cp):
        f, g = find_glyph(font, cp)
        if g is None:
            return (size * 0.25 if cp == 32 else size * 0.5), f, g
        return g["advance"] * scale_of(f) * g["scale"] + spacing + (word_spacing if cp == 32 else 0.0), f, g

    def kern(prev, cp):
        """Pair adjustment moving cp closer to (or away from) the character before it."""
        return font["kerning"].get((prev, cp), 0.0) * s0 if prev is not None else 0.0

    def width_of(word):
        w, prev = 0.0, None
        for ch in word:
            w += kern(prev, ord(ch)) + advance(ord(ch))[0]
            prev = ord(ch)
        return w

    # Lines: hard breaks, then word wrapping.
    lines = []
    for paragraph in text.split("\n"):
        if not item.get("wrap") or avail_w <= 0:
            lines.append(paragraph)
            continue
        line = ""
        for word in paragraph.split(" "):
            candidate = word if not line else f"{line} {word}"
            if line and width_of(candidate) > avail_w + 1e-3:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)

    line_height = font["line_height"] * s0 + item.get("line_spacing", 0.0) * em
    block = (font["ascent"] - font["descent"]) * s0 + (len(lines) - 1) * line_height
    ver = item["align"][1]
    top = margin[1] + {"top": 0.0, "middle": (avail_h - block) / 2, "bottom": avail_h - block}.get(ver, 0.0)
    glyphs, missing = [], 0
    for n, line in enumerate(lines):
        baseline = top + font["ascent"] * s0 + n * line_height
        pen = margin[0] + (avail_w - width_of(line)) * ALIGN_X.get(item["align"][0], 0.0)
        prev = None
        for ch in line:
            cp = ord(ch)
            pen += kern(prev, cp)
            step, f, g = advance(cp)
            if g is None and cp not in (32, 0xA0):
                missing += 1
            if g is not None and g["rect"][2] > 0 and g["rect"][3] > 0:
                sc = scale_of(f) * g["scale"]
                pad = f["padding"]
                x, y, w, h = g["rect"]
                bx, by = g["bearing"]
                gw, gh = g["size"]
                # The atlas rect plus its padding (where an SDF fades out), placed by the glyph's bearing.
                glyphs.append({"font": f, "atlas": g["atlas"], "src": (x - pad, y - pad, w + 2 * pad, h + 2 * pad),
                               "dst": (pen + (bx - pad) * sc, baseline - (by + pad) * sc,
                                       (gw + 2 * pad) * sc, (gh + 2 * pad) * sc),
                               "baseline": baseline, "scale": sc})
            pen += step
            prev = cp
    return {"glyphs": glyphs, "missing": missing}
