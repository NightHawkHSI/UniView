"""Compact object tables for huge Unity files.

UnityPy makes one Python ObjectReader per object when it opens a serialized file. That costs ~400 bytes and a few
microseconds each, which is fine for normal games but not for one-bundle builds like PEAK (data.unity3d: 16 million
objects, ~6.6 GB of readers, mostly scene GameObjects/Transforms/colliders nobody looks at during loading).

Files with more than LAZY_MIN objects keep their object table as numpy arrays instead (~26 bytes an object), and
make ObjectReaders on demand: `file.objects[path_id]`, `.get()`, iteration, and `objects_of(env, types)` (which
filters by type in numpy before making any reader). Readers someone still holds are reused (weak cache), so the
same object keeps one reader while it's in use. Smaller files keep UnityPy's plain dict.

install() swaps in a copy of UnityPy 1.25's SerializedFile.__init__ that differs only in the "ReadObjects" step.
Other UnityPy versions are left alone (everything still works, just with the normal memory use).

Bundles: UnityPy unpacks each compressed bundle into one buffer it keeps for as long as the game is open. With a
SpillFile in use (one per open game, see use_spill), bundles of SPILL_MIN or more are unpacked into that temp file
instead and read through a memory map, so Windows pages them in from disk when needed rather than holding gigabytes
in RAM. The file is opened delete-on-close: it goes away when the game is unloaded, or when the app exits or
crashes."""

import itertools
import logging
import mmap
import os
import re
import shutil
import tempfile
import threading
import weakref
from collections.abc import MutableMapping

import numpy as np

log = logging.getLogger("uniview.unity")

LAZY_MIN = 50_000  # objects in one file before its table is kept compact
SPILL_MIN = 8 << 20  # unpacked bundles this big or bigger go to the game's temp file
SPILL_FREE = 2 << 30  # ...as long as the temp drive keeps this much free space
SPILL_DIR = os.path.join(tempfile.gettempdir(), "UniView")
_installed = False
_spill = None  # the SpillFile bundles unpack into, or None (keep them in RAM)
_spill_ids = itertools.count(1)


class SpillFile:
    """One temp file per open game (%TEMP%/UniView/Temp<Game>-<pid>-<n>.bin) holding its unpacked bundles."""

    def __init__(self, name):
        os.makedirs(SPILL_DIR, exist_ok=True)
        _remove_stale()
        safe = re.sub(r"[^\w.-]+", "_", name).strip("._")[:60] or "Game"
        self.path = os.path.join(SPILL_DIR, f"Temp{safe}-{os.getpid()}-{next(_spill_ids)}.bin")
        temporary = getattr(os, "O_TEMPORARY", 0)  # Windows: deleted once the last handle/mapping closes
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | temporary, 0o600)
        if not temporary:
            os.unlink(self.path)  # elsewhere: out of the folder now, space freed when closed
        self.size = 0          # bytes used (end of the last bundle)
        self.maps = []
        self.lock = threading.Lock()
        self.closed = False

    def store(self, total, chunks):
        """Write `chunks` (an iterable of bytes, `total` long) to the file; a memoryview of them, or None if the
        temp drive is short on space or the write fails (nothing is left half-used: the caller keeps it in RAM)."""
        with self.lock:
            if self.closed:
                return None
            try:
                if shutil.disk_usage(SPILL_DIR).free < total + SPILL_FREE:
                    return None
            except OSError:
                return None
            grain = mmap.ALLOCATIONGRANULARITY
            start = -(-self.size // grain) * grain  # map offsets must be multiples of this
            pos = start
            try:
                for data in chunks:
                    view = memoryview(data)
                    while view.nbytes:
                        written = os.pwrite(self.fd, view, pos) if hasattr(os, "pwrite") else self._write_at(view, pos)
                        view, pos = view[written:], pos + written
                length = pos - start
                if length == 0:
                    return memoryview(b"")
                # read-only: a copy-on-write map would be charged to RAM up front, which is what this avoids
                mapped = mmap.mmap(self.fd, length, access=mmap.ACCESS_READ, offset=start)
            except (OSError, ValueError) as e:
                log.warning("Couldn't unpack a bundle to the temp file (%s) - keeping it in memory", e)
                return None
            self.size = pos
            self.maps.append(weakref.ref(mapped))
            return memoryview(mapped)

    def _write_at(self, view, pos):
        os.lseek(self.fd, pos, os.SEEK_SET)
        return os.write(self.fd, view)

    def close(self):
        """Done with this game. Maps still in use close when the last reader lets go; the file is deleted then."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for ref in self.maps:
                mapped = ref()
                if mapped is not None:
                    try:
                        mapped.close()
                    except BufferError:
                        pass  # something still reads it; it closes when that's garbage
            self.maps = []
            try:
                os.close(self.fd)
            except OSError:
                pass
        log.info("Closed temp file %s (%.0f MB of unpacked bundles)", os.path.basename(self.path), self.size / 1e6)


def _remove_stale():
    """Leftovers from a run that didn't close cleanly (power cut). Files still open can't be deleted - skip those."""
    try:
        names = os.listdir(SPILL_DIR)
    except OSError:
        return
    for name in names:
        if name.startswith("Temp") and name.endswith(".bin"):
            try:
                os.remove(os.path.join(SPILL_DIR, name))
            except OSError:
                pass


def use_spill(spill):
    """Unpack bundles into `spill` (a SpillFile) from now on; None = keep them in RAM."""
    global _spill
    _spill = spill


def release_spill(spill):
    """The game owning `spill` is closing."""
    global _spill
    if _spill is spill:
        _spill = None
    if spill is not None:
        spill.close()


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
    spill = _spill
    if spill is not None and total >= SPILL_MIN:
        start = reader.Position
        chunks = (bundle.decompress_data(reader.read_bytes(b.compressedSize), b.uncompressedSize, b.flags, i)
                  for i, b in enumerate(blocks))
        view = spill.store(total, chunks)
        if view is not None:
            return view
        reader.Position = start  # fall back to RAM: read the blocks again
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
