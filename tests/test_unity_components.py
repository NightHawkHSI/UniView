"""Components as flat SerializedProperty paths, and their references turned into project paths."""

from engines.unity_components import flatten
from uniview.unity_project import component_props


def resolve(file_id, path_id):
    return {7: {"node": 2, "cls": "Rigidbody"}, 8: {"asset": "s:9", "kind": "mesh"},
            9: {"asset": "material:x:1", "kind": "material"}, 10: {"asset": "r:5", "kind": "file"}}.get(path_id)


def test_flatten_values_vectors_and_bookkeeping():
    tree = {"m_GameObject": {"m_FileID": 0, "m_PathID": 3}, "m_Enabled": True, "m_IsTrigger": False,
            "m_Center": {"x": 0.5, "y": 1.0, "z": -2.0}, "m_Layer": 3, "m_Name": "x", "m_Tag": "Player"}
    out = flatten(tree, resolve)
    assert out == [{"p": "m_Enabled", "t": "b", "v": 1}, {"p": "m_IsTrigger", "t": "b", "v": 0},
                   {"p": "m_Center.x", "t": "f", "v": 0.5}, {"p": "m_Center.y", "t": "f", "v": 1.0},
                   {"p": "m_Center.z", "t": "f", "v": -2.0}, {"p": "m_Layer", "t": "i", "v": 3},
                   {"p": "m_Tag", "t": "s", "s": "Player"}]


def test_flatten_arrays_size_first_and_references():
    tree = {"m_Materials": [{"m_FileID": 0, "m_PathID": 9}, {"m_FileID": 0, "m_PathID": 0}],
            "m_ConnectedBody": {"m_FileID": 0, "m_PathID": 7}, "m_Mesh": {"m_FileID": 1, "m_PathID": 8},
            "m_Unknown": {"m_FileID": 0, "m_PathID": 99}, "m_Data": b"\x00\x01"}
    out = flatten(tree, resolve)
    assert out[0] == {"p": "m_Materials.Array.size", "t": "i", "v": 2}
    assert out[1] == {"p": "m_Materials.Array.data[0]", "t": "ref", "asset": "material:x:1", "kind": "material"}
    assert {"p": "m_ConnectedBody", "t": "ref", "node": 2, "cls": "Rigidbody"} in out
    assert not any(e["p"] in ("m_Unknown", "m_Data", "m_Materials.Array.data[1]") for e in out)


def test_flatten_map_pairs():
    out = flatten({"m_Map": [("a", 1), ("b", 2)]}, resolve)
    assert out[:3] == [{"p": "m_Map.Array.size", "t": "i", "v": 2}, {"p": "m_Map.Array.data[0].first", "t": "s", "s": "a"},
                       {"p": "m_Map.Array.data[0].second", "t": "i", "v": 1}]


class Library:
    def path_for(self, uid):
        return "Assets/x/Materials/M.mat" if uid == "material:x:1" else ""


def test_component_props_turn_refs_into_paths():
    props = flatten({"m_Mesh": {"m_FileID": 0, "m_PathID": 8}, "m_Mat": {"m_FileID": 0, "m_PathID": 9},
                     "m_Clip": {"m_FileID": 0, "m_PathID": 10}, "m_Body": {"m_FileID": 0, "m_PathID": 7},
                     "m_Size": 2.0}, resolve)
    paths = {"s:9": "/p/Assets/x/Models/m.glb", "r:5": "/p/Assets/r/Audio/boom.wav"}
    out = component_props(props, paths, "/p", Library())
    assert out == [{"p": "m_Mesh", "t": "m", "s": "Assets/x/Models/m.glb"},
                   {"p": "m_Mat", "t": "a", "s": "Assets/x/Materials/M.mat"},
                   {"p": "m_Clip", "t": "a", "s": "Assets/r/Audio/boom.wav"},
                   {"p": "m_Body", "t": "n", "n": 2, "s": "Rigidbody"},
                   {"p": "m_Size", "t": "f", "v": 2.0}]
    assert component_props(props, {}, "/p", None) == [{"p": "m_Body", "t": "n", "n": 2, "s": "Rigidbody"},
                                                        {"p": "m_Size", "t": "f", "v": 2.0}]
