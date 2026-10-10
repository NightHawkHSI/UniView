"""Compact object tables (engines/unity_lazy.py): same answers as UnityPy's plain dict."""

from types import SimpleNamespace

import numpy as np

from engines.unity_lazy import LazyObjects, _join_blocks, _node_bytes, objects_of


def make_table():
    dtype = np.dtype([("id", "<i8"), ("start", "<i8"), ("size", "<u4"), ("type", "<i4")])
    rows = [(50, 0, 10, 0), (-3, 10, 20, 1), (7, 30, 5, 0), (900, 35, 8, 2)]
    types = [SimpleNamespace(class_id=4), SimpleNamespace(class_id=1), SimpleNamespace(class_id=28)]  # Transform, GO, Tex
    f = SimpleNamespace(types=types, name="level3")
    return f, LazyObjects(f, reader=None, table=np.array(rows, dtype=dtype), data_offset=100)


def test_lookup_and_fields():
    f, objs = make_table()
    assert len(objs) == 4 and objs
    t = objs[7]
    assert (t.path_id, t.type.name, t.byte_start, t.byte_size, t.assets_file) == (7, "Transform", 130, 5, f)
    assert objs[-3].type.name == "GameObject"
    assert objs.get(12345) is None and 12345 not in objs and 50 in objs
    assert objs[7] is t  # one reader per object while it's held


def test_type_filter_counts_and_sizes():
    _f, objs = make_table()
    assert sorted(o.path_id for o in objs.of_types({"Transform"})) == [7, 50]
    assert objs.count_of(["Transform"]) == 2 and objs.size_of(["Transform"]) == 15
    assert [o.path_id for o in objs.of_types({"Transform"}, one_per_type=True)] == [50]
    assert list(objs) == [50, -3, 7, 900]


def test_set_delete_and_env_walk():
    f, objs = make_table()
    extra = SimpleNamespace(path_id=1, type=SimpleNamespace(name="Mesh"))
    objs[1] = extra
    del objs[900]
    assert objs[1] is extra and 900 not in objs and len(objs) == 4
    assert [o.path_id for o in objs.of_types({"Texture2D"})] == []


def test_objects_of_plain_dict():
    from UnityPy.files.SerializedFile import SerializedFile
    sf = SerializedFile.__new__(SerializedFile)
    a = SimpleNamespace(type=SimpleNamespace(name="Mesh"), type_id=0)
    b = SimpleNamespace(type=SimpleNamespace(name="Material"), type_id=1)
    sf.objects = {1: a, 2: b}
    env = SimpleNamespace(files={"x": sf})
    assert list(objects_of(env, ("Mesh",))) == [a]


def test_join_blocks_and_node_views():
    blocks = [SimpleNamespace(compressedSize=3, uncompressedSize=3, flags=0),
              SimpleNamespace(compressedSize=2, uncompressedSize=2, flags=0)]
    data = iter([b"abc", b"de"])
    reader = SimpleNamespace(read_bytes=lambda n: next(data))
    bundle = SimpleNamespace(decompress_data=lambda raw, size, flags, i: raw)
    buf = _join_blocks(bundle, reader, blocks)
    assert bytes(buf) == b"abcde"
    view_reader = SimpleNamespace(view=memoryview(buf), Position=1)
    part = _node_bytes(view_reader, 3)
    assert bytes(part) == b"bcd" and view_reader.Position == 4 and isinstance(part, memoryview)
