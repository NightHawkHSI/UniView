"""Compact object tables for huge Unity files.

UnityPy makes one Python ObjectReader per object when it opens a serialized file. That costs ~400 bytes and a few
microseconds each, which is fine for normal games but not for one-bundle builds like PEAK (data.unity3d: 16 million
objects, ~6.6 GB of readers, mostly scene GameObjects/Transforms/colliders nobody looks at during loading).

Files with more than LAZY_MIN objects keep their object table as numpy arrays instead (~26 bytes an object), and
make ObjectReaders on demand: `file.objects[path_id]`, `.get()`, iteration, and `objects_of(env, types)` (which
filters by type in numpy before making any reader). Readers someone still holds are reused (weak cache), so the
same object keeps one reader while it's in use. Smaller files keep UnityPy's plain dict.

install() swaps in a copy of UnityPy 1.25's SerializedFile.__init__ that differs only in the "ReadObjects" step.
Other UnityPy versions are left alone (everything still works, just with the normal memory use)."""

import logging
import weakref
from collections.abc import MutableMapping

import numpy as np

log = logging.getLogger("uniview.unity")

LAZY_MIN = 50_000  # objects in one file before its table is kept compact
_installed = False


class LazyObjects(MutableMapping):
    """{path id: ObjectReader} backed by numpy arrays; readers are made when asked for."""

    def __init__(self, assets_file, reader, table, data_offset):
        from UnityPy.enums import ClassIDType
        from UnityPy.files.ObjectReader import ObjectReader
        self._ObjectReader, self._ClassIDType = ObjectReader, ClassIDType
        self.file, self.reader = assets_file, reader
        self.ids = np.ascontiguousarray(table["id"], dtype=np.int64)
        self.starts = np.ascontiguousarray(table["start"], dtype=np.int64) + data_offset
        self.sizes = np.ascontiguousarray(table["size"], dtype=np.uint32)
        self.type_ids = np.ascontiguousarray(table["type"], dtype=np.int32)
        type_classes = np.array([t.class_id for t in assets_file.types] or [0], dtype=np.int32)
        self.class_ids = type_classes[np.clip(self.type_ids, 0, len(type_classes) - 1)]
        self._order = np.argsort(self.ids, kind="stable").astype(np.int32 if len(self.ids) < 2**31 else np.int64)
        self._sorted_ids = self.ids[self._order]
        self._live = weakref.WeakValueDictionary()
        self._pinned = {}      # set or modified readers: kept for good
        self._deleted = set()

    # ---- rows
    def _row(self, path_id):
        i = int(np.searchsorted(self._sorted_ids, path_id))
        if i < len(self._sorted_ids) and self._sorted_ids[i] == path_id:
            return int(self._order[i])
        return -1

    def _make(self, row):
        path_id = int(self.ids[row])
        obj = self._pinned.get(path_id) or self._live.get(path_id)
        if obj is None:
            type_id = int(self.type_ids[row])
            st = self.file.types[type_id]
            class_id = st.class_id
            obj = self._ObjectReader(self.file, self.reader, path_id, type_id, st, class_id,
                                     self._ClassIDType(class_id), int(self.starts[row]), int(self.sizes[row]),
                                     None, None)
            self._live[path_id] = obj
        return obj

    def class_ids_of(self, type_names):
        out = []
        for name in type_names:
            try:
                out.append(self._ClassIDType[name].value)
            except KeyError:
                continue
        return out

    def rows_of(self, type_names):
        rows = np.nonzero(np.isin(self.class_ids, self.class_ids_of(type_names)))[0]
        return [r for r in rows.tolist() if int(self.ids[r]) not in self._deleted] if self._deleted else rows.tolist()

    def of_types(self, type_names, one_per_type=False):
        """Readers of these types (names like "Transform"), made one at a time."""
        rows = self.rows_of(type_names)
        if one_per_type:
            _types, first = np.unique(self.type_ids[rows], return_index=True)
            rows = [rows[i] for i in sorted(first.tolist())]
        for row in rows:
            yield self._make(row)

    def size_of(self, type_names):
        return int(self.sizes[self.rows_of(type_names)].sum())

    def count_of(self, type_names):
        return len(self.rows_of(type_names))

    # ---- mapping
    def __getitem__(self, path_id):
        if path_id in self._pinned:
            return self._pinned[path_id]
        if path_id in self._deleted:
            raise KeyError(path_id)
        row = self._row(path_id)
        if row < 0:
            raise KeyError(path_id)
        return self._make(row)

    def get(self, path_id, default=None):
        try:
            return self[path_id]
        except (KeyError, TypeError):
            return default

    def __contains__(self, path_id):
        return path_id in self._pinned or (path_id not in self._deleted and self._row(path_id) >= 0)

    def __setitem__(self, path_id, obj):
        self._deleted.discard(path_id)
        self._pinned[path_id] = obj

    def __delitem__(self, path_id):
        if path_id not in self:
            raise KeyError(path_id)
        self._pinned.pop(path_id, None)
        self._deleted.add(path_id)

    def __iter__(self):
        for path_id in self.ids.tolist():
            if path_id not in self._deleted:
                yield path_id
        known = set(self.ids.tolist()) if self._pinned else ()
        for path_id in list(self._pinned):
            if path_id not in known:
                yield path_id

    def __len__(self):
        extra = sum(1 for p in self._pinned if self._row(p) < 0)
        return len(self.ids) - len(self._deleted) + extra

    def __bool__(self):
        return len(self) > 0

    def pin(self, obj):
        self._pinned[obj.path_id] = obj


def objects_of(env, type_names, one_per_type=False):
    """Every object of these types in the environment, without making readers for the others."""
    names = set(type_names)
    for f in serialized_files(env):
        objects = f.objects
        if isinstance(objects, LazyObjects):
            yield from objects.of_types(names, one_per_type)
        else:
            seen = set()
            for obj in list(objects.values()):
                if obj.type.name not in names:
                    continue
                if one_per_type:
                    if obj.type_id in seen:
                        continue
                    seen.add(obj.type_id)
                yield obj


def serialized_files(env):
    """The loaded SerializedFiles (inside bundles too), in load order, skipping dependencies like UnityPy does."""
    from UnityPy.files.SerializedFile import SerializedFile
    out = []

    def walk(item):
        if isinstance(item, SerializedFile):
            if not getattr(item, "is_dependency", False):
                out.append(item)
            return
        files = getattr(item, "files", None)
        if isinstance(files, dict):
            for sub in list(files.values()):
                walk(sub)
    walk(env)
    return out


def all_objects(env):
    """Every object, like env.objects, but as a generator (no 16-million-entry list)."""
    for f in serialized_files(env):
        objects = f.objects
        if isinstance(objects, LazyObjects):
            for row in range(len(objects.ids)):
                if objects._deleted and int(objects.ids[row]) in objects._deleted:
                    continue
                yield objects._make(row)
            for path_id, obj in list(objects._pinned.items()):
                if objects._row(path_id) < 0:
                    yield obj
        else:
            yield from list(objects.values())


def _read_table(sf, reader, count):
    """The object table as a numpy record array, or None when this file's layout isn't the simple one."""
    version = sf.header.version
    if version < 17 or getattr(sf, "big_id_enabled", 0):
        return None
    e = sf.header.endian
    start = (e + "i8") if version >= 22 else (e + "u4")
    dtype = np.dtype([("id", e + "i8"), ("start", start), ("size", e + "u4"), ("type", e + "i4")])
    reader.align_stream()
    raw = reader.read_bytes(count * dtype.itemsize)
    if len(raw) != count * dtype.itemsize:
        raise EOFError("object table cut short")
    return np.frombuffer(raw, dtype=dtype)


def install():
    """Patch UnityPy's SerializedFile so huge files get a LazyObjects table. Safe to call more than once."""
    global _installed
    if _installed:
        return
    import importlib

    import UnityPy
    SF = importlib.import_module("UnityPy.files.SerializedFile")
    if not str(getattr(UnityPy, "__version__", "")).startswith("1.25."):
        log.info("UnityPy %s: compact object tables not enabled (made for 1.25)", getattr(UnityPy, "__version__", "?"))
        _installed = True
        return
    _installed = True
    original_set_raw_data = SF.ObjectReader.set_raw_data

    def set_raw_data(self, data):
        objects = getattr(self.assets_file, "objects", None)
        if isinstance(objects, LazyObjects):
            objects.pin(self)  # an edited object must outlive the weak cache until the file is saved
        return original_set_raw_data(self, data)

    SF.ObjectReader.set_raw_data = set_raw_data

    def __init__(self, reader, parent=None, name=None, **kwargs):
        # Copy of UnityPy 1.25 SerializedFile.__init__; only "ReadObjects" differs.
        SF.File.File.__init__(self, parent=parent, name=name, **kwargs)
        self.reader = reader
        self.unity_version = "2.5.0f5"
        self.target_platform = SF.BuildTarget.UnknownPlatform
        self._enable_type_tree = True
        self.types = []
        self.script_types = []
        self.externals = []
        self.objects = {}
        self._cache = {}
        self.unknown = 0

        header = SF.SerializedFileHeader(reader)
        self.header = header
        if header.version >= 9:
            header.endian = ">" if reader.read_boolean() else "<"
            header.reserved = reader.read_bytes(3)
            if header.version >= 22:
                header.metadata_size = reader.read_u_int()
                header.file_size = reader.read_long()
                header.data_offset = reader.read_long()
                self.unknown = reader.read_long()
        else:
            reader.Position = header.file_size - header.metadata_size
            header.endian = ">" if reader.read_boolean() else "<"
        reader.endian = header.endian
        if header.version >= 7:
            self.set_version(reader.read_string_to_null())
        if header.version >= 8:
            self._m_target_platform = reader.read_int()
            self.target_platform = SF.BuildTarget(self._m_target_platform)
        if header.version >= 13:
            self._enable_type_tree = reader.read_boolean()

        type_count = reader.read_int()
        self.types = [SF.SerializedType(reader, self, False) for _ in range(type_count)]
        self.big_id_enabled = 0
        if 7 <= header.version < 14:
            self.big_id_enabled = reader.read_int()

        # ReadObjects
        object_count = reader.read_int()
        self.objects = {}
        table = None
        if object_count >= LAZY_MIN:
            at = reader.Position
            try:
                table = _read_table(self, reader, object_count)
            except Exception as e:
                log.debug("Compact object table failed for %s: %s", name, e)
                table = None
            if table is None:
                reader.Position = at
        if table is not None:
            self.objects = LazyObjects(self, reader, table, header.data_offset)
            log.info("%s: %s objects kept as a compact table", name, f"{object_count:,}")
        else:
            for _ in range(object_count):
                obj = SF.ObjectReader.from_reader(self, reader)
                self.objects[obj.path_id] = obj

        if header.version >= 11:
            script_count = reader.read_int()
            self.script_types = [SF.LocalSerializedObjectIdentifier(header, reader) for _ in range(script_count)]
        externals_count = reader.read_int()
        self.externals = [SF.FileIdentifier(header, reader) for _ in range(externals_count)]
        if header.version >= 20:
            ref_type_count = reader.read_int()
            self.ref_types = [SF.SerializedType(reader, self, True) for _ in range(ref_type_count)]
        if SF.config.SERIALIZED_FILE_PARSE_TYPETREE is False:
            self._enable_type_tree = False
        if header.version >= 5:
            self.userInformation = reader.read_string_to_null()

        if isinstance(self.objects, LazyObjects):
            bundles = list(self.objects.of_types(["AssetBundle"]))
        else:
            bundles = [o for o in self.objects.values() if o.type == SF.ClassIDType.AssetBundle]
        if bundles:
            self.assetbundle = bundles[0].parse_as_object()
            self._container = SF.ContainerHelper(self.assetbundle)
        else:
            self.assetbundle = None
            self._container = SF.ContainerHelper([])

    SF.SerializedFile.__init__ = __init__
    _patch_bundle_copies()


def _join_blocks(bundle, reader, blocks):
    """A bundle's blocks decompressed into one buffer. UnityPy's b"".join() of a generator holds every block
    and the joined result at once (twice the bundle's size)."""
    total = sum(b.uncompressedSize for b in blocks)
    buf = bytearray(total)
    view = memoryview(buf)
    pos = 0
    for i, block in enumerate(blocks):
        data = bundle.decompress_data(reader.read_bytes(block.compressedSize), block.uncompressedSize, block.flags, i)
        view[pos:pos + len(data)] = data
        pos += len(data)
    view.release()
    return buf if pos == total else buf[:pos]


def _node_bytes(reader, size):
    """A bundle entry's bytes: a view into the bundle's buffer instead of a copy, when there is one."""
    view = getattr(reader, "view", None)
    if view is None:
        return reader.read(size)
    start = reader.Position
    reader.Position = start + size
    return view[start:start + size]


def _patch_source(cls, method, pattern, new, helpers):
    """Recompile cls.method from its source with the one match of a regex replaced. Returns False (and changes
    nothing) when it doesn't match exactly once, e.g. another UnityPy version."""
    import inspect
    import re
    import sys
    import textwrap
    func = getattr(cls, method)
    src = textwrap.dedent(inspect.getsource(func))
    found = re.findall(pattern, src, re.S)
    if len(found) != 1:
        log.info("UnityPy %s.%s changed - leaving it as is", cls.__name__, method)
        return False
    module = sys.modules[func.__module__]
    namespace = dict(vars(module))
    namespace.update(helpers)
    patched = re.sub(pattern, lambda _m: new, src, flags=re.S)
    exec(compile(patched, f"<uniview patch {cls.__name__}.{method}>", "exec"), namespace)
    setattr(cls, method, namespace[method])
    return True


def _patch_bundle_copies():
    from UnityPy.files.BundleFile import BundleFile
    from UnityPy.files.File import File
    _patch_source(BundleFile, "read_fs",
                  r'b"".join\(\s*self\.decompress_data\(.*?for i, blockInfo in enumerate\(m_BlocksInfo\)\s*\),',
                  "_uniview_join_blocks(self, reader, m_BlocksInfo),", {"_uniview_join_blocks": _join_blocks})
    _patch_source(File, "read_files", r"EndianBinaryReader\(reader\.read\(node\.size\),",
                  "EndianBinaryReader(_uniview_node_bytes(reader, node.size),", {"_uniview_node_bytes": _node_bytes})
