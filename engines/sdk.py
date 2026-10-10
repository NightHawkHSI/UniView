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

Types
-----
Everything here is type-annotated; the dict shapes plugins return (``GameInfo``, ``AssetStats``,
``Rig`` ...) are ``TypedDict``s, so an editor or ``mypy plugins/my_engine.py`` can check a plugin
against this contract. They're plain dicts at runtime.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

import numpy as np
from numpy.typing import ArrayLike, NDArray

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger("viewer.sdk")

API_VERSION = 1

# Asset kinds the app knows how to show. Anything else should be "file" (raw export only).
Kind = Literal["model", "scene", "texture", "sprite", "animation", "controller", "font", "video", "data", "text",
               "audio", "file"]
KINDS: tuple[Kind, ...] = ("model", "scene", "texture", "sprite", "animation", "controller", "font", "video", "data",
                          "text", "audio", "file")
KIND_LABELS: dict[str, str] = {
    "model": "Models",
    "scene": "Scenes & prefabs",
    "texture": "Textures",
    "sprite": "Sprites",
    "animation": "Animations",
    "controller": "Animators",
    "font": "Fonts",
    "video": "Videos",
    "data": "Scripts & data",
    "text": "Text",
    "audio": "Audio",
    "file": "Other files",
}
IMAGE_KINDS: tuple[Kind, ...] = ("texture", "sprite")
MODEL_KINDS: tuple[Kind, ...] = ("model", "scene")  # shown in 3D (a scene/prefab is many placed models)
THUMB_KINDS: tuple[Kind, ...] = ("model", "texture", "sprite")

# Texture roles in a material (used to pick the texture shown on a model / exported as base color).
ALBEDO, NORMAL, OTHER = "albedo", "normal", "other"

Buffer = bytes | bytearray | memoryview


# --------------------------------------------------------------------------- dict shapes plugins return

class GameInfo(TypedDict, total=False):
    """EnginePlugin.game_info()."""
    engine_version: str  # e.g. "2019.4.40f1"
    detail: str          # extra text on the game's box (backend, archive format ...)


class AssetStats(TypedDict, total=False):
    """GameSession.stats(). size/info/sort are what the list shows; the rest can be searched (e.g. tris>1000)."""
    size: int | None
    info: str
    sort: float
    tris: int
    verts: int
    w: int
    h: int


class _PluginOptionRequired(TypedDict):
    id: str
    label: str


class PluginOption(_PluginOptionRequired, total=False):
    """One entry of EnginePlugin.options (a per-game setting the user fills in)."""
    help: str
    multiline: bool


class Joint(TypedDict):
    name: str
    parent: int                      # joint index, -1 for a root
    translation: Sequence[float]     # x, y, z
    rotation: Sequence[float]        # quaternion x, y, z, w
    scale: Sequence[float]


class Bones:
    """A skeleton to draw over a model: joint positions in UniView's space (the one mesh() uses), each joint's
    parent (index, -1 for a root) and name."""

    __slots__ = ("points", "parents", "names")

    def __init__(self, points: ArrayLike, parents: Sequence[int], names: Sequence[str]) -> None:
        self.points = np.asarray(points, dtype=np.float32).reshape(-1, 3)
        self.parents = np.asarray(parents, dtype=np.int64).reshape(-1)
        self.names = list(names)

    def __len__(self) -> int:
        return len(self.points)


class Rig(TypedDict):
    """GameSession.skeleton(): a skinned model's skeleton, in UniView's space (the one mesh() uses)."""
    joints: list[Joint]
    skin_joints: list[int]           # joint index of each skin bone
    inverse_bind: NDArray[Any]       # (B, 4, 4) row-major, one per skin bone
    joints_0: NDArray[Any]           # (N, 4) skin bone indices per vertex
    weights_0: NDArray[Any]          # (N, 4)


class AnimationChannel(TypedDict):
    joint: int
    path: Literal["translation", "rotation", "scale"]
    values: NDArray[Any]             # (F, 3), or (F, 4) for rotations


class AnimationData(TypedDict):
    name: str
    times: NDArray[Any]              # (F,) seconds
    channels: list[AnimationChannel]


class Animated(Protocol):
    """What GameSession.animate() returns."""
    length: float  # seconds

    def points_at(self, t: float) -> NDArray[np.float32]:
        """(N, 3) vertex positions at time t, same order as mesh(model)."""
        ...

    # Optional: bones_at(t) -> (B, 3) joint positions at time t, same order as GameSession.bones(model),
    # so the bone overlay moves with the animation.


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

    kind: Kind
    name: str
    key: Hashable
    uid: str
    size: int | None
    path: str
    source: str
    ref: Any
    ext: str

    def __init__(self, kind: Kind, name: str, key: Hashable, uid: str | None = None, size: int | None = None,
                 path: str = "", source: str = "", ref: Any = None, ext: str = "") -> None:
        self.kind = kind
        self.name = name
        self.key = key
        self.uid = uid if uid is not None else str(key)
        self.size = size
        self.path = path
        self.source = source
        self.ref = ref
        self.ext = ext

    def __repr__(self) -> str:
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
    view_2d     mostly flat 2D content (sprites) facing -Z: shown straight on instead of from an angle
    gizmos      {kind: (points (N, 3), segments (M, 2))} wire overlays for things without a mesh (colliders,
                lights, cameras, sound sources), shown on request; gizmos_only: that's all there is
    owners      scenes/prefabs: (first vertex of each placed piece (K,) int64, [object id or None] * K) - which
                object a vertex belongs to, so a click in the view can select it (object ids are the "go" of the
                plugin's hierarchy() nodes); None for single models
    emitters    particle systems that can play: objects with simulate(t) -> particles alive at t (pos, vel,
                size, rot, color, frame - see engines.unity_particles), material (index), render_mode, tiles,
                mesh and center
    effect_mask (N,) bool: vertices of still effect stand-ins that playback replaces, or None
    bones       skinned meshes' skeletons as a Bones (positions, parents, names) for the bone overlay, or None
    lod_count   scenes: the most LOD levels any LODGroup in it has (the LOD picker offers that many), else 0
    environment scenes: {"sky": (top, horizon, bottom) colors, "ambient": color, "fog": text or None,
                "fog_params": {"mode": 1 linear / 2 exp / 3 exp squared, "color", "density", "start", "end"} or None,
                "sun": {"direction" (where its light goes), "color", "intensity"} of the main directional light or None,
                "sky_name": text, "sky_texture": {"kind": "cube" / "six" / "pano" / "clouds" /
                "gradient", "assets": [texture Assets], "tint", "rotation", "stops": [(height -1..1, color)] of
                three-colour gradient skies} or None} from the scene's lighting settings, or None (colors are
                (r, g, b) 0..1; a "cube" asset's faces come from GameSession.cube_faces())
    Lightmapped scenes also have a "lightmap" entry in uvs (the baked-light texture coordinates; see
    Material.lightmap).
    """

    points: NDArray[np.float32]
    submeshes: list[NDArray[np.int64]]
    normals: NDArray[np.float32] | None
    uvs: dict[str, NDArray[np.float32]]
    colors: NDArray[np.float32] | None
    material_slots: list[int]
    name: str
    skipped: int
    view_2d: bool
    gizmos: dict[str, tuple[NDArray[np.float32], NDArray[np.int64]]]
    gizmos_only: bool
    owners: tuple[NDArray[np.int64], list[str | None]] | None
    emitters: list[Any]
    effect_mask: NDArray[np.bool_] | None
    bones: "Bones | None"
    environment: dict[str, Any] | None
    lod_count: int

    def __init__(self, points: ArrayLike, submeshes: Iterable[ArrayLike], normals: ArrayLike | None = None,
                 uvs: Mapping[str, ArrayLike] | None = None, colors: ArrayLike | None = None,
                 material_slots: Iterable[int] | None = None, name: str = "", skipped: int = 0) -> None:
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
        self.view_2d = False
        self.owners = None
        self.gizmos = {}
        self.gizmos_only = False
        self.emitters = []
        self.effect_mask = None
        self.bones = None
        self.environment = None
        self.lod_count = 0
        if not n:
            raise ValueError("Mesh has no vertices.")
        if not any(len(s) for s in self.submeshes):
            raise ValueError("Mesh has no triangles.")

    @property
    def triangles(self) -> NDArray[np.int64]:
        return np.concatenate([s for s in self.submeshes if len(s)]) if self.submeshes else np.zeros((0, 3), np.int64)

    @property
    def triangle_count(self) -> int:
        return sum(len(s) for s in self.submeshes)


def _checked(arr: ArrayLike | None, n: int, width: int) -> NDArray[np.float32] | None:
    if arr is None:
        return None
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim != 2 or len(arr) != n or arr.shape[1] < width:
        if arr.ndim == 2 and len(arr) == n and width == 4 and arr.shape[1] == 3:
            return np.hstack([arr, np.ones((n, 1), np.float32)])
        return None
    return np.ascontiguousarray(arr[:, :width])


def z_up_to_y_up(points: ArrayLike) -> NDArray[np.float32]:
    """Right-handed Z-up (Source, Blender) -> Y-up: (x, y, z) -> (x, z, -y). Works for normals too."""
    p = np.asarray(points, dtype=np.float32)
    return np.ascontiguousarray(np.stack([p[:, 0], p[:, 2], -p[:, 1]], axis=1))


def orient_to_normals(points: ArrayLike, tris: NDArray[Any], normals: ArrayLike | None) -> NDArray[Any]:
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
    """A texture used by a material: slot/property name, display name, the texture Asset, role.

    label: the shader's name for the slot when the slot itself is cryptic (Shader Graph "Texture2D_<guid>" ->
    "Base Color Map"), else "". uv_scale / uv_offset: the material's tiling and offset for this texture
    (uv * scale + offset)."""

    __slots__ = ("slot", "name", "asset", "role", "label", "uv_scale", "uv_offset")

    slot: str
    name: str
    asset: Asset
    role: str
    label: str
    uv_scale: tuple[float, float]
    uv_offset: tuple[float, float]

    def __init__(self, slot: str, name: str, asset: Asset, role: str = OTHER, label: str = "",
                 uv_scale: Sequence[float] = (1.0, 1.0), uv_offset: Sequence[float] = (0.0, 0.0)) -> None:
        self.slot, self.name, self.asset, self.role, self.label = slot, name, asset, role, label
        self.uv_scale = (float(uv_scale[0]), float(uv_scale[1]))
        self.uv_offset = (float(uv_offset[0]), float(uv_offset[1]))

    @property
    def tiled(self) -> bool:
        return self.uv_scale != (1.0, 1.0) or self.uv_offset != (0.0, 0.0)


class Material:
    """name + textures (albedo first is nice: the first albedo texture goes on the model).

    color: main color (r, g, b, a) in 0..1 - shown when there's no texture, and multiplied with the
    texture in GLB exports. properties: [(name, value text)] shown in the info panel.
    alpha_mode: how the albedo texture's alpha is used - "opaque" (ignored: many games keep other data there),
    "mask" (cut out below alpha_cutoff: leaves, grass, fences), "blend" (see-through: glass, decals, effects) or
    "add" (added on top, black = invisible: glows, sparks, flashes).
    lightmap: texture Asset of the baked light multiplied onto this material in a scene (with the mesh's
    "lightmap" UVs), or None; lightmap_mode: how it's stored - "hdr" (float), "rgbm" (rgb x 5 x alpha) or
    "dldr" (rgb x 2).
    """

    lightmap: Asset | None = None
    lightmap_mode: str = ""

    def __init__(self, name: str, textures: Iterable[TextureRef] = (), color: Sequence[float] | None = None,
                 properties: Iterable[tuple[str, str]] | None = None, alpha_mode: str = "opaque",
                 alpha_cutoff: float = 0.5) -> None:
        self.name = name
        self.textures = list(textures)
        self.color = tuple(color) if color is not None else None
        self.properties = list(properties or [])
        self.alpha_mode = alpha_mode
        self.alpha_cutoff = alpha_cutoff

    def main_texture(self) -> TextureRef | None:
        for t in self.textures:
            if t.role == ALBEDO:
                return t
        return next((t for t in self.textures if t.role != NORMAL), None)


def main_texture(materials: Iterable[Material]) -> Asset | None:
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

    def __init__(self, text_fn: Callable[[str], object] | None = None,
                 value_fn: Callable[[int, int], object] | None = None,
                 options: Mapping[str, Any] | None = None,
                 files_fn: Callable[[str, Any], object] | None = None) -> None:
        self._text: Callable[[str], object] = text_fn or (lambda s: None)
        self._value: Callable[[int, int], object] = value_fn or (lambda d, t: None)
        self._files: Callable[[str, Any], object] = files_fn or (lambda event, payload: None)
        self.options: dict[str, Any] = dict(options or {})

    def __call__(self, text: str, done: int = 0, total: int = 0) -> None:
        """Status text, plus done/total for a progress bar.

        progress("Listing files ...") alone shows a moving 'busy' bar (total 0 = length unknown);
        progress("Reading data 3/10", 3, 10) fills it.
        """
        self._text(text)
        self._value(done, total)

    def files(self, entries: Iterable[tuple[str, int | None]]) -> None:
        """Optional: the files this load is about to read, as (name, size in bytes or None). The loading
        screen shows one icon per file (sized by how big it is) instead of a plain bar."""
        self._files("queue", [(str(name), int(size or 0)) for name, size in entries])

    def file(self, index: int) -> None:
        """Optional, after files(): now reading entries[index] (everything before it is done).
        Pass len(entries) once the last one is finished."""
        self._files("at", int(index))

    def found(self, counts: Mapping[str, int]) -> None:
        """Optional: running totals of the assets found so far, by kind ({"texture": 1200, "model": 85}).
        The loading screen shows them flying out to a counter per kind while the plugin indexes."""
        self._files("found", {str(k): int(v) for k, v in counts.items()})


# Display options from the app's Options menu (all on by default). Plugins read them with
# view_option(); when one changes the app calls GameSession.options_changed() so caches can be dropped.
VIEW_OPTIONS: dict[str, bool] = {
    "hide_skybox": True,         # maps: leave out the 3D skybox (the small scenery copy around the map)
    "hide_tool_surfaces": True,  # maps: leave out nodraw, trigger, clip, hint and sky brushes
    "hide_lods": True,           # scenes: show only the most detailed LOD of each object
    "hide_inactive": True,       # scenes: leave out objects and renderers the game has switched off
    "show_effects": True,        # scenes: particle systems (as a few still puffs) and line renderers
}


def view_option(name: str) -> bool:
    return VIEW_OPTIONS.get(name, True)


# Numeric view settings picked in the 3D view (not saved): "lod_level" = which LOD of each LODGroup scenes show.
VIEW_LEVELS: dict[str, int] = {"lod_level": 0}


def view_level(name: str) -> int:
    return VIEW_LEVELS.get(name, 0)


class GameSession:
    """A loaded game. Created by EnginePlugin.open(). Override what your engine supports."""

    def __init__(self, plugin: EnginePlugin, path: str) -> None:
        self.plugin = plugin
        self.path = path
        self.lock = threading.RLock()     # the app holds this around every call below
        self.assets: list[Asset] = []     # fill this in open()
        self.file_count = 0               # game files that were read (shown on the project box)
        self.engine_version = ""          # set if you learn it while loading (else game_info's is kept)
        self.warnings: list[str] = []     # things the user should know (shown once after loading)

    def options_changed(self) -> None:
        """VIEW_OPTIONS changed: drop cached maps/scenes built with the old settings."""

    # ---- previews (raise an exception with a friendly message if something can't be decoded)
    def image(self, asset: Asset) -> Image:
        """PIL image of a texture/sprite asset."""
        raise NotImplementedError("This engine plugin can't show images yet.")

    def mesh(self, asset: Asset) -> MeshData:
        """MeshData of a model asset."""
        raise NotImplementedError("This engine plugin can't show models yet.")

    def text(self, asset: Asset) -> str | bytes:
        """str (or bytes) of a text asset. Default: decode raw() (binary text formats are decoded)."""
        data = self.raw(asset)
        if (asset.ext or "").lower() == "locres" and isinstance(data, (bytes, bytearray)):
            from .formats import locres_text
            try:
                return locres_text(data)
            except Exception as e:
                log.warning("Couldn't read the localization file '%s': %s", asset.name, e)
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data

    def raw(self, asset: Asset) -> bytes:
        """The asset's bytes as stored (used to export text/file assets as-is)."""
        raise NotImplementedError("This engine plugin can't export raw files.")

    def content_hash(self, asset: Asset) -> bytes | None:
        """Fingerprint of the asset's content, the same for identical copies (duplicate finder); None if it
        can't be fingerprinted. Default: a hash of raw(). Override when raw() holds per-copy details (names,
        offsets into other files) so true copies still match."""
        import hashlib
        data = self.raw(asset)
        return hashlib.blake2b(bytes(data), digest_size=16).digest() if data is not None else None

    def audio(self, asset: Asset) -> tuple[bytes, str]:
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
    def stats(self, asset: Asset) -> AssetStats:
        """Numbers for sorting/searching, cheap to compute (no full decoding if you can avoid it).

        Keys the app uses: size (bytes), info (short text for the Info column), sort (number for
        sorting the Info column), tris, verts (models), w, h (images).
        """
        return {"size": asset.size, "info": "", "sort": asset.size or 0}

    def materials(self, asset: Asset) -> list[Material]:
        """[Material] of a model asset (may be slow the first time)."""
        return []

    def materials_ready(self) -> bool:
        """False while background indexing makes materials() slow; the app retries later."""
        return True

    def describe(self, asset: Asset) -> list[tuple[str, str]]:
        """Extra (label, text) rows for the model/texture info panel."""
        rows = []
        if asset.source:
            rows.append(("File", asset.source))
        if asset.path and asset.path != asset.source:
            rows.append(("Path", asset.path))
        return rows

    def related(self, asset: Asset) -> tuple[str, list[Asset], str]:
        """Links shown under a texture: (title, [Asset], text when empty). E.g. models that use it."""
        return "", [], ""

    def font(self, asset: Asset) -> Image:
        """Sample sheet of a font asset. Default: render raw() (TTF/OTF bytes) with font_preview()."""
        return font_preview(self.raw(asset), asset.name)

    def video(self, asset: Asset) -> tuple[bytes, str]:
        """(bytes, extension) of a video the player can open (mp4, webm, mov, avi, mkv...)."""
        return self.raw(asset), (asset.ext or "mp4").lower()

    def sprite_frames(self, asset: Asset) -> tuple[list[tuple[float, Asset]], float]:
        """2D (flipbook) animation: ([(time, sprite/texture Asset)], length in seconds), or ([], 0)."""
        return [], 0.0

    def sprite_rects(self, asset: Asset) -> list[tuple[float, float, float, float]]:
        """Sprites cut from a texture (sprite sheet): [(x, y, w, h)] in pixels, y measured from the BOTTOM."""
        return []

    def animation_targets(self, asset: Asset) -> list[Asset]:
        """Models an animation clip can play on, best match first ([Asset])."""
        return []

    def animate(self, model: Asset, clip: Asset) -> Animated:
        """An object with .length (seconds) and .points_at(t) -> (N, 3) vertex positions of `model`
        (same order as mesh(model)) posed by `clip` at time t."""
        raise NotImplementedError("This engine plugin can't play animations yet.")

    def cube_faces(self, asset: Asset, max_side: int = 1024) -> list[Any]:
        """The 6 faces (PIL images: +X, -X, +Y, -Y, +Z, -Z in the engine's own handedness) of a cube texture."""
        raise NotImplementedError("This engine plugin has no cube textures.")

    def controller(self, asset: Asset) -> dict[str, Any]:
        """An animation state machine ("controller" asset): {"name", "parameters": [{"name", "type", "default"}],
        "layers": [{"name", "weight", "default_state", "states": [{"name", "tag", "speed", "motion": {"clip": Asset}
        or {"tree": {"nodes": [{"type", "param", "clip", "children", ...}]}} or None, "transitions": [{"dest"
        (state index or -1 = exit), "duration", "exit_time", "has_exit_time", "conditions": [{"mode", "param",
        "threshold"}]}]}], "any_transitions": [...]}]} - see engines.unity_controller."""
        raise NotImplementedError("This engine plugin can't read animation state machines.")

    def lod_siblings(self, model: Asset) -> list[Asset | None]:
        """A model's LOD group: the mesh Asset of each level (LOD0 first, None where unknown), or [] if it isn't
        part of one."""
        return []

    def bones(self, model: Asset) -> Bones | None:
        """Skeleton of a skinned single model for the bone overlay, or None. (Scenes and prefabs put theirs
        in MeshData.bones.)"""
        return None

    def skeleton(self, model: Asset, clip: Asset | None = None) -> tuple[Rig, AnimationData | None]:
        """Rig of a skinned `model` for rigged glTF exports: (rig, animation or None).

        rig: {"joints": [{"name", "parent" (joint index or -1), "translation", "rotation" (x, y, z, w),
        "scale"}], "skin_joints": [joint index per skin bone], "inverse_bind": (B, 4, 4) row-major,
        "joints_0": (N, 4) skin bone indices per vertex, "weights_0": (N, 4)}, all in UniView's space
        (the same one mesh() uses). With `clip`, animation is {"name", "times": (F,), "channels":
        [{"joint", "path": "translation"/"rotation"/"scale", "values": (F, 3 or 4)}]}."""
        raise NotImplementedError("This engine plugin can't export skeletons yet.")

    def start_background(self) -> None:
        """Called once after loading, on the UI thread: start background indexing if you have any."""

    def close(self) -> None:
        """Game is being unloaded: stop background work, close files."""


class EnginePlugin:
    """Describes one engine. Keep detect()/game_info() fast: they run for every game on the start page."""

    id: str = "engine"       # short unique id, saved in projects.json
    name: str = "Engine"     # shown to the user
    version: str = "1.0"     # your plugin's version
    author: str = ""
    description: str = ""
    api_version: int = API_VERSION
    # Per-game settings the user can fill in (right-click a game -> Engine settings...), given to
    # open() as progress.options: [{"id": "aes_keys", "label": "AES keys", "help": "...", "multiline": True}]
    options: list[PluginOption] = []

    def detect(self, path: str) -> int:
        """How sure are you this folder (or file) is a game of this engine? 0 = no ... 100 = certain.

        Only look at folder listings / a few file headers, no big reads.
        """
        return 0

    def game_info(self, path: str) -> GameInfo:
        """{"engine_version": "...", "detail": "..."} read cheaply (file headers). Shown on the game's box."""
        return {"engine_version": "", "detail": ""}

    def count_files(self, path: str) -> int:
        """Number of game data files this plugin would read (shown on the game's box)."""
        return 0

    def open(self, path: str, progress: Progress) -> GameSession:
        """Load the game at `path` and return a GameSession. Runs on a worker thread.

        progress(text, done, total) reports what's happening.
        """
        raise NotImplementedError

    def label(self, info: GameInfo) -> str:
        """Engine + version shown on the game's box (e.g. 'Unity 2019.4.40f1'). info is game_info()'s dict."""
        return f"{self.name} {info.get('engine_version', '')}".strip()

    def short_version(self, info: GameInfo) -> str:
        """Group title for 'Group by engine version' (e.g. 'Unity 2019.4'). Default: label()."""
        return self.label(info)


# --------------------------------------------------------------------------- helpers for plugins

def walk_files(root: str, exts: Iterable[str] | None = None, skip_dirs: Iterable[str] = ()) -> Iterator[str]:
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


def app_dir() -> str:
    """Folder next to UniView.exe (or the source), where user files live."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cache_dir() -> str:
    """A folder plugins can keep caches in (e.g. results of slow indexing), next to the app."""
    path = os.path.join(app_dir(), "cache")
    os.makedirs(path, exist_ok=True)
    return path


def listdir_lower(path: str) -> dict[str, str]:
    try:
        return {n.lower(): n for n in os.listdir(path)}
    except OSError:
        return {}


class BinReader:
    """Little-endian reader over bytes/memoryview (plugins parse binary formats with this)."""

    def __init__(self, data: Buffer, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def seek(self, pos: int) -> BinReader:
        self.pos = pos
        return self

    def skip(self, n: int) -> BinReader:
        self.pos += n
        return self

    def read(self, n: int) -> bytes:
        out = bytes(self.data[self.pos:self.pos + n])
        if len(out) != n:
            raise EOFError(f"Unexpected end of data at {self.pos} (wanted {n} bytes)")
        self.pos += n
        return out

    def _unpack(self, fmt: str, size: int) -> Any:
        import struct
        value = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return value[0] if len(value) == 1 else value

    def u8(self) -> int:
        return self._unpack("B", 1)

    def i8(self) -> int:
        return self._unpack("b", 1)

    def u16(self) -> int:
        return self._unpack("H", 2)

    def i16(self) -> int:
        return self._unpack("h", 2)

    def u32(self) -> int:
        return self._unpack("I", 4)

    def i32(self) -> int:
        return self._unpack("i", 4)

    def u64(self) -> int:
        return self._unpack("Q", 8)

    def i64(self) -> int:
        return self._unpack("q", 8)

    def f32(self) -> float:
        return self._unpack("f", 4)

    def cstr(self, encoding: str = "utf-8") -> str:
        end = bytes(self.data[self.pos:self.pos + 4096]).find(b"\0")
        if end < 0:
            data = bytes(self.data[self.pos:])
            end = len(data)
        else:
            data = bytes(self.data[self.pos:self.pos + end])
        self.pos += end + 1
        return data.decode(encoding, "replace")


def cstr_at(data: Buffer, offset: int, limit: int = 512) -> str:
    end = bytes(data[offset:offset + limit]).find(b"\0")
    raw = bytes(data[offset:offset + (limit if end < 0 else end)])
    return raw.decode("utf-8", "replace")


TEXT_EXTS: set[str] = {
    "txt", "json", "xml", "ini", "cfg", "csv", "tsv", "md", "log", "lua", "nut", "py", "js", "cs",
    "shader", "hlsl", "glsl", "fx", "fxc", "vmt", "vdf", "res", "vfe", "gi", "kv3", "yaml", "yml",
    "html", "htm", "css", "properties", "toml", "uplugin", "uproject", "cmd", "bat", "rad", "lst",
    "acf", "manifest", "vcfg", "qc", "smd", "vsc", "gam",
    # localization, subtitles, level/script text of various engines
    "locres", "po", "pot", "lang", "srt", "vtt", "ass", "pop", "conf", "scr", "ts", "sh", "nfo", "sql",
    "tscn", "tres", "gd", "godot", "rpy", "mcmeta", "ron", "vcd", "cfg", "ent", "def", "mtr", "map",
}
IMAGE_EXTS: set[str] = {"png", "jpg", "jpeg", "jfif", "bmp", "tga", "dds", "gif", "webp", "tif", "tiff", "svg", "ico", "cur",
              "psd", "pcx", "ppm", "pgm", "pbm", "sgi", "icns"}
AUDIO_EXTS: set[str] = {"wav", "mp3", "ogg", "flac", "opus", "m4a", "aac", "wem", "bnk", "bank", "fsb", "xwb", "vsnd_c"}
PLAYABLE_AUDIO: set[str] = {"wav", "mp3", "ogg", "flac", "opus", "m4a", "aac"}  # what the built-in player decodes
VIDEO_EXTS: set[str] = {"mp4", "webm", "mov", "avi", "mkv", "ogv", "m4v", "wmv", "bk2", "bik", "usm"}
FONT_EXTS: set[str] = {"ttf", "otf", "ttc"}


def kind_for_extension(ext: str) -> Kind:
    """Default kind for a loose file by extension (text / texture / audio / file)."""
    ext = ext.lower().lstrip(".")
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "texture"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in FONT_EXTS:
        return "font"
    return "file"


def pil_image_from_bytes(data: Buffer) -> Image:
    """Open a png/jpg/tga/dds/ico/svg/... stored as bytes."""
    import io
    from PIL import Image
    head = bytes(data[:512]).lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in bytes(data[:4096]).lower()) \
            or head.startswith(b"<!--") and b"<svg" in bytes(data[:4096]).lower():
        return svg_image(data)
    img = Image.open(io.BytesIO(data))
    if getattr(img, "format", "") == "ICO" and hasattr(img, "ico"):
        img = img.ico.getimage(max(img.ico.sizes()))  # the biggest icon inside
    img.load()
    return img


def svg_image(data: Buffer, size: int = 1024) -> Image:
    """Render an SVG (bytes) to a PIL image about `size` pixels on its longest side (Qt's renderer)."""
    from PIL import Image
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(QByteArray(bytes(data)))
    if not renderer.isValid():
        raise ValueError("This SVG image couldn't be read.")
    view = renderer.viewBoxF()
    w, h = (view.width(), view.height()) if view.width() > 0 and view.height() > 0 else (1.0, 1.0)
    scale = size / max(w, h)
    img = QImage(max(1, round(w * scale)), max(1, round(h * scale)), QImage.Format.Format_RGBA8888)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    renderer.render(painter, QRectF(0, 0, img.width(), img.height()))
    painter.end()
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA",
                            img.bytesPerLine(), 1).copy()


def find_cstrings(data: Buffer, min_len: int = 4, limit: int = 20000) -> list[str]:
    """Readable zero-terminated strings in binary data (file names, keys...), in order, without repeats."""
    import re
    out, seen = [], set()
    for m in re.finditer(rb"[\x20-\x7e\t]{%d,}" % min_len, bytes(data)):
        text = m.group().decode("ascii").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                break
    return out


def font_preview(data: bytes | None, name: str = "") -> Image:
    """A sample sheet rendered with a font (TTF/OTF bytes)."""
    import io
    if not data:
        raise ValueError("This font has no font file inside - it points to a built-in or system font.")
    from PIL import Image, ImageDraw, ImageFont
    lines = ((52, name or "Font"), (38, "The quick brown fox jumps over the lazy dog"),
             (30, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), (30, "abcdefghijklmnopqrstuvwxyz"),
             (30, "0123456789  !?&@#$%*()[]{}<>+-=/"), (22, "Grumpy wizards make toxic brew for the jumping queen."),
             (16, "Pack my box with five dozen liquor jugs."))
    img = Image.new("RGBA", (1100, 40 + sum(int(size * 1.4) for size, _t in lines)), (250, 250, 250, 255))
    draw = ImageDraw.Draw(img)
    y = 16
    for size, text in lines:
        try:
            font = ImageFont.truetype(io.BytesIO(data), size)
        except OSError as e:
            raise ValueError(f"This font can't be rendered: {e}")
        draw.text((20, y), text, font=font, fill=(25, 25, 25, 255))
        y += int(size * 1.4)
    return img
