"""Unreal Engine 4/5 plugin.

Reads .pak archives (versions 3-12) and IoStore containers (.utoc/.ucas), including encrypted
ones when you give the game's AES key (right-click the game -> Engine settings...).
Compression: zlib, gzip, LZ4 and Oodle (Oodle needs an oo2core_*_win64.dll - many games ship
one; UniView looks in the game folder, next to UniView and in your Steam libraries).

Packages (.uasset/.umap) are sorted by the class of what they contain (read from the package
headers and cached): textures (incl. virtual textures) and static/skeletal meshes preview and
export; other classes (materials, blueprints, sounds...) export as raw files. Loose images and
text files (ini, json, csv...) preview too.
"""

import ctypes
import glob
import hashlib
import json
import logging
import os
import re
import struct
import threading
import time
import zlib

from . import iostore as io
from . import ue_package as up
from .aes import AES, parse_key
from .sdk import (
    ALBEDO, NORMAL, Asset, EnginePlugin, GameSession, Material, TextureRef, cache_dir, kind_for_extension,
    pil_image_from_bytes,
)

log = logging.getLogger("viewer.unreal")

PAK_MAGIC = 0x5A6F12E1
PAK_VERSIONS = {1: "UE4 (early)", 2: "UE4 (early)", 3: "UE 4.0-4.2", 4: "UE 4.3-4.15", 5: "UE 4.16-4.19",
                6: "UE 4.20", 7: "UE 4.21", 8: "UE 4.22-4.24", 9: "UE 4.25", 10: "UE 4.25",
                11: "UE 4.26-5.2", 12: "UE 5.3+"}
LEGACY_COMPRESSION = {0: "", 1: "zlib", 2: "gzip", 4: "oodle"}
PACKAGE_EXTS = (".uasset", ".umap")
LOOSE_EXTS = {"bank", "bnk", "wem", "fsb", "mp4", "bk2", "webm", "wav", "ogg", "mp3", "png", "jpg", "json", "txt",
              "ini", "csv", "xml"}
COMPANION_EXTS = (".uexp", ".ubulk", ".uptnl", ".m.ubulk")
CLASS_KINDS = {**{c: "texture" for c in up.TEXTURE_CLASSES}, **{c: "model" for c in up.MESH_CLASSES},
               "SoundWave": "audio"}
# Naming conventions, used when a package header can't be read.
PREFIX_CLASSES = (("t_", "Texture2D"), ("tx_", "Texture2D"), ("tex_", "Texture2D"), ("sm_", "StaticMesh"),
                  ("s_", "StaticMesh"), ("sk_", "SkeletalMesh"), ("skm_", "SkeletalMesh"),
                  ("m_", "Material"), ("mi_", "MaterialInstanceConstant"), ("bp_", "BlueprintGeneratedClass"))
CLASSIFY_CACHE_VERSION = 1


# --------------------------------------------------------------------------- Oodle (optional)

_oodle = None
_oodle_lock = threading.Lock()


def steam_commons():
    steam = r"C:\Program Files (x86)\Steam"
    libs = [steam]
    try:
        with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    return [os.path.join(lib, "steamapps", "common") for lib in dict.fromkeys(libs)
            if os.path.isdir(os.path.join(lib, "steamapps", "common"))]


def find_oodle(hint_dirs=()):
    """Path of an oo2core_*.dll: game folder, UniView folder/plugins, then Steam libraries."""
    import sys
    patterns = []
    for base in hint_dirs:
        patterns += [os.path.join(base, "oo2core_*_win64.dll"), os.path.join(base, "*", "oo2core_*_win64.dll"),
                     os.path.join(base, "*", "*", "Binaries", "Win64", "oo2core_*_win64.dll"),
                     os.path.join(base, "Engine", "Binaries", "ThirdParty", "Oodle", "*", "oo2core_*_win64.dll")]
    app = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    patterns += [os.path.join(app, "oo2core_*_win64.dll"), os.path.join(app, "plugins", "oo2core_*_win64.dll")]
    for pattern in patterns:
        found = sorted(glob.glob(pattern), reverse=True)
        if found:
            return found[0]
    for common in steam_commons():
        found = sorted(glob.glob(os.path.join(common, "*", "oo2core_*_win64.dll")), reverse=True)
        if found:
            return found[0]
    return None


def oodle(hint_dirs=()):
    """ctypes OodleLZ_Decompress, or None if no DLL can be found."""
    global _oodle
    with _oodle_lock:
        if _oodle is None:
            path = find_oodle(hint_dirs)
            _oodle = False
            if path:
                try:
                    fn = ctypes.WinDLL(path).OodleLZ_Decompress
                    fn.restype = ctypes.c_int64
                    fn.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int64,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int64,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]
                    _oodle = fn
                    log.info("Using Oodle from %s", path)
                except OSError as e:
                    log.warning("Could not load %s: %s", path, e)
        return _oodle or None


def oodle_decompress(data, size, hint_dirs=()):
    fn = oodle(hint_dirs)
    if fn is None:
        raise RuntimeError("This file is Oodle-compressed and no oo2core_*_win64.dll was found. "
                           "Copy one from a game that ships it next to UniView.exe.")
    src = ctypes.create_string_buffer(bytes(data), len(data))
    dst = ctypes.create_string_buffer(size)
    got = fn(src, len(data), dst, size, 1, 0, 0, None, 0, None, None, None, 0, 3)
    if got != size:
        raise RuntimeError(f"Oodle decompression failed ({got} of {size} bytes)")
    return dst.raw


def decompress(method, data, size, hint_dirs=()):
    if method == "zlib":
        return zlib.decompress(data)
    if method == "gzip":
        return zlib.decompress(data, 16 + zlib.MAX_WBITS)
    if method == "lz4":
        import lz4.block
        return lz4.block.decompress(data, uncompressed_size=size)
    if method.startswith("oodle"):
        return oodle_decompress(data, size, hint_dirs)
    raise RuntimeError(f"Compression '{method}' isn't supported")


def parse_keys(text):
    """AES keys from the user's text (one per line / comma separated, hex or base64)."""
    keys = []
    for part in re.split(r"[\s,;]+", text or ""):
        part = part.strip().strip('"')
        if not part:
            continue
        try:
            keys.append(AES(parse_key(part)))
        except ValueError as e:
            log.warning("Ignoring AES key %s...: %s", part[:10], e)
    return keys


# --------------------------------------------------------------------------- pak reading

class PakEntry:
    __slots__ = ("path", "offset", "size", "usize", "method", "encrypted", "blocks", "block_size", "header")

    def __init__(self, path, offset, size, usize, method, encrypted, blocks, block_size, header):
        self.path, self.offset, self.size, self.usize = path, offset, size, usize
        self.method, self.encrypted, self.blocks, self.block_size = method, encrypted, blocks, block_size
        self.header = header  # size of the entry record in front of the data


class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def take(self, fmt):
        value = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += struct.calcsize("<" + fmt)
        return value if len(value) > 1 else value[0]

    def bytes(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def fstring(self):
        n = self.take("i")
        if n == 0:
            return ""
        if n < 0:
            if -n > 65536:
                raise ValueError("bad string")
            return self.bytes(-n * 2).decode("utf-16-le", "replace").rstrip("\0")
        if n > 65536:
            raise ValueError("bad string")
        return self.bytes(n).decode("utf-8", "replace").rstrip("\0")


def read_footer(f):
    f.seek(0, 2)
    size = f.tell()
    f.seek(max(0, size - 512))
    tail = f.read()
    i = tail.rfind(struct.pack("<I", PAK_MAGIC))
    if i < 0:
        raise ValueError("Not an Unreal .pak file (no footer)")
    version, index_offset, index_size = struct.unpack_from("<iqq", tail, i + 4)
    encrypted = tail[i - 1] == 1 if version >= 4 and i >= 1 else False
    names = []
    rest = tail[i + 44 + (1 if version == 9 else 0):]
    for j in range(0, len(rest) - 31, 32):
        name = rest[j:j + 32].split(b"\0")[0]
        # Some games customize the footer; only keep names that look like method names.
        names.append(name.decode("ascii").lower() if name.isascii() and name.isalnum() else "")
    return {"version": version, "index_offset": index_offset, "index_size": index_size,
            "encrypted_index": encrypted, "methods": names}


def pak_summary(path):
    with open(path, "rb") as f:
        return read_footer(f)


class Pak:
    def __init__(self, path, hint_dirs=(), keys=()):
        self.path = path
        self.hint_dirs = hint_dirs
        self.entries = []
        self.aes = None
        self._f = open(path, "rb")
        self._lock = threading.Lock()
        footer = read_footer(self._f)
        self.version = footer["version"]
        self.methods = footer["methods"]
        self.encrypted_index = footer["encrypted_index"]
        self._f.seek(footer["index_offset"])
        raw_index = self._f.read(footer["index_size"])
        candidates = [None] if not self.encrypted_index else list(keys)
        if not candidates:
            raise PermissionError("the file list is encrypted (needs the game's AES key)")
        last_error = None
        for key in candidates:
            try:
                self.aes = key
                index = key.decrypt(raw_index[:len(raw_index) // 16 * 16]) if key else raw_index
                self.entries = []
                if self.version >= 10:
                    self._read_index_v10(index)
                else:
                    self._read_index_legacy(index)
                return
            except Exception as e:
                last_error = e
        if self.encrypted_index:
            raise PermissionError("none of the AES keys opens this file (wrong key?)")
        raise last_error

    def _decrypt(self, data):
        if self.aes is None:
            raise PermissionError("This file is encrypted (needs the game's AES key).")
        return self.aes.decrypt(data)

    def _method(self, index):
        if self.version < 8:
            return LEGACY_COMPRESSION.get(index, f"method {index}")
        if index == 0:
            return ""
        return self.methods[index - 1] if index - 1 < len(self.methods) else f"method {index}"

    @staticmethod
    def _mount(mount):
        mount = mount.replace("\\", "/")
        while mount.startswith("../"):
            mount = mount[3:]
        return mount.lstrip("/")

    def _entry_size(self, method_index, n_blocks):
        size = 8 + 8 + 8 + 20 + 4
        if self.version >= 3:
            size += 1 + 4
            if method_index:
                size += 4 + 16 * n_blocks
        if self.version < 2:
            size += 8
        return size

    def _read_entry(self, r, path):
        """A full (unencoded) FPakEntry record."""
        start = r.pos
        offset, size, usize = r.take("qqq")
        method_index = r.take("I") if self.version >= 8 else r.take("i")
        if self.version < 2:
            r.take("q")  # timestamp
        r.bytes(20)
        blocks, encrypted, block_size = [], False, 0
        if self.version >= 3:
            if method_index:
                n = r.take("i")
                blocks = [r.take("qq") for _ in range(n)]
            encrypted = bool(r.take("B") & 1)
            block_size = r.take("I")
        header = r.pos - start
        if self.version >= 5:
            blocks = [(s + offset, e + offset) for s, e in blocks]
        return PakEntry(path, offset, size, usize, self._method(method_index), encrypted, blocks, block_size, header)

    def _read_index_legacy(self, data):
        r = Reader(data)
        mount = self._mount(r.fstring())
        count = r.take("i")
        if not 0 <= count <= 10_000_000:
            raise ValueError("bad pak index")
        for _ in range(count):
            name = r.fstring()
            self.entries.append(self._read_entry(r, mount + name))

    def _decode_entry(self, data, pos, path):
        r = Reader(data, pos)
        value = r.take("I")
        block_size = r.take("I") if (value & 0x3F) == 0x3F else (value & 0x3F) << 11
        method_index = (value >> 23) & 0x3F
        offset = r.take("I") if value & (1 << 31) else r.take("Q")
        usize = r.take("I") if value & (1 << 30) else r.take("Q")
        size = (r.take("I") if value & (1 << 29) else r.take("Q")) if method_index else usize
        encrypted = bool(value & (1 << 22))
        n_blocks = (value >> 6) & 0xFFFF
        header = self._entry_size(method_index, n_blocks)
        blocks = []
        if n_blocks == 1 and not encrypted:
            blocks = [(offset + header, offset + header + size)]
        elif n_blocks:
            cursor = offset + header
            for _ in range(n_blocks):
                length = r.take("I")
                blocks.append((cursor, cursor + length))
                cursor += (length + 15) // 16 * 16 if encrypted else length
        return PakEntry(path, offset, size, usize, self._method(method_index), encrypted, blocks,
                        min(block_size, usize) if block_size else usize, header)

    def _read_index_v10(self, data):
        r = Reader(data)
        mount = self._mount(r.fstring())
        r.take("i")  # entry count (the directory index below has them all)
        r.take("Q")  # path hash seed
        if r.take("I"):
            r.take("qq")
            r.bytes(20)
        has_full = r.take("I")
        full_offset = full_size = 0
        if has_full:
            full_offset, full_size = r.take("qq")
            r.bytes(20)
        encoded_size = r.take("i")
        if not 0 <= encoded_size <= len(data):
            raise ValueError("bad pak index")
        encoded = r.bytes(encoded_size)
        n_files = r.take("i")
        files = [self._read_entry(r, "") for _ in range(n_files)]
        if not has_full:
            raise ValueError("pak has no full directory index (file names were stripped)")
        self._f.seek(full_offset)
        raw = self._f.read(full_size)
        if self.aes is not None:
            raw = self.aes.decrypt(raw[:len(raw) // 16 * 16])
        d = Reader(raw)
        for _ in range(d.take("i")):
            folder = d.fstring()
            for _ in range(d.take("i")):
                name = d.fstring()
                location = d.take("i")
                path = (mount + folder.lstrip("/") + name).replace("//", "/")
                if location >= 0:
                    self.entries.append(self._decode_entry(encoded, location, path))
                elif -location - 1 < len(files):
                    entry = files[-location - 1]
                    entry.path = path
                    self.entries.append(entry)

    def read(self, entry):
        with self._lock:
            if not entry.method:
                self._f.seek(entry.offset + entry.header)
                if entry.encrypted:
                    return self._decrypt(self._f.read((entry.size + 15) // 16 * 16))[:entry.size]
                return self._f.read(entry.size)
            chunks = []
            for start, end in entry.blocks:
                self._f.seek(start)
                length = end - start
                if entry.encrypted:
                    chunks.append(self._decrypt(self._f.read((length + 15) // 16 * 16))[:length])
                else:
                    chunks.append(self._f.read(length))
        out = bytearray()
        remaining = entry.usize
        for chunk in chunks:
            want = min(entry.block_size or remaining, remaining)
            out += decompress(entry.method, chunk, want, self.hint_dirs)
            remaining -= want
        return bytes(out)

    def close(self):
        with self._lock:
            self._f.close()


# --------------------------------------------------------------------------- game layout

def find_paks(path):
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".pak") else []
    found = []
    for pattern in ("*.pak", os.path.join("*", "Content", "Paks", "**", "*.pak"),
                    os.path.join("Content", "Paks", "**", "*.pak")):
        found += glob.glob(os.path.join(path, pattern), recursive=True)
    return sorted(set(found))


def find_utocs(path):
    if os.path.isfile(path):
        return [path] if path.lower().endswith(".utoc") else []
    found = []
    for pattern in ("*.utoc", os.path.join("*", "Content", "Paks", "**", "*.utoc"),
                    os.path.join("Content", "Paks", "**", "*.utoc")):
        found += glob.glob(os.path.join(path, pattern), recursive=True)
    return sorted(set(found))


def guess_class(path):
    base = os.path.basename(path).lower()
    for prefix, cls in PREFIX_CLASSES:
        if base.startswith(prefix):
            return cls
    return ""


class UFile:
    """One file in the merged game tree: from a pak entry or an IoStore chunk."""

    __slots__ = ("path", "size", "pak", "entry", "store", "chunk", "disk")

    def __init__(self, path, size, pak=None, entry=None, store=None, chunk=None, disk=None):
        self.path, self.size, self.pak, self.entry, self.store, self.chunk = path, size, pak, entry, store, chunk
        self.disk = disk  # loose file on disk

    @property
    def container(self):
        if self.disk:
            return "loose file"
        return os.path.basename(self.pak.path if self.pak else self.store.utoc_path)

    def read(self, start=0, length=None):
        if self.disk:
            with open(self.disk, "rb") as f:
                f.seek(start)
                return f.read() if length is None else f.read(length)
        if self.store is not None:
            return self.store.read(self.chunk, start, length)
        data = self.pak.read(self.entry)
        return data[start:] if length is None else data[start:start + length]


class UnrealSession(GameSession):
    def __init__(self, plugin, path):
        super().__init__(plugin, path)
        self.paks = []
        self.stores = []
        self.files = {}          # lowercase path -> UFile
        self.script_objects = {}
        self.classes = {}        # package key -> class name
        self._textures_by_folder = None
        self._headers = {}
        self._logical = None
        self._by_key = {}

    def close(self):
        for pak in self.paks:
            pak.close()
        for store in self.stores:
            store.close()

    def add(self, path, ufile):
        self.files.setdefault(path.lower(), ufile)

    def raw(self, asset):
        return self.files[asset.ref].read()

    # ---- packages
    def _package(self, key):
        """(export data, PackageInfo or None, bulk reader) for a .uasset/.umap."""
        data = self.files[key].read()
        base = key.rsplit(".", 1)[0]
        info = None
        try:
            info = up.package_info(data, self.script_objects)
        except Exception as e:
            log.debug("Couldn't read the header of %s: %s", key, e)
        if info is not None and info.zen:
            body = data[info.data_offset:]
            combined = data
        else:
            uexp = self.files.get(base + ".uexp")
            body = uexp.read() if uexp else data
            combined = data + body if uexp else data

        def read_bulk(flags, offset, size):
            if flags & up.BULK_SEPARATE_FILE or info is None or info.zen:
                exts = [".uptnl"] if flags & up.BULK_OPTIONAL else [".m.ubulk"] if flags & up.BULK_MEMORY_MAPPED else []
                for ext in exts + [".ubulk"]:
                    f = self.files.get(base + ext)
                    if f is not None:
                        return f.read(offset, size)
                raise FileNotFoundError("the texture's .ubulk data isn't in the game files")
            if 0 <= offset and offset + size <= len(combined):
                return combined[offset:offset + size]
            return body[offset:offset + size]

        return body, info, read_bulk

    def _bulk_buffers(self, key, body):
        buffers = [body]
        f = self.files.get(key.rsplit(".", 1)[0] + ".ubulk")
        if f is not None:
            buffers.append(f.read())
        return buffers

    def image(self, asset):
        if asset.ref.endswith(PACKAGE_EXTS):
            body, info, read_bulk = self._package(asset.ref)
            return up.texture_image(body, info, read_bulk)
        return pil_image_from_bytes(self.raw(asset))

    def audio(self, asset):
        if not asset.ref.endswith(PACKAGE_EXTS):
            if asset.ext == "bnk":
                data = self.raw(asset)
                if b"DIDX" not in data[:65536]:
                    raise NotImplementedError("This Wwise bank has no sound inside - it only points to separate "
                                              ".wem files (they are in the Audio list).")
            return super().audio(asset)  # loose files (.wem/.bnk/.bank via vgmstream when installed)
        body, info, _rb = self._package(asset.ref)
        for buf in self._bulk_buffers(asset.ref, body):
            at = buf.find(b"OggS")
            if at >= 0:
                return buf[at:], "ogg"
            at = buf.find(b"RIFF")
            if at >= 0 and buf[at + 8:at + 12] == b"WAVE":
                size = struct.unpack_from("<I", buf, at + 4)[0]
                return buf[at:at + 8 + size], "wav"
            at = buf.find(b"fLaC")
            if at >= 0:
                return buf[at:], "flac"
        names = set(info.names) if info else set()
        codec = next((c for c in ("BINKA", "OPUS", "ADPCM", "PLATFORMSPECIFIC") if c in {n.upper() for n in names}), "")
        if codec == "BINKA":
            raise NotImplementedError("This sound uses Bink Audio (UE5's default codec), which can't be played yet.")
        raise NotImplementedError(f"This sound's format ({codec or 'unknown'}) can't be played yet.")

    def mesh(self, asset):
        body, _info, _rb = self._package(asset.ref)
        return up.mesh_data(self._bulk_buffers(asset.ref, body), name=os.path.basename(asset.name))

    ALBEDO_SUFFIXES = ("_bc", "_d", "_base", "_basecolor", "_base_color", "_diffuse", "_albedo", "_color", "_col", "_c", "_a")
    NORMAL_SUFFIXES = ("_n", "_normal", "_nrm", "_nm", "_norm")
    ALBEDO_WORDS = ("basecolor", "base_color", "albedo", "diffuse", "_bc", "_d_", "color")
    NORMAL_WORDS = ("normal", "_nrm", "_n_")
    MESH_PREFIXES = ("sm_", "sk_", "skm_", "s_", "mesh_", "sm", "sk")
    MATERIAL_CLASSES = ("Material", "MaterialInstanceConstant", "MaterialInstance", "MaterialInstanceDynamic")

    def _header(self, key):
        """PackageInfo from a package's header only (cached), or None."""
        if key in self._headers:
            return self._headers[key]
        info = None
        try:
            f = self.files[key]
            if f.store is not None:
                head = f.read(0, min(f.size, 16384))
                if len(head) >= 8 and up.zen_header_size(head) > len(head):
                    head = f.read(0, up.zen_header_size(head))
            else:
                head = f.read()
            info = up.package_info(head, self.script_objects)
        except Exception as e:
            log.debug("No header for %s: %s", key, e)
        self._headers[key] = info
        return info

    def _resolve(self, package_name):
        """'/Game/Foo/MI_Bar' -> the package's key in the game tree, or None."""
        if self._logical is None:
            self._logical = {}
            for key in self.files:
                if not key.endswith(PACKAGE_EXTS):
                    continue
                parts = key.rsplit(".", 1)[0].split("/")
                if "content" not in parts:
                    continue
                c = parts.index("content")
                rest = "/".join(parts[c + 1:])
                if c >= 1:
                    self._logical.setdefault(f"/{parts[c - 1]}/{rest}", key)  # plugin or /engine mount
                if c == 1 and parts[0] != "engine":
                    self._logical.setdefault(f"/game/{rest}", key)
        return self._logical.get(package_name.lower())

    @classmethod
    def _role(cls, name):
        low = name.lower()
        if low.endswith(cls.NORMAL_SUFFIXES) or any(w in low for w in cls.NORMAL_WORDS):
            return NORMAL
        if low.endswith(cls.ALBEDO_SUFFIXES) or any(w in low for w in cls.ALBEDO_WORDS):
            return ALBEDO
        return "other"

    def materials(self, asset):
        """Materials a model uses and their textures, followed through the package imports
        (mesh -> material instance -> parent material -> textures). Games whose headers don't name
        their imports (UE5.0-5.2 IoStore) fall back to textures named after the model."""
        info = self._header(asset.ref)
        out = []
        for package in (info.imported_packages if info else []):
            key = self._resolve(package)
            if key is None:
                continue
            cls = self.classes.get(key, "")
            if cls in self.MATERIAL_CLASSES or cls.startswith("Material"):
                textures, seen, todo = [], set(), [key]
                while todo and len(seen) < 6:  # material instance -> parent material ...
                    mk = todo.pop(0)
                    if mk in seen:
                        continue
                    seen.add(mk)
                    minfo = self._header(mk)
                    for dep in (minfo.imported_packages if minfo else []):
                        dk = self._resolve(dep)
                        if dk is None:
                            continue
                        dcls = self.classes.get(dk, "")
                        if CLASS_KINDS.get(dcls) == "texture" and dk not in {t.asset.ref for t in textures}:
                            tex = self._by_key.get(dk)
                            if tex is not None:
                                name = os.path.basename(tex.name)
                                textures.append(TextureRef(dep.rsplit("/", 1)[-1], name, tex, self._role(name)))
                        elif dcls.startswith("Material") and dk not in seen:
                            todo.append(dk)
                textures.sort(key=lambda t: (t.role != ALBEDO, t.role == NORMAL))
                out.append(Material(package.rsplit("/", 1)[-1], textures))
            elif CLASS_KINDS.get(cls) == "texture" and key in self._by_key:
                tex = self._by_key[key]
                name = os.path.basename(tex.name)
                out.append(Material(name, [TextureRef("texture", name, tex, self._role(name))]))
        if any(m.textures for m in out):
            return out
        return self._materials_by_name(asset) or out

    def _materials_by_name(self, asset):
        """Textures named after the model (SM_Name -> T_Name_BC) in its folder or a Textures folder."""
        if self._textures_by_folder is None:
            self._textures_by_folder = {}
            for a in self.assets:
                if a.kind == "texture" and a.ref.endswith(PACKAGE_EXTS):
                    self._textures_by_folder.setdefault(a.ref.rsplit("/", 1)[0], []).append(a)
        folder, base = asset.ref.rsplit("/", 1)
        stem = base.rsplit(".", 1)[0]
        for prefix in self.MESH_PREFIXES:
            if stem.startswith(prefix) and len(stem) > len(prefix) + 2:
                stem = stem[len(prefix):].lstrip("_")
                break
        parent = folder.rsplit("/", 1)[0]
        folders = [folder, parent + "/textures", folder + "/textures", parent + "/texture", parent]
        albedo = normal = None
        for f in folders:
            for tex in self._textures_by_folder.get(f, []):
                name = tex.ref.rsplit("/", 1)[1].rsplit(".", 1)[0]
                if stem not in name:
                    continue
                if albedo is None and name.endswith(self.ALBEDO_SUFFIXES):
                    albedo = tex
                elif normal is None and name.endswith(self.NORMAL_SUFFIXES):
                    normal = tex
            if albedo is not None:
                break
        refs = []
        if albedo is not None:
            refs.append(TextureRef("BaseColor (by name)", os.path.basename(albedo.name), albedo, ALBEDO))
        if normal is not None:
            refs.append(TextureRef("Normal (by name)", os.path.basename(normal.name), normal, NORMAL))
        return [Material(stem, refs)] if refs else []

    def stats(self, asset):
        stats = {"size": asset.size, "info": self.classes.get(asset.ref, ""), "sort": asset.size or 0}
        if asset.kind == "texture" and asset.ref.endswith(PACKAGE_EXTS):
            body, info, _rb = self._package(asset.ref)
            found = up.texture_size(body, info)
            if found:
                fmt, w, h = found
                stats.update(w=w, h=h, info=f"{w}\u00d7{h} {fmt[3:]}", sort=w * h)
        elif asset.kind == "model":
            md = self.mesh(asset)
            tris, verts = md.triangle_count, len(md.points)
            stats.update(tris=tris, verts=verts, info=f"{tris:,} tris", sort=tris)
        return stats

    def describe(self, asset):
        f = self.files[asset.ref]
        rows = [("File", f.container), ("Path", f.path)]
        cls = self.classes.get(asset.ref)
        if cls:
            rows.append(("Class", cls))
        if f.pak is not None:
            e = f.entry
            rows.append(("Stored", f"{e.method or 'uncompressed'}, {e.size:,} bytes" + (" (encrypted)" if e.encrypted else "")))
        if asset.ref.endswith(PACKAGE_EXTS):
            try:
                _body, info, _rb = self._package(asset.ref)
                if info is not None and info.exports:
                    shown = ", ".join(f"{n} ({c})" if c else n for n, c in info.exports[:8])
                    rows.append((f"Exports ({len(info.exports)})", shown + (" ..." if len(info.exports) > 8 else "")))
            except Exception:
                pass
        return rows


class UnrealPlugin(EnginePlugin):
    id = "unreal"
    name = "Unreal"
    version = "2.0"
    author = "UniView"
    description = ("Unreal Engine 4/5 games: .pak and IoStore (.utoc/.ucas) containers, encrypted ones with the "
                   "game's AES key; textures (incl. virtual textures), static and skeletal meshes.")
    options = [{"id": "aes_keys", "label": "AES key(s)", "multiline": True,
                "help": "Only needed for encrypted games. The 64-digit hex key (0x...), one per line if the "
                        "game has several. Community sites list keys for many games."}]

    def detect(self, path):
        if os.path.isfile(path):
            return 90 if path.lower().endswith((".pak", ".utoc")) else 0
        return 90 if find_paks(path) or find_utocs(path) else 0

    def game_info(self, path):
        paks = find_paks(path)
        parts, version = [], ""
        for pak in paks[:1] + [p for p in paks[1:] if "crash" not in p.lower()][:1]:
            try:
                footer = pak_summary(pak)
            except Exception:
                continue
            version = PAK_VERSIONS.get(footer["version"], f"pak v{footer['version']}")
            parts = [f"pak v{footer['version']}"]
            if footer["encrypted_index"]:
                parts.append("encrypted")
            methods = [m for m in footer["methods"] if m]
            if methods:
                parts.append("/".join(m.capitalize() for m in methods))
        if find_utocs(path):
            parts.append("IoStore")
        return {"engine_version": version, "detail": " \u00b7 ".join(parts)}

    def label(self, info):
        return info.get("engine_version") or "Unreal"

    def count_files(self, path):
        return len(find_paks(path)) + len([u for u in find_utocs(path) if os.path.basename(u) != "global.utoc"])

    # ---- loading
    def open(self, path, progress):
        root = path if os.path.isdir(path) else os.path.dirname(path)
        keys = parse_keys(progress.options.get("aes_keys", ""))
        pak_files, utoc_files = find_paks(path), find_utocs(path)
        if not pak_files and not utoc_files:
            raise FileNotFoundError(f"No .pak or .utoc files found in:\n{path}")
        session = UnrealSession(self, path)
        hint = (root,)
        encrypted = failed = 0
        total = len(pak_files) + len(utoc_files)

        def dec(method, data, size):
            return decompress(method, data, size, hint)

        # IoStore first: in games that have both, the .pak next to a .utoc only holds loose files.
        for n, utoc in enumerate(utoc_files, 1):
            progress(f"Reading {os.path.basename(utoc)} ({n}/{total})", n - 1, total)
            store, error = None, None
            for key in [None] + keys:
                try:
                    store = io.IoStore(utoc, dec, key)
                    break
                except PermissionError as e:
                    error = e
                except Exception as e:  # a wrong key gives a garbage file list
                    error = e
                    if key is None:
                        break
            if store is None:
                if isinstance(error, PermissionError) or keys:
                    encrypted += 1
                    log.warning("Skipped %s: %s", os.path.basename(utoc),
                                "none of the AES keys opens it" if keys else "encrypted (needs the game's AES key)")
                else:
                    failed += 1
                    log.warning("Could not read %s: %s: %s", os.path.basename(utoc), type(error).__name__, error)
                continue
            if store.encrypted and store.aes is None and keys:
                store.aes = keys[0]
            if os.path.basename(utoc).lower() == "global.utoc":
                chunk = store.chunk_of_type(io.CHUNK_SCRIPT_OBJECTS)
                if chunk is not None:
                    try:
                        session.script_objects.update(up.read_script_objects(store.read(chunk)))
                    except Exception as e:
                        log.warning("Couldn't read the script object names: %s", e)
                session.stores.append(store)
                continue
            session.stores.append(store)
            for key, chunk in store.files.items():
                session.add(chunk.path, UFile(chunk.path, chunk.length, store=store, chunk=chunk))
            log.info("Read %s: IoStore v%d, %d files", os.path.basename(utoc), store.version, len(store.files))
        for n, pak_path in enumerate(pak_files, len(utoc_files) + 1):
            progress(f"Reading {os.path.basename(pak_path)} ({n}/{total})", n - 1, total)
            try:
                pak = Pak(pak_path, hint_dirs=hint, keys=keys)
            except PermissionError as e:
                encrypted += 1
                log.warning("Skipped %s: %s", os.path.basename(pak_path), e)
                continue
            except Exception as e:
                failed += 1
                log.warning("Could not read %s: %s: %s", os.path.basename(pak_path), type(e).__name__, e)
                continue
            session.paks.append(pak)
            for entry in pak.entries:
                session.add(entry.path, UFile(entry.path, entry.usize, pak=pak, entry=entry))
            log.info("Read %s: pak v%d, %d files", os.path.basename(pak_path), pak.version, len(pak.entries))
        # Loose files games keep next to their paks (FMOD banks, Wwise sounds, movies...).
        for content in glob.glob(os.path.join(root, "*", "Content")) + glob.glob(os.path.join(root, "Content")):
            for dirpath, dirs, names in os.walk(content):
                dirs[:] = [d for d in dirs if d.lower() != "paks"]
                for fname in names:
                    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    if ext in LOOSE_EXTS:
                        full = os.path.join(dirpath, fname)
                        rel = os.path.relpath(full, root).replace("\\", "/")
                        session.add(rel, UFile(rel, os.path.getsize(full), disk=full))
        session.file_count = len(session.paks) + len([s for s in session.stores
                                                     if os.path.basename(s.utoc_path).lower() != "global.utoc"])

        self._classify(session, progress, pak_files + utoc_files)
        for key, f in session.files.items():
            low = key
            if low.endswith(COMPANION_EXTS):
                continue
            base = f.path.rsplit("/", 1)[-1]
            ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
            if low.endswith(PACKAGE_EXTS):
                cls = session.classes.get(key, "")
                kind = CLASS_KINDS.get(cls, "file")
                name = f.path.rsplit(".", 1)[0].replace("/Content/", "/")
            else:
                kind, name = kind_for_extension(ext), f.path
            asset = Asset(kind, name, key, uid=key, size=f.size, path=f.path, source=f.container,
                          ref=key, ext=ext or "bin")
            session.assets.append(asset)
            session._by_key[key] = asset
        if encrypted:
            session.warnings.append(
                f"{encrypted} of {total} container(s) are encrypted and were skipped. Right-click the game on the "
                "Projects page \u2192 Engine settings... and enter the game's AES key.")
        if failed:
            session.warnings.append(f"{failed} container(s) could not be read (see the console).")
        if not session.assets:
            raise RuntimeError("Nothing could be listed from this game's files:\n" + "\n".join(session.warnings))
        return session

    # ---- classification (which class each package holds), cached per game
    def _cache_file(self, containers):
        sig = hashlib.sha1()
        for p in sorted(containers):
            try:
                st = os.stat(p)
                sig.update(f"{p}|{st.st_size}|{int(st.st_mtime)}".encode())
            except OSError:
                pass
        return os.path.join(cache_dir(), f"unreal_{sig.hexdigest()[:16]}.json")

    def _classify(self, session, progress, containers):
        packages = [k for k in session.files if k.endswith(PACKAGE_EXTS)]
        if not packages:
            return
        cache_path = self._cache_file(containers)
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("version") == CLASSIFY_CACHE_VERSION:
                session.classes = cached["classes"]
                if all(k in session.classes for k in packages[:200]):
                    log.info("Package classes loaded from cache (%d)", len(session.classes))
                    return
        except (OSError, ValueError, KeyError):
            pass
        started = time.time()

        def order(k):
            f = session.files[k]
            return (id(f.store) if f.store else id(f.pak), f.chunk.offset if f.chunk else f.entry.offset)

        packages.sort(key=order)  # read in file order: much faster on hard drives
        failed = 0
        for n, key in enumerate(packages):
            if n % 500 == 0:
                progress(f"Sorting packages by type ... {n:,}/{len(packages):,}", n, len(packages))
            f = session.files[key]
            cls = ""
            try:
                if f.store is not None:
                    head = f.read(0, min(f.size, 16384))
                    if len(head) >= 8 and up.zen_header_size(head) > len(head):
                        head = f.read(0, up.zen_header_size(head))
                else:
                    head = f.read()
                cls = up.package_info(head, session.script_objects).main_class
            except Exception:
                failed += 1
            session.classes[key] = cls or guess_class(key)
        log.info("Sorted %d packages by type in %.1fs (%d headers unreadable, guessed from their names)",
                 len(packages), time.time() - started, failed)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"version": CLASSIFY_CACHE_VERSION, "classes": session.classes}, f)
        except OSError as e:
            log.debug("Couldn't save the class cache: %s", e)


PLUGIN = UnrealPlugin()
