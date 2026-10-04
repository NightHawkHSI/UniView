"""TextMeshPro font assets (TMP_FontAsset MonoBehaviours): glyph metrics, atlas positions and kerning, from both
the current layout (m_FaceInfo / m_GlyphTable / m_CharacterTable) and the old one (m_fontInfo / m_glyphInfoList)."""

# Glyph render modes that are plain (non-SDF) bitmaps: SMOOTH, RASTER and their hinted versions.
BITMAP_MODES = {4117, 4118, 4121, 4122}


def _f(d, key, default=0.0):
    try:
        return float(d.get(key, default))
    except (TypeError, ValueError):
        return default


def parse_font(tree):
    """Font metrics + glyphs of a TMP_FontAsset type tree. Returns {"name", "point_size", "scale", "line_height",
    "ascent", "descent", "padding", "atlas_size" (w, h), "sdf", "bold_style", "bold_spacing", "italic_style",
    "chars": {codepoint: glyph}, "kerning": {(codepoint, codepoint): x advance}, "atlases": [PPtr dict],
    "fallbacks": [PPtr dict], "source_font": PPtr dict or None}. A glyph is {"atlas" (index), "rect"
    (x, y from the TOP, w, h) in atlas pixels, "bearing" (x, y), "size" (w, h), "advance", "scale"}."""
    face = tree.get("m_FaceInfo") or {}
    old = tree.get("m_fontInfo") or {}
    new_layout = bool(tree.get("m_GlyphTable")) or not tree.get("m_glyphInfoList")
    atlas_w = _f(tree, "m_AtlasWidth", _f(old, "AtlasWidth", 0))
    atlas_h = _f(tree, "m_AtlasHeight", _f(old, "AtlasHeight", 0))
    out = {
        "name": tree.get("m_Name", ""),
        "point_size": _f(face, "m_PointSize", 0) or _f(old, "PointSize", 36) or 36.0,
        "scale": _f(face, "m_Scale", 0) or _f(old, "Scale", 1) or 1.0,
        "line_height": _f(face, "m_LineHeight", 0) or _f(old, "LineHeight", 0),
        "ascent": _f(face, "m_AscentLine", 0) or _f(old, "Ascender", 0),
        "descent": _f(face, "m_DescentLine", 0) or _f(old, "Descender", 0),
        "padding": _f(tree, "m_AtlasPadding", 0) or _f(old, "Padding", 0),
        "atlas_size": (atlas_w, atlas_h),
        "sdf": int(tree.get("m_AtlasRenderMode", 4165) or 4165) not in BITMAP_MODES,
        "bold_style": _f(tree, "boldStyle", 0.75), "bold_spacing": _f(tree, "boldSpacing", 7.0),
        "italic_style": _f(tree, "italicStyle", 35.0),
        "chars": {}, "kerning": {},
        "atlases": list(tree.get("m_AtlasTextures") or []) or [p for p in (tree.get("atlas"),) if p],
        "fallbacks": list(tree.get("m_FallbackFontAssetTable") or tree.get("fallbackFontAssets") or []),
        "source_font": tree.get("m_SourceFontFile"),
    }
    if new_layout:
        glyphs = {}
        for g in tree.get("m_GlyphTable") or []:
            m, r = g.get("m_Metrics") or {}, g.get("m_GlyphRect") or {}
            h = _f(r, "m_Height")
            glyphs[int(g.get("m_Index", -1))] = {
                "atlas": int(g.get("m_AtlasIndex", 0) or 0),
                "rect": (_f(r, "m_X"), atlas_h - _f(r, "m_Y") - h, _f(r, "m_Width"), h),  # GlyphRect y is from the bottom
                "bearing": (_f(m, "m_HorizontalBearingX"), _f(m, "m_HorizontalBearingY")),
                "size": (_f(m, "m_Width"), _f(m, "m_Height")),
                "advance": _f(m, "m_HorizontalAdvance"), "scale": _f(g, "m_Scale", 1.0) or 1.0}
        by_glyph = {}
        for c in tree.get("m_CharacterTable") or []:
            glyph = glyphs.get(int(c.get("m_GlyphIndex", -1)))
            if glyph is not None:
                cp = int(c.get("m_Unicode", 0))
                out["chars"][cp] = glyph
                by_glyph.setdefault(int(c.get("m_GlyphIndex", -1)), []).append(cp)
        features = tree.get("m_FontFeatureTable") or {}
        for rec in features.get("m_GlyphPairAdjustmentRecords") or []:
            first, second = rec.get("m_FirstAdjustmentRecord") or {}, rec.get("m_SecondAdjustmentRecord") or {}
            dx = _f(first.get("m_GlyphValueRecord") or {}, "m_XAdvance")
            if not dx:
                continue
            for a in by_glyph.get(int(first.get("m_GlyphIndex", -1)), []):
                for b in by_glyph.get(int(second.get("m_GlyphIndex", -1)), []):
                    out["kerning"][(a, b)] = dx
    else:
        for g in tree.get("m_glyphInfoList") or []:
            w, h = _f(g, "width"), _f(g, "height")
            out["chars"][int(g.get("id", 0))] = {
                "atlas": 0, "rect": (_f(g, "x"), _f(g, "y"), w, h),  # old glyph y is from the top
                "bearing": (_f(g, "xOffset"), _f(g, "yOffset")), "size": (w, h),
                "advance": _f(g, "xAdvance"), "scale": _f(g, "scale", 1.0) or 1.0}
        kerning = (tree.get("m_kerningInfo") or {}).get("kerningPairs") or []
        for p in kerning:
            a, b = int(p.get("m_FirstGlyph", p.get("AscII_Left", 0))), int(p.get("m_SecondGlyph", p.get("AscII_Right", 0)))
            dx = _f(p.get("m_FirstGlyphAdjustments") or {}, "xAdvance") or _f(p, "XadvanceOffset")
            if dx:
                out["kerning"][(a, b)] = dx
    if not out["line_height"]:
        out["line_height"] = out["ascent"] - out["descent"] or out["point_size"] * 1.2
    if not out["ascent"]:
        out["ascent"] = out["point_size"] * 0.8
    return out
