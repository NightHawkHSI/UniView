"""Animation state machine graph: layout, edges and readable transitions."""

from types import SimpleNamespace

from uniview import controller_graph as cg


def state(name, *dests, clip=None, conditions=()):
    return {"name": name, "motion": {"clip": clip} if clip else None,
            "transitions": [{"dest": d, "conditions": list(conditions), "has_exit_time": False, "duration": 0.0}
                            for d in dests]}


def layer():
    # Idle <-> Walk -> Run, Any State -> Dead, Orphan nothing reaches
    return {"default_state": 0, "states": [state("Idle", 1), state("Walk", 0, 2), state("Run"), state("Dead"),
                                           state("Orphan")],
            "any_transitions": [{"dest": 3, "conditions": [{"mode": 1, "param": "Die", "threshold": 0}]}]}


def test_layout_columns_by_steps_from_default():
    places = cg.layout(layer())
    assert places[0] == (0, 0)
    assert places[1][0] == 1 and places[3][0] == 1  # Walk, and Dead through Any State, one step in
    assert places[2][0] == 2 and places[4][0] == 3  # Run two steps; Orphan in the last column
    assert len(set(places.values())) == 5           # no two states on the same spot


def test_edges_include_any_state():
    e = {(a, b) for a, b, _t in cg.edges(layer())}
    assert e == {(0, 1), (1, 0), (1, 2), (-1, 3)}


def test_transition_text():
    states = layer()["states"]
    t = {"dest": 2, "conditions": [{"mode": 3, "param": "Speed", "threshold": 0.5},
                                   {"mode": 2, "param": "Crouch", "threshold": 0}],
         "has_exit_time": True, "exit_time": 0.75, "duration": 0.25, "fixed_duration": True}
    assert cg.transition_text(t, states) == "Run  (Speed > 0.5, not Crouch, at 0.75 of the clip, blend 0.25 s)"
    assert cg.transition_text({"dest": -1}, states) == "Exit"


def test_motion_clips():
    a, b = SimpleNamespace(name="a"), SimpleNamespace(name="b")
    assert cg.motion_clips({"clip": a}) == [a]
    tree = {"tree": {"nodes": [{"clip": None, "children": [1, 2]}, {"clip": a}, {"clip": b}]}}
    assert cg.motion_clips(tree) == [a, b] and cg.motion_clips(None) == []
