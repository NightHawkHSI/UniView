"""UI prefab layout: RectTransform anchors, layout groups, masks and graphics."""

import numpy as np
import pytest

from uniview import ui_canvas


def rect(amin=(0.5, 0.5), amax=(0.5, 0.5), pos=(0, 0), size=(100, 100), pivot=(0.5, 0.5)):
    return {"anchor_min": list(amin), "anchor_max": list(amax), "pos": list(pos), "size": list(size),
            "pivot": list(pivot)}


def node(name, parent=-1, r=None, comps=(), active=True):
    return {"name": name, "parent": parent, "active": active, "rot": [0, 0, 0, 1], "scale": [1, 1, 1],
            "rect": r or rect(), "components": list(comps)}


def comp(script, **props):
    out = []
    for p, v in props.items():
        p = p.replace("__", ".")
        if isinstance(v, str):
            out.append({"p": p, "t": "s", "s": v})
        elif isinstance(v, dict):
            out.append({"p": p, "t": "ref", **v})
        else:
            out.append({"p": p, "t": "f" if isinstance(v, float) else "i", "v": v})
    return {"type": "MonoBehaviour", "script": script, "props": out}


def image(**props):
    return comp(ui_canvas.IMAGE, m_Sprite={"asset": "s1", "kind": "sprite"}, **props)


def test_rect_layout_fixed_and_stretched():
    pos, own = ui_canvas.rect_layout(rect(pos=(10, 20), size=(40, 30), pivot=(0, 1)), (-50, -50, 100, 100))
    assert pos == (10, 20) and own == (0, -30, 40, 30)
    pos, own = ui_canvas.rect_layout(rect((0, 0), (1, 1), size=(-20, 0)), (-50, -50, 100, 100))
    assert pos == (0, 0) and own == (-40, -50, 80, 100)


def test_layout_places_children_and_skips_hidden():
    nodes = [node("root", r=rect(size=(200, 100)), comps=[image()]),
             node("child", 0, rect((1, 1), (1, 1), size=(20, 20), pivot=(1, 1)), [image(m_Color__a=0.5)]),
             node("off", 0, comps=[image()], active=False)]
    visible = [True, True, False]
    boxes = {}
    items = ui_canvas.layout(nodes, visible, (1000, 1000), boxes)
    assert [i["node"] for i in items] == [0, 1]
    assert items[1]["color"][3] == 0.5
    corners = ui_canvas.corners(items[1]["matrix"], items[1]["rect"])
    assert np.allclose(corners.min(axis=0), [80, 30]) and np.allclose(corners.max(axis=0), [100, 50])
    assert set(boxes) == {0, 1, 2}
    assert ui_canvas.bounds(items) == (-100, -50, 100, 50)


def test_mask_clips_children_and_can_hide_its_graphic():
    mask = comp("UnityEngine.UI.Mask", m_ShowMaskGraphic=0)
    nodes = [node("mask", comps=[image(), mask]), node("inside", 0, rect(size=(300, 300)), [image()])]
    items = ui_canvas.layout(nodes, screen=(1000, 1000))
    shown = [i for i in items if not i.get("mask_only")]
    assert [i["node"] for i in shown] == [1]
    assert len(shown[0]["clip"]) == 1 and shown[0]["masks"] == (0,)
    mask_graphic = next(i for i in items if i.get("mask_only"))
    assert mask_graphic["mask_of"] == 0  # its shape cuts the child
    assert ui_canvas.bounds(items) == (-150, -150, 150, 150)  # the hidden mask graphic doesn't count


def test_canvas_group_alpha_multiplies_down():
    group = {"type": "CanvasGroup", "props": [{"p": "m_Alpha", "t": "f", "v": 0.5}]}
    nodes = [node("g", comps=[group]), node("img", 0, comps=[image(m_Color__a=0.5)])]
    item = ui_canvas.layout(nodes, screen=(1000, 1000))[0]
    assert item["alpha"] == 0.5 and item["color"][3] == 0.5


def test_grid_layout_with_content_size_fitter():
    grid = comp(ui_canvas.GRID, m_CellSize__x=16.0, m_CellSize__y=28.0, m_Spacing__x=-6.0, m_Constraint=2,
                m_ConstraintCount=1, m_StartAxis=0)
    fitter = comp(ui_canvas.FITTER, m_HorizontalFit=2, m_VerticalFit=2)
    cells = [node(f"c{k}", 0, rect((0, 0), (0, 0), size=(0, 0)), [image()]) for k in range(3)]
    nodes = [node("grid", r=rect(size=(0, 0)), comps=[grid, fitter])] + cells
    for k, c in enumerate(cells):
        c["parent"] = 0
    boxes = {}
    ui_canvas.layout(nodes, screen=(1000, 1000), boxes=boxes)
    assert np.allclose(boxes[0].max(axis=0) - boxes[0].min(axis=0), [16 * 3 - 12, 28])
    lefts = [boxes[k].min(axis=0)[0] for k in (1, 2, 3)]
    assert np.allclose(np.diff(lefts), [10, 10])


def test_horizontal_layout_expands_controlled_children():
    row = comp(ui_canvas.HORIZONTAL, m_Spacing=10.0, m_ChildControlWidth=1, m_ChildControlHeight=1,
               m_ChildForceExpandWidth=1, m_ChildForceExpandHeight=1)
    nodes = [node("row", r=rect(size=(210, 50)), comps=[row]),
             node("a", 0, rect(size=(0, 0)), [image()]), node("b", 0, rect(size=(0, 0)), [image()])]
    boxes = {}
    ui_canvas.layout(nodes, screen=(1000, 1000), boxes=boxes)
    for k, x0 in ((1, -105), (2, 5)):
        lo, hi = boxes[k].min(axis=0), boxes[k].max(axis=0)
        assert np.allclose([lo[0], hi[0] - lo[0], hi[1] - lo[1]], [x0, 100, 50])


@pytest.mark.parametrize("h, v, old, expect", [(2, 4096, 65535, ("center", "middle")),
                                               (0, 0, 257, ("left", "top")), (4, 1024, 65535, ("right", "bottom"))])
def test_text_alignment(h, v, old, expect):
    tmp = comp("TMPro.TextMeshProUGUI", m_text="<b>Hi</b>", m_HorizontalAlignment=h, m_VerticalAlignment=v,
               m_textAlignment=old, m_fontSize=20.0)
    item = ui_canvas.layout([node("t", comps=[tmp])], screen=(1000, 1000))[0]
    assert item["align"] == expect and item["text"] == "Hi" and item["size"] == 20.0


def test_slice_borders_shrink_when_rect_is_small():
    assert ui_canvas.slice_borders(100, 100, [10, 10, 10, 10], 100, 1) == (10, 10, 10, 10)
    assert ui_canvas.slice_borders(10, 100, [10, 0, 10, 0], 100, 1) == (5, 0, 5, 0)
    assert ui_canvas.slice_borders(100, 100, [10, 10, 10, 10], 200, 1) == (5, 5, 5, 5)


def test_shadow_and_outline_effects():
    shadow = comp("UnityEngine.UI.Shadow", m_EffectColor__r=0.0, m_EffectColor__g=0.0, m_EffectColor__b=0.0,
                  m_EffectColor__a=0.5, m_EffectDistance__x=2.0, m_EffectDistance__y=-3.0, m_UseGraphicAlpha=1)
    outline = comp("UnityEngine.UI.Outline", m_EffectDistance__x=1.0, m_EffectDistance__y=1.0)
    items = ui_canvas.layout([node("img", comps=[image(), shadow, outline])], screen=(1000, 1000))
    effects = items[0]["effects"]
    assert effects[0] == (2.0, -3.0, (0.0, 0.0, 0.0, 0.5), True)
    assert sorted((dx, dy) for dx, dy, _c, _a in effects[1:]) == [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    tmp = comp("TMPro.TextMeshProUGUI", m_text="x")
    assert ui_canvas.layout([node("t", comps=[tmp, shadow])], screen=(1000, 1000))[0]["effects"] == []  # TMP ignores them
