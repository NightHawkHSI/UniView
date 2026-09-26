"""UniView engine plugin API.

An engine plugin teaches UniView how to read one game engine's files. The app itself only
knows about the neutral types below (Asset, MeshData, Material, ...); everything engine
specific lives in a plugin.

Writing a plugin
----------------
1. Copy ``plugins/_template.py`` to ``plugins/my_engine.py`` (files starting with ``_`` are skipped).
2. Subclass :class:`EnginePlugin` (detect a game folder, open it) and :class:`GameSession`
   (list assets, decode them).
3. Expose it with a module level ``PLUGIN = MyEnginePlugin()``.
4. Restart UniView. Help -> Engine plugins shows whether it loaded (errors go to the console).

Everything is called from worker threads as well as the UI thread; the app serializes calls
into a session with ``session.lock``, so plugin code doesn't need its own locking.

Conventions for decoded data
----------------------------
* Meshes: right-handed, **Y up**, counter-clockwise front faces (the glTF / Blender-import
  convention). Convert from the engine's own space in the plugin (see ``z_up_to_y_up``).
* UVs: origin at the **bottom-left** (OpenGL). DirectX-style engines flip v (``1 - v``).
* Images: any ``PIL.Image`` (converted to RGBA by the app when needed).
"""

import os
import threading

import numpy as np

API_VERSION = 1

# Asset kinds the app knows how to show. Anything else should be "file" (raw export only).
KINDS = ("model", "texture", "sprite", "animation", "text", "audio", "file")
KIND_LABELS = {
    "model": "Models",
    "texture": "Textures",
    "sprite": "Sprites",
    "animation": "Animations",
    "text": "Text",
    "audio": "Audio",
    "file": "Other files",
}
IMAGE_KINDS = ("texture", "sprite")
THUMB_KINDS = ("model", "texture", "sprite")

# Texture roles in a material (used to pick the texture shown on a model / exported as base color).
ALBEDO, NORMAL, OTHER = "albedo", "normal", "other"


class Asset:
    """One thing in a game's list.

    kind    one of KINDS
    name    display name
    key     hashable id, unique inside the loaded game (used for caches / jumping between assets)
    uid     string id that stays the same between sessions (favorites are saved by it)
    size    size in bytes if known (shown in the list before stats are measured), else None
    path    where it lives in the game, '/' separated (bulk export can mirror this as folders)
    source  file it was read from (shown in the info panel)
    ref     anything the plugin needs to find the data again
    ext     file extension for raw export of text/file assets (e.g. "txt", "vmt", "wav")
    """

    __slots__ = ("kind", "name", "key", "uid", "size", "path", "source", "ref", "ext")

    def __init__(self, kind, name, key, uid=None, size=None, path="", source="", ref=None, ext=""):
        self.kind = kind
        self.name = name
        self.key = key
        self.uid = uid if uid is not None else str(key)
        self.size = size
        self.path = path
        self.source = source
        self.ref = ref
        self.ext = ext

    def __repr__(self):
        return f"<Asset {self.kind} {self.name!r}>"


class MeshData:
    """Decoded triangle mesh in UniView's convention (right-handed, Y up, CCW, UV origin bottom-left).

    points      (N, 3) float32
    submeshes   list of (M, 3) int arrays, one per material slot
    normals     (N, 3) float32 or None
    uvs         {"UV0": (N, 2) float32, "UV1": ...} in order
    colors      (N, 4) float32 in 0..1, or None
    material_slots  for each submesh, the index into the model's materials (default: same index)
    skipped     parts that couldn't be shown (lines/points), for the log
    """

    def __init__(self, points, submeshes, normals=None, uvs=None, colors=None, material_slots=None,
                 name="", skipped=0):
        self.points = np.ascontiguousarray(points, dtype=np.float32).reshape(-1, 3)
        self.submeshes = [np.asarray(s, dtype=np.int64).reshape(-1, 3) for s in submeshes]
        self.submeshes = [s for s in self.submeshes if len(s)] or self.submeshes[:1]
        n = len(self.points)
        self.normals = _checked(normals, n, 3)
        self.uvs = {k: v for k, v in ((k, _checked(v, n, 2)) for k, v in (uvs or {}).items()) if v is not None}
        self.colors = _checked(colors, n, 4)
        self.material_slots = list(material_slots) if material_slots is not None else list(range(len(self.submeshes)))
        self.name = name
        self.skipped = skipped
        if not n:
            raise ValueError("Mesh has no vertices.")
        if not any(len(s) for s in self.submeshes):
            raise ValueError("Mesh has no triangles.")

    @property
    def triangles(self):
        return np.concatenate([s for s in self.submeshes if len(s)]) if self.submeshes else np.zeros((0, 3), np.int64)

    @property
    def triangle_count(self):
        return sum(len(s) for s in self.submeshes)


def _checked(arr, n, width):
    if arr is None:
        return None
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim != 2 or len(arr) != n or arr.shape[1] < width:
        if arr.ndim == 2 and len(arr) == n and width == 4 and arr.shape[1] == 3:
            return np.hstack([arr, np.ones((n, 1), np.float32)])
        return None
    return np.ascontiguousarray(arr[:, :width])


def z_up_to_y_up(points):
    """Right-handed Z-up (Source, Blender) -> Y-up: (x, y, z) -> (x, z, -y). Works for normals too."""
    p = np.asarray(points, dtype=np.float32)
    return np.ascontiguousarray(np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1))


def orient_to_normals(points, tris, normals):
    """Flip triangle winding if most faces point against the vertex normals. Returns tris."""
    if normals is None or not len(tris):
        return tris
    p = np.asarray(points, dtype=np.float64)
    t = np.asarray(tris)
    face = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    vn = np.asarray(normals, dtype=np.float64)
    agree = np.einsum("ij,ij->i", face, vn[t[:, 0]] + vn[t[:, 1]] + vn[t[:, 2]])
    return t[:, ::-1] if (agree < 0).sum() > (agree > 0).sum() else t


class TextureRef:
    """A texture used by a material: slot/property name, display name, the texture Asset, role."""

    __slots__ = ("slot", "name", "asset", "role")

    def __init__(self, slot, name, asset, role=OTHER):
        self.slot, self.name, self.asset, self.role = slot, name, asset, role


class Material:
    """name + textures (albedo first is nice: the first albedo texture goes on the model)."""

    def __init__(self, name, textures=()):
        self.name = name
        self.textures = list(textures)

    def main_texture(self):
        for t in self.textures:
            if t.role == ALBEDO:
                return t
        return next((t for t in self.textures if t.role != NORMAL), None)


def main_texture(materials):
    """Texture Asset to show on a model: the first material's albedo, else any non-normal map."""
    for mat in materials:
        tex = mat.main_texture()
        if tex is not None:
            return tex.asset
    return None


class Progress:
    """Passed to EnginePlugin.open(): report what's going on. Safe to call from the loader thread.

    progress.options holds the per-game settings the user entered for the plugin's `options`
    (right-click a game -> Engine settings...), e.g. {"aes_keys": "0x1234..."}.
    """

    def __init__(self, text_fn=None, value_fn=None, options=None):
        self._text = text_fn or (lambda s: None)
        self._value = value_fn or (lambda d, t: None)
        self.options = dict(options or {})

    def __call__(self, text, done=0, total=0):
        """Status text, plus done/total for a progress bar (total 0 = busy animation)."""
        self._text(text)
        self._value(done, total)


class GameSession:
    """A loaded game. Created by EnginePlugin.open(). Override what your engine supports."""

    def __init__(self, plugin, path):
        self.plugin = plugin
        self.path = path
        self.lock = threading.RLock()  # the app holds this around every call below
        self.assets = []               # [Asset] - fill this in open()
        self.file_count = 0            # game files that were read (shown on the project box)
        self.engine_version = ""       # set if you learn it while loading (else game_info's is kept)
        self.warnings = []             # things the user should know (shown once after loading)

    # ---- previews (raise an exception with a friendly message if something can't be decoded)
    def image(self, asset):
        """PIL image of a texture/sprite asset."""
        raise NotImplementedError("This engine plugin can't show images yet.")

    def mesh(self, asset):
        """MeshData of a model asset."""
        raise NotImplementedError("This engine plugin can't show models yet.")

    def text(self, asset):
        """str (or bytes) of a text asset. Default: decode raw()."""
        data = self.raw(asset)
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data

    def raw(self, asset):
        """The asset's bytes as stored (used to export text/file assets as-is)."""
        raise NotImplementedError("This engine plugin can't export raw files.")

    def audio(self, asset):
        """(bytes, extension) of a sound in a format a media player understands:
        wav, mp3, ogg, flac, m4a/aac. Default: the raw file if its extension is one of those."""
        ext = (asset.ext or "").lower()
        if ext in PLAYABLE_AUDIO:
            return self.raw(asset), ext
        from . import extdecode
        if ext in extdecode.VGMSTREAM_EXTS:
            return extdecode.to_wav(self.raw(asset), ext), "wav"
        raise NotImplementedError(f"Playing .{ext or '?'} sounds isn't supported yet (export saves the file as-is).")

    # ---- extra info (all optional)
    def stats(self, asset):
        """Numbers for sorting/searching, cheap to compute (no full decoding if you can avoid it).

        Keys the app uses: size (bytes), info (short text for the Info column), sort (number for
        sorting the Info column), tris, verts (models), w, h (images).
        """
        return {"size": asset.size, "info": "", "sort": asset.size or 0}

    def materials(self, asset):
        """[Material] of a model asset (may be slow the first time)."""
        return []

    def materials_ready(self):
        """False while background indexing makes materials() slow; the app retries later."""
        return True

    def describe(self, asset):
        """Extra (label, text) rows for the model/texture info panel."""
        rows = []
        if asset.source:
            rows.append(("File", asset.source))
        if asset.path and asset.path != asset.source:
            rows.append(("Path", asset.path))
        return rows

    def related(self, asset):
        """Links shown under a texture: (title, [Asset], text when empty). E.g. models that use it."""
        return "", [], ""

    def animation_targets(self, asset):
        """Models an animation clip can play on, best match first ([Asset])."""
        return []

    def animate(self, model, clip):
        """An object with .length (seconds) and .points_at(t) -> (N, 3) vertex positions of `model`
        (same order as mesh(model)) posed by `clip` at time t."""
        raise NotImplementedError("This engine plugin can't play animations yet.")

    def start_background(self):
        """Called once after loading, on the UI thread: start background indexing if you have any."""

    def close(self):
        """Game is being unloaded: stop background work, close files."""


class EnginePlugin:
    """Describes one engine. Keep detect()/game_info() fast: they run for every game on the start page."""

    id = "engine"            # short unique id, saved in projects.json
    name = "Engine"          # shown to the user
    version = "1.0"          # your plugin's version
    author = ""
    description = ""
    api_version = API_VERSION
    # Per-game settings the user can fill in (right-click a game -> Engine settings...), given to
    # open() as progress.options: [{"id": "aes_keys", "label": "AES keys", "help": "...", "multiline": True}]
    options = []

    def detect(self, path):
        """How sure are you this folder (or file) is a game of this engine? 0 = no ... 100 = certain.

        Only look at folder listings / a few file headers, no big reads.
        """
        return 0

    def game_info(self, path):
        """{"engine_version": "...", "detail": "..."} read cheaply (file headers). Shown on the game's box."""
        return {"engine_version": "", "detail": ""}

    def count_files(self, path):
        """Number of game data files this plugin would read (shown on the game's box)."""
        return 0

    def open(self, path, progress):
        """Load the game at `path` and return a GameSession. Runs on a worker thread.

        progress(text, done, total) reports what's happening.
        """
        raise NotImplementedError

    def label(self, info):
        """Engine + version shown on the game's box (e.g. 'Unity 2019.4.40f1'). info is game_info()'s dict."""
        return f"{self.name} {info.get('engine_version', '')}".strip()

    def short_version(self, info):
        """Group title for 'Group by engine version' (e.g. 'Unity 2019.4'). Default: label()."""
        return self.label(info)


# --------------------------------------------------------------------------- helpers for plugins

def walk_files(root, exts=None, skip_dirs=()):
    """Yield file paths under root (or root itself if it's a file), optionally only these extensions."""
    if os.path.isfile(root):
        yield root
        return
    exts = tuple(e.lower() for e in exts) if exts else None
    skip = {d.lower() for d in skip_dirs}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for name in files:
            if exts is None or name.lower().endswith(exts):
                yield os.path.join(dirpath, name)


def app_dir():
    """Folder next to UniView.exe (or the source), where user files live."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cache_dir():
    """A folder plugins can keep caches in (e.g. results of slow indexing), next to the app."""
    path = os.path.join(app_dir(), "cache")
    os.makedirs(path, exist_ok=True)
    return path


def listdir_lower(path):
    try:
        return {n.lower(): n for n in os.listdir(path)}
    except OSError:
        return {}


class BinReader:
    """Little-endian reader over bytes/memoryview (plugins parse binary formats with this)."""

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def seek(self, pos):
        self.pos = pos
        return self

    def skip(self, n):
        self.pos += n
        return self

    def read(self, n):
        out = bytes(self.data[self.pos:self.pos + n])
        if len(out) != n:
            raise EOFError(f"Unexpected end of data at {self.pos} (wanted {n} bytes)")
        self.pos += n
        return out

    def _unpack(self, fmt, size):
        import struct
        value = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return value[0] if len(value) == 1 else value

    def u8(self):
        return self._unpack("B", 1)

    def i8(self):
        return self._unpack("b", 1)

    def u16(self):
        return self._unpack("H", 2)

    def i16(self):
        return self._unpack("h", 2)

    def u32(self):
        return self._unpack("I", 4)

    def i32(self):
        return self._unpack("i", 4)

    def u64(self):
        return self._unpack("Q", 8)

    def i64(self):
        return self._unpack("q", 8)

    def f32(self):
        return self._unpack("f", 4)

    def cstr(self, encoding="utf-8"):
        end = bytes(self.data[self.pos:self.pos + 4096]).find(b"\0")
        if end < 0:
            data = bytes(self.data[self.pos:])
            end = len(data)
        else:
            data = bytes(self.data[self.pos:self.pos + end])
        self.pos += end + 1
        return data.decode(encoding, "replace")


def cstr_at(data, offset, limit=512):
    end = bytes(data[offset:offset + limit]).find(b"\0")
    raw = bytes(data[offset:offset + (limit if end < 0 else end)])
    return raw.decode("utf-8", "replace")


TEXT_EXTS = {
    "txt", "json", "xml", "ini", "cfg", "csv", "tsv", "md", "log", "lua", "nut", "py", "js", "cs",
    "shader", "hlsl", "glsl", "fx", "fxc", "vmt", "vdf", "res", "vfe", "gi", "kv3", "yaml", "yml",
    "html", "htm", "css", "properties", "toml", "uplugin", "uproject", "cmd", "bat", "rad", "lst",
    "acf", "manifest", "vcfg", "qc", "smd", "vsc", "gam",
}
IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "tga", "dds", "gif", "webp", "tif", "tiff"}
AUDIO_EXTS = {"wav", "mp3", "ogg", "flac", "opus", "m4a", "aac", "wem", "bnk", "bank", "fsb", "xwb", "vsnd_c"}
PLAYABLE_AUDIO = {"wav", "mp3", "ogg", "flac", "opus", "m4a", "aac"}  # what the built-in player decodes


def kind_for_extension(ext):
    """Default kind for a loose file by extension (text / texture / audio / file)."""
    ext = ext.lower().lstrip(".")
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "texture"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


def pil_image_from_bytes(data):
    """Open a png/jpg/tga/dds/... stored as bytes."""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
