"""TextMeshPro font assets and text layout with them."""

import pytest

from engines.unity_tmp import parse_font
from uniview import ui_text


def font_tree():
    """A tiny TMP_FontAsset type tree: point size 10, glyphs 'A' (index 1) and space (index 2)."""
    return {
        "m_Name": "Test SDF",
        "m_FaceInfo": {"m_PointSize": 10.0, "m_Scale": 1.0, "m_LineHeight": 12.0, "m_AscentLine": 8.0,
                       "m_DescentLine": -2.0},
        "m_AtlasWidth": 64, "m_AtlasHeight": 64, "m_AtlasPadding": 2, "m_AtlasRenderMode": 4165,
        "m_GlyphTable": [
            {"m_Index": 1, "m_Metrics": {"m_Width": 6.0, "m_Height": 8.0, "m_HorizontalBearingX": 1.0,
                                         "m_HorizontalBearingY": 8.0, "m_HorizontalAdvance": 8.0},
             "m_GlyphRect": {"m_X": 4, "m_Y": 50, "m_Width": 6, "m_Height": 8}, "m_Scale": 1.0, "m_AtlasIndex": 0},
            {"m_Index": 2, "m_Metrics": {"m_HorizontalAdvance": 4.0},
             "m_GlyphRect": {"m_X": 0, "m_Y": 0, "m_Width": 0, "m_Height": 0}, "m_Scale": 1.0},
        ],
        "m_CharacterTable": [{"m_Unicode": 65, "m_GlyphIndex": 1}, {"m_Unicode": 32, "m_GlyphIndex": 2}],
        "m_FontFeatureTable": {"m_GlyphPairAdjustmentRecords": [
            {"m_FirstAdjustmentRecord": {"m_GlyphIndex": 1, "m_GlyphValueRecord": {"m_XAdvance": -1.0}},
             "m_SecondAdjustmentRecord": {"m_GlyphIndex": 1}}]},
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 5}],
    }


def ready(tree):
    font = parse_font(tree)
    font["images"] = [object()]  # stands in for the atlas image
    font["fallback_fonts"] = []
    return font


def text_item(text, **kw):
    item = {"text": text, "size": 20.0, "align": ("left", "top"), "wrap": False}
    item.update(kw)
    return item


def test_parse_new_layout():
    f = parse_font(font_tree())
    a = f["chars"][65]
    assert a["rect"] == (4, 64 - 50 - 8, 6, 8)  # flipped to y-from-top
    assert a["bearing"] == (1.0, 8.0) and a["advance"] == 8.0
    assert f["kerning"] == {(65, 65): -1.0}
    assert f["sdf"] and f["atlases"] == [{"m_FileID": 0, "m_PathID": 5}]


def test_parse_old_layout():
    tree = {"m_fontInfo": {"PointSize": 10, "Scale": 1, "LineHeight": 12, "Ascender": 8, "Descender": -2,
                           "Padding": 2, "AtlasWidth": 64, "AtlasHeight": 64},
            "m_glyphInfoList": [{"id": 66, "x": 3, "y": 4, "width": 5, "height": 6, "xOffset": 1, "yOffset": 6,
                                 "xAdvance": 7, "scale": 1}],
            "atlas": {"m_FileID": 0, "m_PathID": 9}}
    f = parse_font(tree)
    assert f["chars"][66]["rect"] == (3, 4, 5, 6) and f["atlases"] == [{"m_FileID": 0, "m_PathID": 9}]


def test_layout_places_glyphs_with_kerning_and_spacing():
    font = ready(font_tree())
    laid = ui_text.layout_text(text_item("AA A"), font, 200, 50)
    glyphs = laid["glyphs"]
    assert len(glyphs) == 3 and laid["missing"] == 0
    s = 2.0  # size 20 / point size 10
    g0, g1, g2 = glyphs
    assert g0["dst"][0] == pytest.approx((1 - 2) * s)              # bearing x minus padding
    assert g0["dst"][1] == pytest.approx(8 * s - (8 + 2) * s)      # baseline (ascent) minus bearing y + padding
    assert g1["dst"][0] - g0["dst"][0] == pytest.approx((8 - 1) * s)  # advance with kerning
    assert g2["dst"][0] - g1["dst"][0] == pytest.approx((8 + 4) * s)  # 'A' then the space
    assert g0["src"] == (2, 4, 10, 12)  # atlas rect grown by the padding


def test_layout_wraps_aligns_and_counts_missing():
    font = ready(font_tree())
    laid = ui_text.layout_text(text_item("AA AA", wrap=True, align=("right", "bottom")), font, 40, 100)
    baselines = sorted({g["baseline"] for g in laid["glyphs"]})
    assert len(baselines) == 2 and baselines[1] - baselines[0] == pytest.approx(12 * 2)
    assert baselines[1] == pytest.approx(100 - 2 * 2)  # bottom: last baseline = height + descent * scale
    right = max(g["dst"][0] + g["dst"][2] for g in laid["glyphs"])
    assert right <= 40 + 2 * 2 + 1e-6
    assert ui_text.layout_text(text_item("AzA"), font, 100, 50)["missing"] == 1


def test_fallback_font_and_case():
    main = ready(font_tree())
    main["chars"] = {}
    main["fallback_fonts"] = [ready(font_tree())]
    laid = ui_text.layout_text(text_item("a", upper=True), main, 100, 50)
    assert len(laid["glyphs"]) == 1 and laid["glyphs"][0]["font"] is main["fallback_fonts"][0]
    assert not ui_text.has_glyphs(main) and ui_text.has_glyphs(main["fallback_fonts"][0])


def test_tmp_material_passes():
    from uniview.ui.canvas_render import tmp_passes
    item = text_item("x", color=(1.0, 1.0, 1.0, 1.0))
    assert len(tmp_passes(item, None, 10)) == 1  # face only
    material = {"floats": {"_OutlineWidth": 0.2, "_UnderlayOffsetX": 0.5, "_UnderlayOffsetY": -1.0},
                "colors": {"_OutlineColor": (1.0, 0.0, 0.0, 1.0), "_FaceColor": (0.0, 1.0, 0.0, 1.0)},
                "keywords": ["UNDERLAY_ON"]}
    under, outline, face = tmp_passes(item, material, 10)
    assert under[3] == (5.0, -10.0)                       # offset in atlas pixels (x gradient scale)
    assert outline[0] == (1.0, 0.0, 0.0, 1.0) and face[0] == (0.0, 1.0, 0.0, 1.0)
    assert outline[1] < 0.5 < face[1]                     # outline outside the glyph edge, face inside it
