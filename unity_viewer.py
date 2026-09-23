"""UniView - Unity Asset Viewer. Browse meshes, textures and text assets from Unity games.

Starts on a projects page with a box per saved game (add one by folder or let it
find Unity games in your Steam libraries). Opening a game scans its whole folder
for Unity files; pick an item in the tree and it previews on the right. Meshes render in 3D (with their texture when it
can be found through a MeshRenderer), textures show as images.
"""

__version__ = "1.1.2"
APP_SHORT = "UniView"
APP_TITLE = "UniView - Unity Asset Viewer"

import faulthandler
import glob
import io
import json
import logging
import logging.handlers
import operator
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
import pyvista as pv
import UnityPy
from PIL import Image
from PySide6.QtCore import (
    QEvent, QFileInfo, QObject, QPoint, QRect, QRectF, QSignalBlocker, QSize, Qt, QThread, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QIcon, QImage, QPainter,
    QPalette, QPen, QPixmap, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QFileDialog, QFormLayout, QHeaderView, QProgressBar, QToolButton,
    QFileIconProvider, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QStackedWidget, QStyle, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PIL import ImageDraw
from pyvistaqt import QtInteractor
from UnityPy.export.MeshExporter import export_mesh_obj
from UnityPy.helpers.MeshHelper import MeshHandler

SHOWN_TYPES = ("Mesh", "Texture2D", "Sprite", "TextAsset")
MAX_TEXT_CHARS = 200_000
THUMB_TYPES = ("Mesh", "Texture2D", "Sprite")
THUMB_SIZE = 40      # icon size in the list
GRID_THUMB = 96      # thumbnail size generated (grid view uses it 1:1)
THUMB_CACHE_MAX = 4000
GRID_LIMIT = 20000
REPO_URL = "https://github.com/NightHawkHSI/UniView"
COMPAT_URL = "https://raw.githubusercontent.com/NightHawkHSI/UniView/main/compat.json"
SORT_ROLE = Qt.UserRole + 10   # value used when sorting a tree column
TYPE_ROLE = Qt.UserRole + 11   # type name stored on group rows
ALBEDO_PROPS = ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo", "_BaseColorTexture")
NORMAL_PROPS = ("_BumpMap", "_NormalMap")

if getattr(sys, "frozen", False):
    # Packaged .exe: user files live next to the exe, bundled files in the unpack dir.
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR
ICON_FILE = os.path.join(RESOURCE_DIR, "Icon.png")
LOG_FILE = os.path.join(APP_DIR, "viewer.log")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
COMPAT_CACHE = os.path.join(APP_DIR, "compat_cache.json")
COMPAT_BUNDLED = os.path.join(RESOURCE_DIR, "compat.json")
CRASH_FILE = os.path.join(APP_DIR, "crash.log")

log = logging.getLogger("viewer")


class LogBridge(QObject):
    """Carries log lines from any thread to the console widget (queued to the UI thread)."""

    message = Signal(str, int)  # formatted text, level number


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.bridge = LogBridge()

    def emit(self, record):
        try:
            self.bridge.message.emit(self.format(record), record.levelno)
        except RuntimeError:
            pass  # console already destroyed (app closing)


def setup_logging():
    """Log to viewer.log (rotating) and to the in-app console. Returns the console handler."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    console_handler = QtLogHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console_handler)
    for noisy in ("PIL", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return console_handler


_crash_stream = None  # kept open for faulthandler


def write_crash(kind, exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(CRASH_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {kind} =====\n{text}")
    except OSError:
        pass
    log.critical("%s - saved to crash.log\n%s", kind, text.rstrip())


def install_crash_handlers():
    """Python errors -> crash.log + dialog; hard crashes (segfaults) -> crash.log via faulthandler."""
    global _crash_stream

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        write_crash("Unhandled error", exc_type, exc, tb)
        app = QApplication.instance()
        if app is not None:
            try:
                QMessageBox.critical(app.activeWindow(), "Something went wrong",
                                     f"{exc_type.__name__}: {exc}\n\nDetails were saved to:\n{CRASH_FILE}")
            except Exception:
                pass

    def thread_excepthook(args):
        write_crash(f"Unhandled error in thread '{args.thread.name if args.thread else '?'}'",
                    args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:
        _crash_stream = open(CRASH_FILE, "a", encoding="utf-8")
        faulthandler.enable(_crash_stream, all_threads=True)
    except OSError:
        pass


def norm_path(path):
    return os.path.normcase(os.path.abspath(path))


# UnityPy objects share file readers, so every read goes through this lock
# (the thumbnail thread and the UI thread both read assets).
UNITY_LOCK = threading.RLock()


# --------------------------------------------------------------------------- loading

BUNDLE_MAGICS = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
RESOURCE_EXTS = (".ress", ".resource")
SKIP_EXTS = (".exe", ".dll", ".so", ".pdb", ".mdb", ".txt", ".log", ".json", ".xml",
             ".config", ".ini", ".cfg", ".png", ".jpg", ".bat", ".sys", ".manifest")


def looks_like_unity_file(path):
    """Sniff a file's header to decide whether UnityPy can load it."""
    lower = path.lower()
    if lower.endswith(RESOURCE_EXTS) or ".split" in os.path.basename(lower):
        return True  # streamed texture/mesh data referenced by .assets files
    if lower.endswith(SKIP_EXTS):
        return False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(48)
    except OSError:
        return False
    if head.startswith(BUNDLE_MAGICS):
        return True
    # Serialized file (.assets, level0, globalgamemanagers...): no magic, so check
    # that the big-endian header's version and recorded file size are sane.
    if len(head) < 48:
        return False
    version = int.from_bytes(head[8:12], "big")
    if not 5 <= version <= 50:
        return False
    if version < 22:
        return int.from_bytes(head[4:8], "big") == size
    return int.from_bytes(head[24:32], "big") == size


def find_unity_files(root):
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if looks_like_unity_file(path):
                found.append(path)
    return sorted(found)


class Loader(QObject):
    """Loads a Unity environment off the UI thread and lists interesting objects."""

    progress = Signal(str)
    progress_value = Signal(int, int)  # done, total (total 0 = busy)
    finished = Signal(object, object, int)  # env, {type_name: [(name, ObjectReader)]}, file count
    failed = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            started = time.time()
            self.progress.emit(f"Scanning {self.path} for Unity files ...")
            self.progress_value.emit(0, 0)
            log.info("Scanning %s for Unity files", self.path)
            files = find_unity_files(self.path)
            if not files:
                raise FileNotFoundError(f"No Unity asset files found in:\n{self.path}")
            log.info("Found %d Unity files in %.1fs", len(files), time.time() - started)
            root = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            env = UnityPy.Environment(path=root)
            failed = 0
            for n, path in enumerate(files, 1):
                rel = os.path.relpath(path, root)
                self.progress.emit(f"Loading file {n}/{len(files)}: {rel}")
                self.progress_value.emit(n - 1, len(files))
                log.info("Loading %d/%d  %s  (%.1f MB)", n, len(files), rel, os.path.getsize(path) / 1e6)
                try:
                    env.load_files([path])
                except Exception as e:
                    failed += 1  # one broken/encrypted file shouldn't stop the rest
                    log.warning("Could not load %s: %s: %s", rel, type(e).__name__, e)
            log.info("Indexing objects...")
            self.progress.emit("Indexing objects ...")
            self.progress_value.emit(0, 0)
            groups = {t: [] for t in SHOWN_TYPES}
            for i, obj in enumerate(env.objects):
                type_name = obj.type.name
                if type_name not in groups:
                    continue
                try:
                    name = obj.peek_name()
                except Exception:
                    name = None
                if not name:
                    # Unnamed (common for combined/prefab meshes): name it after where it lives.
                    container = getattr(obj, "container", None)
                    stem = os.path.splitext(os.path.basename(container))[0] if container else type_name
                    name = f"{stem} #{obj.path_id % 100000:05d}"
                groups[type_name].append((name, obj))
                if i % 2000 == 0:
                    self.progress.emit(f"Indexing objects ... {i}")
            for items in groups.values():
                items.sort(key=lambda x: x[0].lower())
            counts = ", ".join(f"{len(v):,} {k}" for k, v in groups.items())
            log.info("Done in %.1fs: %s (%d file(s) failed)", time.time() - started, counts, failed)
            self.finished.emit(env, groups, len(files))
        except Exception:
            tb = traceback.format_exc()
            log.error("Loading failed:\n%s", tb.rstrip())
            self.failed.emit(tb)


def obj_key(assets_file, path_id):
    return (id(assets_file), path_id)


class TextureFinder:
    """Finds what uses a mesh and which materials/textures it has (renderer -> material)."""

    def __init__(self, env):
        self.env = env
        self._index = None  # mesh key -> [(gameobject name, [material PPtr])]
        self._tex_users = None  # texture key -> {mesh keys}

    def _build(self):
        log.info("Indexing which objects use which meshes/materials (first model view only)...")
        started = time.time()
        self._index = {}
        for obj in self.env.objects:
            tname = obj.type.name
            if tname not in ("MeshFilter", "SkinnedMeshRenderer"):
                continue
            try:
                comp = obj.read()
                mesh_ptr = comp.m_Mesh
                if mesh_ptr.path_id == 0:
                    continue
                go = comp.m_GameObject.deref_parse_as_object()
                if tname == "MeshFilter":
                    mats = self._materials_of_gameobject(go)
                else:
                    mats = list(comp.m_Materials)
                key = obj_key(mesh_ptr.assetsfile, mesh_ptr.path_id)
                self._index.setdefault(key, []).append((go.m_Name, mats))
            except Exception:
                continue
        log.info("Indexed %d meshes with renderers in %.1fs", len(self._index), time.time() - started)

    @staticmethod
    def _materials_of_gameobject(go):
        for comp in go.m_Component:
            ptr = comp.component if hasattr(comp, "component") else comp[1]
            try:
                reader = ptr.deref()
            except Exception:
                continue
            if reader.type.name == "MeshRenderer":
                return list(reader.read().m_Materials)
        return []

    def info(self, mesh_obj):
        """Return (gameobject names, materials).

        materials: [{"name": str, "textures": [(property, texture name, ObjectReader)]}],
        one per submesh slot, with albedo textures first.
        """
        with UNITY_LOCK:
            if self._index is None:
                self._build()
            users = self._index.get(obj_key(mesh_obj.assets_file, mesh_obj.path_id), [])
            names = [name for name, _ in users]
            mat_ptrs = next((mats for _, mats in users if mats), [])
            materials = []
            for ptr in mat_ptrs:
                try:
                    mat = ptr.deref_parse_as_object()
                    tex_envs = mat.m_SavedProperties.m_TexEnvs
                except Exception:
                    continue
                textures = []
                for prop, tex_env in tex_envs:
                    prop = getattr(prop, "name", prop)
                    tptr = tex_env.m_Texture
                    if tptr.path_id == 0:
                        continue
                    try:
                        reader = tptr.deref()
                        if reader.type.name != "Texture2D":
                            continue
                        textures.append((prop, reader.peek_name() or prop, reader))
                    except Exception:
                        continue
                textures.sort(key=lambda t: t[0] not in ALBEDO_PROPS)
                materials.append({"name": mat.m_Name, "textures": textures})
            return names, materials

    def texture_users(self, tex_obj):
        """Keys of the meshes whose materials use this texture."""
        with UNITY_LOCK:
            if self._index is None:
                self._build()
            if self._tex_users is None:
                self._build_texture_users()
        return self._tex_users.get(obj_key(tex_obj.assets_file, tex_obj.path_id), set())

    def _build_texture_users(self):
        started = time.time()
        self._tex_users = {}
        material_textures = {}
        for mesh_key, users in self._index.items():
            for _go, mats in users:
                for ptr in mats:
                    try:
                        mat_key = (id(ptr.assetsfile), ptr.path_id)
                    except Exception:
                        continue
                    if mat_key not in material_textures:
                        material_textures[mat_key] = self._texture_keys(ptr)
                    for tex_key in material_textures[mat_key]:
                        self._tex_users.setdefault(tex_key, set()).add(mesh_key)
        log.info("Indexed which models use which textures (%d textures) in %.1fs",
                 len(self._tex_users), time.time() - started)

    @staticmethod
    def _texture_keys(mat_ptr):
        try:
            tex_envs = mat_ptr.deref_parse_as_object().m_SavedProperties.m_TexEnvs
        except Exception:
            return ()
        keys = []
        for _prop, tex_env in tex_envs:
            ptr = tex_env.m_Texture
            if ptr.path_id:
                try:
                    keys.append(obj_key(ptr.assetsfile, ptr.path_id))
                except Exception:
                    pass
        return keys

    @staticmethod
    def main_texture(materials):
        """ObjectReader of the best albedo texture in `materials`, or None."""
        for mat in materials:
            if mat["textures"]:
                return mat["textures"][0][2]
        return None

    def find(self, mesh_obj):
        """Return a PIL image of the mesh's main texture, or None."""
        reader = self.main_texture(self.info(mesh_obj)[1])
        if reader is None:
            return None
        with UNITY_LOCK:
            return asset_image(reader.read())


# --------------------------------------------------------------------------- mesh conversion

SURFACE_TOPOLOGIES = (0, 1, 2)  # Triangles, TriangleStrip, Quads (not Lines/LineStrip/Points)


@contextmanager
def surface_submeshes(mesh):
    """Temporarily hide line/point submeshes, which UnityPy can't turn into triangles."""
    original = mesh.m_SubMeshes
    surfaces = [sm for sm in original or [] if int(sm.topology) in SURFACE_TOPOLOGIES]
    if original and not surfaces:
        raise ValueError("Mesh only has lines/points (no surfaces to show).")
    mesh.m_SubMeshes = surfaces
    try:
        yield len(original or []) - len(surfaces)
    finally:
        mesh.m_SubMeshes = original


def mesh_arrays(mesh):
    """Return (handler, points Nx3, triangles Mx3) in Unity's own coordinates."""
    handler = MeshHandler(mesh)
    handler.process()
    if not handler.m_VertexCount or not handler.m_Vertices:
        raise ValueError("Mesh has no vertices (it may be stripped or read/write disabled).")
    points = np.asarray(handler.m_Vertices, dtype=np.float32)[:, :3].copy()
    with surface_submeshes(mesh):
        triangles = handler.get_triangles()
    tris = [t for sub in triangles for t in sub if len(t) == 3]
    if not tris:
        raise ValueError("Mesh has no triangles.")
    return handler, points, np.asarray(tris, dtype=np.int64)


def unity_mesh_to_polydata(mesh):
    """Convert a UnityPy Mesh into a pyvista PolyData (x flipped like Unity -> OBJ)."""
    handler, points, tris = mesh_arrays(mesh)
    points[:, 0] *= -1  # Unity is left-handed
    tris = tris[:, ::-1]  # flip winding to match x flip
    faces = np.hstack([np.full((len(tris), 1), 3, dtype=np.int64), tris]).ravel()

    poly = pv.PolyData(points, faces)
    channels = []
    for i in range(8):
        uv = getattr(handler, f"m_UV{i}", None)
        if uv and len(uv) == len(points):
            poly.point_data[f"UV{i}"] = np.asarray(uv, dtype=np.float32)[:, :2]
            channels.append(f"UV{i}")
    if channels:
        poly.active_texture_coordinates = poly.point_data[channels[0]]
    if handler.m_Colors and len(handler.m_Colors) == len(points):
        colors = np.asarray(handler.m_Colors, dtype=np.float32)
        if colors.max() <= 1.0:
            colors = colors * 255
        poly.point_data["vertex_colors"] = np.clip(colors, 0, 255).astype(np.uint8)
    return poly


def render_mesh_thumbnail(points, tris, size, max_tris=20000):
    """Cheap software render of a mesh: flat-shaded, seen from front-right and above."""
    pts = points - (points.min(0) + points.max(0)) / 2
    yaw, pitch = np.radians(35), np.radians(-25)
    ry = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    rx = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    pts = pts @ (rx @ ry).T
    if len(tris) > max_tris:
        tris = tris[np.random.default_rng(0).choice(len(tris), max_tris, replace=False)]

    ss = size * 3  # supersample, then shrink for anti-aliasing
    extent = float(np.abs(pts[:, :2]).max()) or 1.0
    scr = (pts[:, :2] / extent * 0.46 + 0.5) * ss
    scr[:, 1] = ss - scr[:, 1]

    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    normals = np.cross(b - a, c - a)
    facing = np.abs(normals[:, 2]) / (np.linalg.norm(normals, axis=1) + 1e-12)
    shade = 0.3 + 0.7 * facing
    order = np.argsort(-(a[:, 2] + b[:, 2] + c[:, 2]))  # far to near

    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    base = np.array([150, 185, 225])
    tri_scr = scr[tris]
    for i in order:
        r, g, bl = (base * shade[i]).astype(int)
        draw.polygon([tuple(p) for p in tri_scr[i]], fill=(r, g, bl, 255))
    return img.resize((size, size), Image.LANCZOS)


def asset_image(asset):
    """PIL image of a Texture2D/Sprite, with a clear error for textures that have no pixels.

    Some textures (e.g. "Font Texture") are generated while the game runs, so the files only
    hold an empty 0x0 placeholder; UnityPy would otherwise fail with a confusing file error.
    """
    width = getattr(asset, "m_Width", None)
    stream = getattr(asset, "m_StreamData", None)
    if width is not None and (width == 0 or asset.m_Height == 0 or (
            not getattr(asset, "image_data", None) and (stream is None or not stream.path))):
        raise ValueError("Empty texture - it's created while the game runs (e.g. a font), "
                         "so there are no pixels saved in the game files.")
    return asset.image


def make_thumbnail(type_name, obj, size):
    with UNITY_LOCK:
        asset = obj.read()
        if type_name == "Mesh":
            _, points, tris = mesh_arrays(asset)
        else:
            img = asset_image(asset)
    if type_name == "Mesh":
        return render_mesh_thumbnail(points, tris, size)
    img = img.convert("RGBA")
    scale = size / max(img.width, img.height)
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    # Pixel-art look for tiny textures, smooth for big ones.
    return img.resize(new_size, Image.NEAREST if scale > 1 else Image.LANCZOS)


def thumb_key(obj):
    return obj_key(obj.assets_file, obj.path_id)


def pil_to_qimage(img):
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    return QImage(data, img.width, img.height, QImage.Format_RGBA8888).copy()


def pil_to_pixmap(img):
    return QPixmap.fromImage(pil_to_qimage(img))


class ThumbnailWorker(QObject):
    """Background thread that makes thumbnails. Panel jobs go before list jobs."""

    ready = Signal(object, QImage)  # key, image (null image = no preview)

    def __init__(self, size):
        super().__init__()
        self.size = size
        self._priority = []
        self._visible = []
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def request_visible(self, jobs):
        """Replace the list jobs (only what's on screen matters)."""
        with self._cv:
            self._visible = list(jobs)
            self._cv.notify()

    def request_priority(self, jobs):
        with self._cv:
            self._priority = list(jobs) + self._priority
            self._cv.notify()

    def clear(self):
        with self._cv:
            self._priority, self._visible = [], []

    def _run(self):
        while True:
            with self._cv:
                while not self._priority and not self._visible:
                    self._cv.wait()
                key, type_name, obj = (self._priority or self._visible).pop(0)
            try:
                qimg = pil_to_qimage(make_thumbnail(type_name, obj, self.size))
            except Exception as e:
                log.debug("No thumbnail for %s %s: %s", type_name, obj.path_id, e)
                qimg = QImage()
            self.ready.emit(key, qimg)


def safe_filename(name):
    cleaned = "".join(c if c.isalnum() or c in " ._-#" else "_" for c in name)
    return cleaned.strip(" ._") or "unnamed"


# --------------------------------------------------------------------------- stats & search

def fmt_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def asset_id(obj):
    """Id that stays the same between sessions (file name + path id); used for favorites."""
    return f"{obj.assets_file.name}:{obj.path_id}"


def triangle_count(submeshes):
    tris = 0
    for sm in submeshes or []:
        topology, count = int(sm.topology), sm.indexCount
        if topology == 0:
            tris += count // 3
        elif topology == 1:
            tris += max(count - 2, 0)
        elif topology == 2:
            tris += count // 2
    return tris


def asset_stats(type_name, obj):
    """Numbers for sorting/filtering, read without decoding images or vertex data."""
    stats = {"size": obj.byte_size}
    if type_name == "TextAsset":
        stats.update(info="", sort=obj.byte_size)
        return stats
    with UNITY_LOCK:
        asset = obj.read()
    if type_name == "Mesh":
        tris = triangle_count(asset.m_SubMeshes)
        vertex_data = getattr(asset, "m_VertexData", None)
        verts = vertex_data.m_VertexCount if vertex_data is not None else 0
        stats.update(tris=tris, verts=verts, info=f"{tris:,} tris", sort=tris)
    elif type_name == "Texture2D":
        w, h = asset.m_Width, asset.m_Height
        stream = getattr(asset, "m_StreamData", None)
        if stream is not None and stream.size:
            stats["size"] += stream.size  # pixel data lives in the .resS file
        stats.update(w=w, h=h, info=f"{w}\u00d7{h}", sort=w * h)
    elif type_name == "Sprite":
        w, h = int(asset.m_Rect.width), int(asset.m_Rect.height)
        stats.update(w=w, h=h, info=f"{w}\u00d7{h}", sort=w * h)
    return stats


class StatsWorker(QObject):
    """Background thread that measures every asset (tris, pixel size, bytes) for sort/filter."""

    ready = Signal(object, int)  # [(key, stats)], jobs remaining

    def __init__(self):
        super().__init__()
        self._jobs = []
        self._gen = 0
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True, name="asset-stats").start()

    def start(self, jobs):
        with self._cv:
            self._gen += 1
            self._jobs = list(jobs)
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._jobs:
                    self._cv.wait()
                gen = self._gen
                batch_jobs, self._jobs = self._jobs[:40], self._jobs[40:]
            batch = []
            for key, type_name, obj in batch_jobs:
                try:
                    batch.append((key, asset_stats(type_name, obj)))
                except Exception as e:
                    log.debug("No stats for %s %s: %s", type_name, obj.path_id, e)
                    batch.append((key, {"size": obj.byte_size, "info": "?", "sort": -1}))
            with self._cv:
                if gen != self._gen:
                    continue  # a different game was opened meanwhile
                remaining = len(self._jobs)
            self.ready.emit(batch, remaining)


FILTER_HELP = """Search by name, or add filters (no spaces around the sign):
  tris>1000     triangles (models)
  verts<500     vertices (models)
  size>1mb      data size (kb, mb, gb)
  w>=512 h>=512 width / height (textures, sprites)
  type:mesh     type (mesh, texture, sprite, text)
Example:  gun tris>2000 size<5mb"""
FILTER_FIELDS = {"tris": "tris", "verts": "verts", "size": "size",
                 "w": "w", "width": "w", "h": "h", "height": "h"}
FILTER_TOKEN = re.compile(r"^([a-z]+)(>=|<=|>|<|=|:)(.+)$", re.I)
FILTER_OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le, "=": operator.eq}
FILTER_UNITS = {"": 1, "k": 1e3, "m": 1e6, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
TYPE_ALIASES = {"texture": "texture2d", "tex": "texture2d", "model": "mesh", "text": "textasset"}


def parse_number(text):
    match = re.fullmatch(r"([\d.]+)\s*([a-z]*)", text.lower())
    if not match or match.group(2) not in FILTER_UNITS:
        raise ValueError(text)
    return float(match.group(1)) * FILTER_UNITS[match.group(2)]


class AssetFilter:
    """Parsed search box text: plain words + field conditions like tris>1000."""

    def __init__(self, text):
        self.words, self.conditions, self.type = [], [], None
        for token in text.split():
            match = FILTER_TOKEN.match(token)
            key = match.group(1).lower() if match else ""
            if match and key == "type":
                value = match.group(3).lower()
                self.type = TYPE_ALIASES.get(value, value)
                continue
            if match and key in FILTER_FIELDS and match.group(2) != ":":
                try:
                    self.conditions.append((FILTER_FIELDS[key], FILTER_OPS[match.group(2)],
                                            parse_number(match.group(3))))
                    continue
                except ValueError:
                    pass
            self.words.append(token.lower())

    def matches(self, name, type_name, stats):
        low = name.lower()
        if any(word not in low for word in self.words):
            return False
        if self.type and not type_name.lower().startswith(self.type):
            return False
        for field, op, number in self.conditions:
            value = (stats or {}).get(field)
            if value is None or not op(value, number):
                return False
        return True


# --------------------------------------------------------------------------- export helpers

def export_subfolder(type_name, obj, name):
    """Folder that mirrors where the asset lives in the game (container path or source file)."""
    container = getattr(obj, "container", None)
    if container:
        parts = container.replace("\\", "/").strip("/").split("/")
        stem = os.path.splitext(parts[-1])[0]
        folder = parts[:-1] + ([stem] if stem.lower() != name.lower() else [])
    else:
        folder = [obj.assets_file.name or "unknown", type_name]
    return os.path.join(*[safe_filename(part)[:80] for part in folder if part] or ["."])


def write_glb(mesh, materials, path):
    """Binary glTF: positions, normals, UVs, vertex colors and embedded base-color textures.

    One primitive per submesh, each with its material. Opens directly in Blender.
    """
    handler = MeshHandler(mesh)
    handler.process()
    if not handler.m_VertexCount or not handler.m_Vertices:
        raise ValueError("Mesh has no vertices (it may be stripped or read/write disabled).")
    points = np.asarray(handler.m_Vertices, dtype=np.float32)[:, :3].copy()
    points[:, 0] *= -1  # Unity is left-handed, glTF right-handed
    count = len(points)
    keep = [i for i, sm in enumerate(mesh.m_SubMeshes or [])
            if int(sm.topology) in SURFACE_TOPOLOGIES]
    with surface_submeshes(mesh):
        submeshes = handler.get_triangles()

    buf, views, accessors = bytearray(), [], []

    def add_view(data, target=None):
        while len(buf) % 4:
            buf.append(0)
        view = {"buffer": 0, "byteOffset": len(buf), "byteLength": len(data)}
        if target:
            view["target"] = target
        buf.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(arr, kind, component=5126, target=34962, bounds=False):
        arr = np.ascontiguousarray(arr)
        acc = {"bufferView": add_view(arr.tobytes(), target), "componentType": component,
               "count": len(arr), "type": kind}
        if bounds:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    attributes = {"POSITION": add_accessor(points, "VEC3", bounds=True)}
    if handler.m_Normals and len(handler.m_Normals) == count:
        normals = np.asarray(handler.m_Normals, dtype=np.float32)[:, :3].copy()
        normals[:, 0] *= -1
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.where(lengths > 1e-8, normals / np.maximum(lengths, 1e-8), [0, 1, 0]).astype(np.float32)
        attributes["NORMAL"] = add_accessor(normals, "VEC3")
    if handler.m_UV0 and len(handler.m_UV0) == count:
        uv = np.asarray(handler.m_UV0, dtype=np.float32)[:, :2].copy()
        uv[:, 1] = 1.0 - uv[:, 1]  # glTF's UV origin is top-left
        attributes["TEXCOORD_0"] = add_accessor(uv, "VEC2")
    if handler.m_Colors and len(handler.m_Colors) == count:
        colors = np.asarray(handler.m_Colors, dtype=np.float32)[:, :4]
        if colors.max() > 1.0:
            colors = colors / 255.0
        attributes["COLOR_0"] = add_accessor(np.clip(colors, 0, 1).astype(np.float32), "VEC4")

    gl_materials, images, textures, texture_index = [], [], [], {}
    for mat in materials:
        entry = {"name": mat["name"] or "material", "doubleSided": True,
                 "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        reader = next((r for prop, _n, r in mat["textures"] if prop not in NORMAL_PROPS), None)
        if reader is not None:
            key = thumb_key(reader)
            if key not in texture_index:
                texture_index[key] = None
                try:
                    png = io.BytesIO()
                    asset_image(reader.read()).convert("RGBA").save(png, "PNG")
                    images.append({"bufferView": add_view(png.getvalue()), "mimeType": "image/png",
                                   "name": reader.peek_name() or "texture"})
                    textures.append({"source": len(images) - 1, "sampler": 0})
                    texture_index[key] = len(textures) - 1
                except Exception as e:
                    log.warning("Could not embed a texture in %s: %s", os.path.basename(path), e)
            if texture_index[key] is not None:
                entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_index[key]}
        gl_materials.append(entry)

    primitives = []
    for j, tris in enumerate(submeshes):
        tris = [t for t in tris if len(t) == 3]
        if not tris:
            continue
        indices = np.asarray(tris, dtype=np.uint32)[:, ::-1].ravel()  # winding flips with x
        prim = {"attributes": attributes, "mode": 4,
                "indices": add_accessor(indices, "SCALAR", 5125, 34963)}
        if gl_materials:
            slot = keep[j] if j < len(keep) else j
            prim["material"] = min(slot, len(gl_materials) - 1)
        primitives.append(prim)
    if not primitives:
        raise ValueError("Mesh has no triangles to export.")

    gltf = {
        "asset": {"version": "2.0", "generator": f"{APP_SHORT} {__version__}"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": mesh.m_Name or "mesh"}],
        "meshes": [{"name": mesh.m_Name or "mesh", "primitives": primitives}],
        "accessors": accessors, "bufferViews": views,
    }
    if gl_materials:
        gltf["materials"] = gl_materials
    if images:
        gltf.update(images=images, textures=textures,
                    samplers=[{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}])
    while len(buf) % 4:
        buf.append(0)
    gltf["buffers"] = [{"byteLength": len(buf)}]
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(buf)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(buf), 0x004E4942))
        f.write(buf)
    return [path]


def render_uv_layout(poly, channel, texture_img, max_tris=80000):
    """Draw a mesh's UV triangles over its texture (or a grid) to check layouts / lightmap UVs."""
    uv = np.asarray(poly.point_data[channel])
    faces = poly.faces.reshape(-1, 4)[:, 1:][:max_tris]
    if texture_img is not None:
        base = texture_img.convert("RGBA")
        if max(base.size) < 1024:
            factor = max(1, 1024 // max(base.size))
            base = base.resize((base.width * factor, base.height * factor), Image.NEAREST)
        else:
            base = base.copy()
    else:
        base = Image.new("RGBA", (1024, 1024), (38, 38, 38, 255))
        grid = ImageDraw.Draw(base)
        for i in range(0, 1025, 128):
            grid.line([(i, 0), (i, 1024)], fill=(70, 70, 70, 255))
            grid.line([(0, i), (1024, i)], fill=(70, 70, 70, 255))
    w, h = base.size
    pts = np.column_stack([uv[:, 0] * w, (1.0 - uv[:, 1]) * h])
    draw = ImageDraw.Draw(base, "RGBA")
    for tri in faces:
        a, b, c = pts[tri]
        draw.line([tuple(a), tuple(b), tuple(c), tuple(a)], fill=(0, 255, 140, 210), width=1)
    return base


# --------------------------------------------------------------------------- settings, Blender, compat list

class Settings(dict):
    """Small per-user settings file (settings.json next to the app)."""

    def __init__(self):
        super().__init__()
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self, f, indent=2)
        except OSError as e:
            log.warning("Could not save settings: %s", e)


def _version_key(path):
    return [int(n) for n in re.findall(r"\d+", os.path.basename(os.path.dirname(path)))]


def find_blender(saved=None):
    """Path of blender.exe: saved choice, PATH, registry, Program Files, then Steam."""
    candidates = [saved, shutil.which("blender")]
    if sys.platform == "win32":
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe") as key:
                        candidates.append(winreg.QueryValue(key, None).strip('"'))
                except OSError:
                    pass
        except ImportError:
            pass
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                found = glob.glob(os.path.join(base, "Blender Foundation", "*", "blender.exe"))
                candidates += sorted(found, key=_version_key, reverse=True)
        try:
            candidates += [os.path.join(common, "Blender", "blender.exe") for common in steam_library_dirs()]
        except Exception:
            pass
    return next((c for c in candidates if c and os.path.isfile(c)), None)


BLENDER_IMPORT = (
    "import bpy\n"
    "for o in list(bpy.data.objects):\n"
    "    bpy.data.objects.remove(o, do_unlink=True)\n"
    "bpy.ops.import_scene.gltf(filepath={path!r})\n"
)

COMPAT_STATUS = {"works": "\u2714 Works well", "partial": "\u25d0 Partly works", "broken": "\u2716 Doesn't work"}


def load_compat():
    """Community compatibility list: {install folder name (lowercase): entry}."""
    for path in (COMPAT_CACHE, COMPAT_BUNDLED):
        try:
            with open(path, encoding="utf-8") as f:
                games = json.load(f).get("games")
            if isinstance(games, dict):
                return {name.lower(): entry for name, entry in games.items() if isinstance(entry, dict)}
        except (OSError, ValueError, AttributeError):
            continue
    return {}


def fetch_compat():
    """Download the latest list from the repo (called from a background thread)."""
    with urllib.request.urlopen(COMPAT_URL, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data.get("games"), dict):
        raise ValueError("unexpected compat.json format")
    with open(COMPAT_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return len(data["games"])


def compat_report_url(project):
    name = project["name"]
    body = (f"**Game:** {name}\n"
            f"**Install folder name:** {os.path.basename(os.path.normpath(project['path']))}\n"
            f"**Status:** works / partial / broken  (keep one)\n"
            f"**UniView version:** {__version__}\n"
            f"**Unity files / assets:** {project.get('file_count', '?')} / {project.get('asset_count', '?')}\n\n"
            f"**What works / what doesn't:**\n\n")
    query = urllib.parse.urlencode({"title": f"Compatibility: {name}", "body": body, "labels": "compatibility"})
    return f"{REPO_URL}/issues/new?{query}"


# --------------------------------------------------------------------------- UI

def wheel_steps(event):
    """Scroll amount in 'notches' (mouse wheel = 1 per notch, touchpads give fractions)."""
    delta = event.angleDelta().y() or event.pixelDelta().y() * 4
    return delta / 120


class MeshInfoPanel(QWidget):
    """Right-hand panel for a model: stats, source file, materials and their textures."""

    apply_texture = Signal(object)  # ("Texture2D", name, ObjectReader)
    open_texture = Signal(object)   # jump to the texture in the asset list
    save_texture = Signal(object)
    save_model = Signal()
    open_blender = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.textures = QListWidget()
        self.textures.setIconSize(QSize(64, 64))
        self.textures.setSpacing(2)
        self.textures.itemClicked.connect(self._clicked)
        self.textures.itemDoubleClicked.connect(self._double_clicked)
        self.textures.setContextMenuPolicy(Qt.CustomContextMenu)
        self.textures.customContextMenuRequested.connect(self._context_menu)
        hint = QLabel("Click a texture to put it on the model. Double-click to jump to it in the list.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")

        save_tex = QPushButton("Save selected texture...")
        save_tex.clicked.connect(self._save_selected)
        save_model = QPushButton("Save model...")
        save_model.setToolTip("OBJ + MTL + PNG textures, or a single GLB with the textures inside.\n"
                              "Both open textured in Blender.")
        save_model.clicked.connect(self.save_model)
        blender = QPushButton("Open in Blender")
        blender.setToolTip("Exports the model as GLB and opens it in Blender (Ctrl+B).")
        blender.clicked.connect(self.open_blender)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.addWidget(self.title)
        lay.addWidget(self.details)
        lay.addWidget(QLabel("<b>Materials &amp; textures</b>"))
        lay.addWidget(self.textures, 1)
        lay.addWidget(hint)
        lay.addWidget(save_tex)
        lay.addWidget(save_model)
        lay.addWidget(blender)
        self._items = {}  # thumb key -> [QListWidgetItem]

    def set_info(self, name, details_html, materials, blank_icon):
        """Fill the panel; returns thumbnail jobs [(key, type, obj)] for the textures."""
        self.title.setText(name)
        self.details.setText(details_html)
        self.textures.clear()
        self._items = {}
        jobs = []
        if not materials:
            empty = QListWidgetItem("No materials/textures found for this model.\n"
                                    "Right-click any texture in the list on the left\n"
                                    "and pick 'Apply as texture to current model'.")
            empty.setFlags(Qt.NoItemFlags)
            self.textures.addItem(empty)
        for mat in materials:
            header = QListWidgetItem(f"Material: {mat['name'] or '(unnamed)'}")
            header.setFlags(Qt.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.textures.addItem(header)
            if not mat["textures"]:
                none = QListWidgetItem("    (no textures)")
                none.setFlags(Qt.NoItemFlags)
                self.textures.addItem(none)
            for prop, tex_name, reader in mat["textures"]:
                item = QListWidgetItem(blank_icon, f"{tex_name}\n{prop}")
                item.setData(Qt.UserRole, ("Texture2D", tex_name, reader))
                item.setToolTip(f"{tex_name}\nslot: {prop}\nfile: {reader.assets_file.name}")
                self.textures.addItem(item)
                key = thumb_key(reader)
                self._items.setdefault(key, []).append(item)
                jobs.append((key, "Texture2D", reader))
        return jobs

    def set_icon(self, key, icon):
        for item in self._items.get(key, []):
            item.setIcon(icon)

    def _data(self, item):
        return item.data(Qt.UserRole) if item else None

    def _clicked(self, item):
        if self._data(item):
            self.apply_texture.emit(self._data(item))

    def _double_clicked(self, item):
        if self._data(item):
            self.open_texture.emit(self._data(item))

    def _save_selected(self):
        items = self.textures.selectedItems()
        if items and self._data(items[0]):
            self.save_texture.emit(self._data(items[0]))

    def _context_menu(self, pos):
        data = self._data(self.textures.itemAt(pos))
        if not data:
            return
        menu = QMenu(self)
        menu.addAction("Put on model", lambda: self.apply_texture.emit(data))
        menu.addAction("Go to texture in list", lambda: self.open_texture.emit(data))
        menu.addAction("Save texture (PNG)...", lambda: self.save_texture.emit(data))
        menu.exec(self.textures.viewport().mapToGlobal(pos))


class MeshView(QWidget):
    uv_layout_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plotter = QtInteractor(self)
        self.plotter.set_background("#2b2b2b")
        # Handle the mouse wheel ourselves so zoom works regardless of focus/touchpad.
        self.plotter.interactor.installEventFilter(self)
        self.poly = None
        self.texture_img = None

        self.wire = QCheckBox("Wireframe")
        self.edges = QCheckBox("Edges")
        self.use_tex = QCheckBox("Texture")
        self.use_tex.setChecked(True)
        self.flip_v = QCheckBox("Flip texture V")
        self.tex_alpha = QCheckBox("Texture alpha")
        self.tex_alpha.setToolTip("Use the texture's alpha channel as transparency.\n"
                                  "Off by default: many games store other data there.")
        self.use_colors = QCheckBox("Vertex colors")
        self.use_colors.setChecked(True)
        for cb in (self.wire, self.edges, self.use_tex, self.flip_v, self.tex_alpha, self.use_colors):
            cb.toggled.connect(self.redraw)
        zoom_in = QPushButton("+")
        zoom_out = QPushButton("−")
        for btn, factor in ((zoom_in, 1.25), (zoom_out, 0.8)):
            btn.setFixedWidth(32)
            btn.setToolTip("Zoom (or use the mouse wheel)")
            btn.clicked.connect(lambda _=False, f=factor: self.zoom(f))
        reset = QPushButton("Reset camera")
        reset.clicked.connect(self.reset_camera)
        self.uv_combo = QComboBox()
        self.uv_combo.setToolTip("Which UV set maps the texture.\n"
                                 "UV0 is the normal one; UV1 is often the baked-lighting (lightmap) layout.")
        self.uv_combo.currentTextChanged.connect(self.set_uv_channel)
        uv_layout = QPushButton("UV layout")
        uv_layout.setToolTip("Show the UV triangles drawn over the texture (Ctrl+U).")
        uv_layout.clicked.connect(self.uv_layout_requested)

        self.info = QLabel()
        bar = QHBoxLayout()
        for w in (self.wire, self.edges, self.use_tex, self.flip_v, self.tex_alpha, self.use_colors,
                  zoom_in, zoom_out, reset, QLabel("UV:"), self.uv_combo, uv_layout):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.info)

        self.panel = MeshInfoPanel()
        split = QSplitter()
        split.addWidget(self.plotter.interactor)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setSizes([800, 300])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

    def eventFilter(self, obj, event):
        if obj is self.plotter.interactor and event.type() == QEvent.Wheel:
            self.zoom(1.2 ** wheel_steps(event))
            return True
        return super().eventFilter(obj, event)

    def zoom(self, factor):
        if self.poly is None or factor <= 0:
            return
        self.plotter.camera.zoom(factor)
        self.plotter.render()

    def reset_camera(self):
        self.plotter.reset_camera()
        self.plotter.render()

    def uv_channels(self):
        if self.poly is None:
            return []
        return sorted(k for k in self.poly.point_data.keys() if re.fullmatch(r"UV\d", k))

    def set_uv_channel(self, name):
        if self.poly is None or name not in self.poly.point_data:
            return
        self.poly.active_texture_coordinates = self.poly.point_data[name]
        self.redraw()

    def show_mesh(self, poly, texture_img):
        self.poly = poly
        self.texture_img = texture_img
        with QSignalBlocker(self.uv_combo):
            self.uv_combo.clear()
            channels = self.uv_channels()
            self.uv_combo.addItems(channels or ["none"])
            self.uv_combo.setEnabled(len(channels) > 1)
        tex_note = "textured" if texture_img is not None else "no texture found"
        self.info.setText(f"{poly.n_points:,} verts  {poly.n_cells:,} tris  ({tex_note})")
        self.redraw(reset_camera=True)

    def set_texture(self, img):
        self.texture_img = img
        self.redraw()

    def redraw(self, *_, reset_camera=False):
        self.plotter.clear()
        if self.poly is None:
            return
        kwargs = dict(
            style="wireframe" if self.wire.isChecked() else "surface",
            show_edges=self.edges.isChecked(),
            smooth_shading=True,
            split_sharp_edges=True,
        )
        has_uv = self.poly.active_texture_coordinates is not None
        if self.use_tex.isChecked() and self.texture_img is not None and has_uv:
            arr = np.asarray(self.texture_img.convert("RGBA" if self.tex_alpha.isChecked() else "RGB"))
            if self.flip_v.isChecked():
                arr = arr[::-1]
            kwargs["texture"] = pv.Texture(np.ascontiguousarray(arr))
        elif self.use_colors.isChecked() and "vertex_colors" in self.poly.point_data:
            kwargs.update(scalars="vertex_colors", rgba=True)
        else:
            kwargs["color"] = "#c8c8c8"
        self.plotter.add_mesh(self.poly, **kwargs)
        if reset_camera:
            self.plotter.reset_camera()
        self.plotter.render()


def checker_brush(cell=10):
    pix = QPixmap(cell * 2, cell * 2)
    pix.fill(QColor("#555"))
    painter = QPainter(pix)
    painter.fillRect(0, 0, cell, cell, QColor("#444"))
    painter.fillRect(cell, cell, cell, cell, QColor("#444"))
    painter.end()
    return QBrush(pix)


class ZoomGraphicsView(QGraphicsView):
    """Graphics view that zooms under the mouse on wheel and pans by dragging."""

    zoomed = Signal()
    MIN_SCALE, MAX_SCALE = 0.02, 64.0

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(checker_brush())
        self.setFrameShape(QGraphicsView.NoFrame)

    def scale_factor(self):
        return self.transform().m11()

    def zoom_by(self, factor):
        new = min(max(self.scale_factor() * factor, self.MIN_SCALE), self.MAX_SCALE)
        factor = new / self.scale_factor()
        self.scale(factor, factor)
        self.zoomed.emit()

    def wheelEvent(self, event):
        self.zoom_by(1.25 ** wheel_steps(event))


class ImageView(QWidget):
    """Texture viewer: wheel to zoom, drag to pan, save as PNG. Optional 'used by' links on the right."""

    save_requested = Signal()
    goto_requested = Signal(object)  # asset key

    def __init__(self, parent=None, show_links=True):
        super().__init__(parent)
        self.image = None
        self.scene = QGraphicsScene(self)
        self.pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self.pix_item)
        self.view = ZoomGraphicsView(self.scene)
        self.view.zoomed.connect(self._update_zoom)

        fit = QPushButton("Fit")
        fit.clicked.connect(self.fit)
        actual = QPushButton("100%")
        actual.clicked.connect(self.actual_size)
        zoom_in = QPushButton("+")
        zoom_in.clicked.connect(lambda: self.view.zoom_by(1.25))
        zoom_out = QPushButton("−")
        zoom_out.clicked.connect(lambda: self.view.zoom_by(0.8))
        for b in (zoom_in, zoom_out):
            b.setFixedWidth(32)
        self.zoom_label = QLabel()
        self.info = QLabel()
        save = QPushButton("Save PNG...")
        save.clicked.connect(self.save_requested)

        bar = QHBoxLayout()
        for w in (fit, actual, zoom_in, zoom_out, self.zoom_label):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.info)
        bar.addWidget(save)
        self.links_title = QLabel()
        self.links_title.setWordWrap(True)
        self.links = QListWidget()
        self.links.setIconSize(QSize(48, 48))
        self.links.setWordWrap(True)
        self.links.itemActivated.connect(self._link_clicked)
        self.links.itemClicked.connect(self._link_clicked)
        links_box = QWidget()
        links_lay = QVBoxLayout(links_box)
        links_lay.setContentsMargins(6, 0, 0, 0)
        links_lay.addWidget(self.links_title)
        links_lay.addWidget(self.links, 1)
        hint = QLabel("Click one to jump to it.")
        hint.setStyleSheet("color: gray;")
        links_lay.addWidget(hint)
        links_box.setVisible(show_links)
        self._link_items = {}

        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(links_box)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 260])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

    def set_links(self, title, entries, empty_text):
        """entries: [(key, text, icon)] shown in the side list."""
        self.links_title.setText(f"<b>{title}</b>")
        self.links.clear()
        self._link_items = {}
        if not entries:
            item = QListWidgetItem(empty_text)
            item.setFlags(Qt.NoItemFlags)
            self.links.addItem(item)
        for key, text, icon in entries:
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, key)
            self.links.addItem(item)
            self._link_items[key] = item

    def set_link_icon(self, key, icon):
        item = self._link_items.get(key)
        if item is not None:
            item.setIcon(icon)

    def _link_clicked(self, item):
        key = item.data(Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)

    def show_image(self, img, name=""):
        self.image = img
        pix = pil_to_pixmap(img)
        self.pix_item.setPixmap(pix)
        self.scene.setSceneRect(pix.rect())
        self.info.setText(f"{name}  {img.width}x{img.height}")
        QTimer.singleShot(0, self.fit)  # after the view has its final size

    def fit(self):
        self.view.resetTransform()
        self.view.fitInView(self.pix_item, Qt.KeepAspectRatio)
        self._update_zoom()

    def actual_size(self):
        self.view.resetTransform()
        self._update_zoom()

    def _update_zoom(self):
        scale = self.view.scale_factor()
        # Blend when shrinking, keep pixels crisp when magnifying.
        mode = Qt.SmoothTransformation if scale < 1 else Qt.FastTransformation
        self.pix_item.setTransformationMode(mode)
        self.zoom_label.setText(f"{scale * 100:.0f}%")


class ImageWindow(QDialog):
    """Separate zoomable image window (used for UV layouts)."""

    def __init__(self, img, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 900)
        self.view = ImageView(show_links=False)
        self.view.save_requested.connect(self._save)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self.view.show_image(img, title)
        self._title = title

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save", f"{safe_filename(self._title)}.png", "PNG image (*.png)")
        if path:
            self.view.image.save(path)
            log.info("Saved %s", path)


class ExportDialog(QDialog):
    """Options for bulk export: folder, model format, keep the game's folder structure."""

    def __init__(self, count, has_models, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Export")
        self.folder = QLineEdit(settings.get("export_dir", settings.get("last_dir", os.path.expanduser("~"))))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.format = QComboBox()
        self.format.addItem("OBJ + MTL + PNG textures", "obj")
        self.format.addItem("GLB (one file, textures inside)", "glb")
        self.format.setCurrentIndex(max(0, self.format.findData(settings.get("model_format", "obj"))))
        self.format.setEnabled(has_models)
        self.keep = QCheckBox("Keep the game's folder structure")
        self.keep.setToolTip("Puts each asset in folders that mirror where it lives in the game\n"
                             "(e.g. assets/prefabs/weapons/...), so big dumps stay easy to browse.")
        self.keep.setChecked(settings.get("keep_structure", True))
        form = QFormLayout(self)
        form.addRow(QLabel(f"{count:,} item(s) will be exported."))
        form.addRow("Folder:", folder_row)
        form.addRow("Models as:", self.format)
        form.addRow(self.keep)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(560, 0)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Export to", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _accept(self):
        if not self.folder.text().strip():
            return
        self.settings.update(export_dir=self.folder.text().strip(), model_format=self.format.currentData(),
                             keep_structure=self.keep.isChecked())
        self.settings.save()
        self.accept()

    def values(self):
        return self.folder.text().strip(), self.format.currentData(), self.keep.isChecked()


class SortItem(QTreeWidgetItem):
    """Tree row that sorts by SORT_ROLE numbers/strings instead of display text."""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        a, b = self.data(col, SORT_ROLE), other.data(col, SORT_ROLE)
        if a is None or b is None or type(a) is not type(b):
            a = -1 if a is None else a
            b = -1 if b is None else b
            if type(a) is not type(b):
                return str(a) < str(b)
        return a < b


# --------------------------------------------------------------------------- projects

PROJECTS_FILE = os.path.join(APP_DIR, "projects.json")
STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"


def unity_data_dir(game_dir):
    """Return the game's <Name>_Data folder if this looks like a Unity game, else None."""
    try:
        entries = os.listdir(game_dir)
    except OSError:
        return None
    for name in entries:
        path = os.path.join(game_dir, name)
        if name.endswith("_Data") and os.path.isdir(path):
            if any(os.path.exists(os.path.join(path, f))
                   for f in ("globalgamemanagers", "mainData", "data.unity3d", "level0")):
                return path
    return None


def game_exe(game_dir):
    data = unity_data_dir(game_dir)
    if data:
        exe = data[: -len("_Data")] + ".exe"
        if os.path.isfile(exe):
            return exe
    exes = [f for f in os.listdir(game_dir) if f.lower().endswith(".exe")] if os.path.isdir(game_dir) else []
    exes = [f for f in exes if "crash" not in f.lower() and "unins" not in f.lower()]
    return os.path.join(game_dir, exes[0]) if exes else None


def steam_library_dirs():
    libs = [STEAM_DEFAULT]
    vdf = os.path.join(STEAM_DEFAULT, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    seen, out = set(), []
    for lib in libs:
        common = os.path.join(lib, "steamapps", "common")
        key = os.path.normcase(os.path.abspath(common))
        if key not in seen and os.path.isdir(common):
            seen.add(key)
            out.append(common)
    return out


def find_steam_unity_games():
    games = []
    for common in steam_library_dirs():
        for name in sorted(os.listdir(common)):
            path = os.path.join(common, name)
            if os.path.isdir(path) and unity_data_dir(path):
                games.append((name, path))
    return games


class ProjectStore:
    """List of saved games, persisted to projects.json."""

    def __init__(self, path=PROJECTS_FILE):
        self.path = path
        self.projects = []
        try:
            with open(path, encoding="utf-8") as f:
                self.projects = json.load(f)
        except (OSError, ValueError):
            pass

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.projects, f, indent=2)

    def get(self, path):
        key = norm_path(path)
        return next((p for p in self.projects if norm_path(p["path"]) == key), None)

    def has(self, path):
        return self.get(path) is not None

    def set_counts(self, path, files=None, assets=None):
        project = self.get(path)
        if project is None:
            return
        if files is not None:
            project["file_count"] = files
        if assets is not None:
            project["asset_count"] = assets
        self.save()

    def add(self, name, path):
        if not self.has(path):
            self.projects.append({"name": name, "path": path, "last_opened": 0})
            self.save()
            log.info("Added game '%s' (%s)", name, path)

    def remove(self, project):
        self.projects.remove(project)
        self.save()

    def touch(self, project):
        project["last_opened"] = time.time()
        self.save()

    def sorted(self):
        """Pinned games first, then most recently opened."""
        return sorted(self.projects, key=lambda p: (not p.get("pinned"), -p.get("last_opened", 0),
                                                    p["name"].lower()))


class SteamPickDialog(QDialog):
    """Checklist of Unity games found in Steam libraries."""

    def __init__(self, games, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Unity games found in Steam")
        self.resize(480, 520)
        self.list = QListWidget()
        icons = QFileIconProvider()
        for name, path in games:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, path)
            exe = game_exe(path)
            if exe:
                item.setIcon(icons.icon(QFileInfo(exe)))
            if store.has(path):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setText(f"{name}  (already added)")
            else:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
            item.setToolTip(path)
            self.list.addItem(item)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the games to add:"))
        lay.addWidget(self.list)
        lay.addWidget(buttons)

    def chosen(self):
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.flags() & Qt.ItemIsEnabled and item.checkState() == Qt.Checked:
                out.append((item.text(), item.data(Qt.UserRole)))
        return out


class CardDelegate(QStyledItemDelegate):
    """Draws a game box: icon, name, counts, compatibility. Green = loaded, red = not loaded."""

    STATE_ROLE = Qt.UserRole + 1
    SUB_ROLE = Qt.UserRole + 2
    PIN_ROLE = Qt.UserRole + 3
    NOTES_ROLE = Qt.UserRole + 4
    STYLES = {  # state: (border, fill)
        "loaded": ("#43a047", QColor(67, 160, 71, 60)),
        "unloaded": ("#e53935", QColor(229, 57, 53, 40)),
        "missing": ("#888888", QColor(128, 128, 128, 35)),
        "action": ("#666666", QColor(0, 0, 0, 0)),
    }
    SIZE = QSize(176, 222)
    ICON = 80

    def sizeHint(self, option, index):
        return self.SIZE

    def paint(self, painter, option, index):
        state = index.data(self.STATE_ROLE) or "action"
        border, fill = self.STYLES[state]
        hover = bool(option.state & QStyle.State_MouseOver)
        rect = option.rect.adjusted(5, 5, -5, -5)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        fill = QColor(fill)
        if hover:
            fill = QColor(61, 142, 230, 45) if state == "action" else QColor(fill.red(), fill.green(),
                                                                              fill.blue(), fill.alpha() + 45)
        border = QColor(border).lighter(135) if hover else QColor(border)
        painter.setPen(QPen(border, 1 if state == "action" else 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect), 10, 10)

        # Corner badges: pinned (top-right), has notes (top-left).
        badge_font = QFont(option.font)
        badge_font.setPointSizeF(option.font.pointSizeF() * 1.1)
        painter.setFont(badge_font)
        painter.setPen(QColor("#f4c542"))
        if index.data(self.PIN_ROLE):
            painter.drawText(QRect(rect.right() - 26, rect.top() + 6, 20, 20), Qt.AlignCenter, "\U0001F4CC")
        if index.data(self.NOTES_ROLE):
            painter.drawText(QRect(rect.left() + 6, rect.top() + 6, 20, 20), Qt.AlignCenter, "\U0001F4DD")

        icon = index.data(Qt.DecorationRole)
        if icon is not None:
            icon.paint(painter, QRect(rect.center().x() - self.ICON // 2, rect.top() + 12, self.ICON, self.ICON))

        text_rect = QRect(rect.left() + 8, rect.top() + 20 + self.ICON,
                          rect.width() - 16, rect.height() - self.ICON - 26)
        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(option.palette.color(QPalette.Text))
        flags = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
        name = index.data(Qt.DisplayRole) or ""
        painter.drawText(text_rect, flags, name)

        sub = index.data(self.SUB_ROLE)
        if sub:
            used = QFontMetrics(name_font).boundingRect(text_rect, flags, name).height()
            small = QFont(option.font)
            small.setPointSizeF(max(7.0, option.font.pointSizeF() * 0.9))
            painter.setFont(small)
            painter.setPen(QColor("#a0a0a0"))
            painter.drawText(text_rect.adjusted(0, used + 4, 0, 0), flags, sub)
        painter.restore()


class HomePage(QWidget):
    """Start page: a box per saved game plus 'Add game' / 'Find Unity games' boxes."""

    open_requested = Signal(str)  # project path
    unload_requested = Signal(str)
    _counted = Signal(str, int)
    _compat_fetched = Signal(int)
    ADD, FIND = "__add__", "__find__"

    def __init__(self, store, is_loaded, settings, parent=None):
        super().__init__(parent)
        self.store = store
        self.is_loaded = is_loaded
        self.settings = settings
        self.icons = QFileIconProvider()
        self.compat = load_compat()
        self._counting = set()
        self._counted.connect(self._on_counted)
        self._compat_fetched.connect(self._on_compat_fetched)

        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        hint = QLabel("Pick a game to browse its models and textures.  "
                      "<span style='color:#43a047'>Green</span> = already loaded (opens instantly), "
                      "<span style='color:#e53935'>red</span> = not loaded yet.  "
                      "Drag a game folder here to add it.")
        hint.setStyleSheet("color: gray;")

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setGridSize(QSize(188, 234))
        self.grid.setUniformItemSizes(True)
        self.grid.setFocusPolicy(Qt.NoFocus)
        self.grid.setMouseTracking(True)
        self.grid.setItemDelegate(CardDelegate(self.grid))
        self.grid.setStyleSheet("QListWidget { border: none; background: transparent; }")
        self.grid.itemClicked.connect(self.on_activate)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self.on_context_menu)
        # Drag-and-drop a game folder (or any file inside it) onto the page.
        self.setAcceptDrops(True)
        self.grid.setAcceptDrops(True)
        self.grid.viewport().setAcceptDrops(True)
        self.grid.viewport().installEventFilter(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addWidget(self.grid, 1)
        self.refresh()
        if self.settings.get("online_compat", True):
            self.fetch_compat()

    # ---- compatibility list
    def fetch_compat(self):
        def work():
            try:
                n = fetch_compat()
            except Exception as e:
                log.info("Couldn't download the compatibility list (%s) - using the saved one", e)
                return
            self._compat_fetched.emit(n)

        threading.Thread(target=work, daemon=True, name="compat-fetch").start()

    def _on_compat_fetched(self, n):
        self.compat = load_compat()
        log.info("Compatibility list updated (%d games)", n)
        self.refresh()

    def compat_entry(self, path):
        return self.compat.get(os.path.basename(os.path.normpath(path)).lower())

    # ---- drag and drop
    @staticmethod
    def _dropped_folders(mime):
        folders = []
        for url in mime.urls() if mime.hasUrls() else []:
            path = url.toLocalFile()
            if path:
                folders.append(path if os.path.isdir(path) else os.path.dirname(path))
        return folders

    def eventFilter(self, obj, event):
        if obj is self.grid.viewport():
            kind = event.type()
            if kind in (QEvent.DragEnter, QEvent.DragMove) and self._dropped_folders(event.mimeData()):
                event.acceptProposedAction()
                return True
            if kind == QEvent.Drop:
                self._drop(event)
                return True
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        if self._dropped_folders(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self._drop(event)

    def _drop(self, event):
        folders = self._dropped_folders(event.mimeData())
        if not folders:
            return
        event.acceptProposedAction()
        # Leave the drag's event loop before showing dialogs.
        QTimer.singleShot(0, lambda: self.add_folders(folders))

    # ---- boxes
    def _action_item(self, text, icon, key):
        item = QListWidgetItem(self.style().standardIcon(icon), text)
        item.setData(Qt.UserRole, key)
        item.setData(CardDelegate.STATE_ROLE, "action")
        return item

    def refresh(self):
        self.grid.clear()
        for project in self.store.sorted():
            path = project["path"]
            exe = game_exe(path) if os.path.isdir(path) else None
            icon = (self.icons.icon(QFileInfo(exe)) if exe
                    else self.style().standardIcon(QStyle.SP_DirIcon))
            item = QListWidgetItem(icon, project["name"])
            item.setData(Qt.UserRole, path)
            compat = self.compat_entry(path)
            tip = [path]
            if not os.path.isdir(path):
                state, lines = "missing", ["Folder missing"]
            else:
                state = "loaded" if self.is_loaded(path) else "unloaded"
                files = project.get("file_count")
                if files is None:
                    self._count_files(path)
                parts = [f"{files:,} files" if files is not None else "counting files..."]
                if project.get("asset_count") is not None:
                    parts.append(f"{project['asset_count']:,} assets")
                lines = [" \u00b7 ".join(parts), "Loaded" if state == "loaded" else "Not loaded"]
            if compat and compat.get("status") in COMPAT_STATUS:
                lines.append(COMPAT_STATUS[compat["status"]])
                if compat.get("notes"):
                    tip.append(f"Community notes: {compat['notes']}")
            if project.get("notes"):
                tip.append(f"Your notes: {project['notes']}")
            if project.get("last_opened"):
                tip.append(f"Last opened: {datetime.fromtimestamp(project['last_opened']):%Y-%m-%d %H:%M}")
            item.setToolTip("\n\n".join(tip))
            item.setData(CardDelegate.STATE_ROLE, state)
            item.setData(CardDelegate.SUB_ROLE, "\n".join(lines))
            item.setData(CardDelegate.PIN_ROLE, bool(project.get("pinned")))
            item.setData(CardDelegate.NOTES_ROLE, bool(project.get("notes")))
            self.grid.addItem(item)
        self.grid.addItem(self._action_item("\uff0b Add game", QStyle.SP_FileDialogNewFolder, self.ADD))
        self.grid.addItem(self._action_item("Find Unity games in Steam",
                                            QStyle.SP_FileDialogContentsView, self.FIND))

    def _count_files(self, path):
        """Count a game's Unity files in the background (header sniffing, no loading)."""
        if path in self._counting:
            return
        self._counting.add(path)

        def work():
            try:
                n = len(find_unity_files(path))
            except Exception:
                log.exception("Counting files in %s failed", path)
                n = 0
            self._counted.emit(path, n)

        threading.Thread(target=work, daemon=True, name="count-files").start()

    def _on_counted(self, path, n):
        self._counting.discard(path)
        log.info("Counted %d Unity files in %s", n, path)
        self.store.set_counts(path, files=n)
        self.refresh()

    def on_activate(self, item):
        data = item.data(Qt.UserRole)
        if data == self.ADD:
            self.add_game()
        elif data == self.FIND:
            self.find_games()
        elif data:
            project = self.store.get(data)
            if project is None:
                return
            if not os.path.isdir(project["path"]):
                QMessageBox.warning(self, "Missing", f"Folder not found:\n{project['path']}")
                return
            self.store.touch(project)
            self.open_requested.emit(project["path"])

    def add_game(self):
        path = QFileDialog.getExistingDirectory(self, "Pick the game's install folder")
        if path:
            self.add_folders([path])

    def add_folders(self, paths):
        added = 0
        for path in paths:
            path = os.path.normpath(path)
            if self.store.has(path):
                log.info("Already in the list: %s", path)
                if len(paths) == 1:
                    QMessageBox.information(self, "Add game", "That game is already added.")
                continue
            if not unity_data_dir(path) and not find_unity_files(path):
                QMessageBox.warning(self, "Add game", f"No Unity asset files found in:\n{path}")
                continue
            name = os.path.basename(path)
            if len(paths) == 1:
                name, ok = QInputDialog.getText(self, "Add game", "Name:", text=name)
                if not ok or not name.strip():
                    continue
            self.store.add(name.strip(), path)
            added += 1
        if added:
            self.refresh()

    def find_games(self):
        log.info("Searching Steam libraries for Unity games...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            games = find_steam_unity_games()
        finally:
            QApplication.restoreOverrideCursor()
        log.info("Found %d Unity game(s) in Steam libraries", len(games))
        if not games:
            QMessageBox.information(self, "Find games", "No Unity games found in your Steam libraries.")
            return
        dlg = SteamPickDialog(games, self.store, self)
        if dlg.exec():
            for name, path in dlg.chosen():
                self.store.add(name, path)
            self.refresh()

    def on_context_menu(self, pos):
        item = self.grid.itemAt(pos)
        path = item.data(Qt.UserRole) if item else None
        project = self.store.get(path) if path not in (None, self.ADD, self.FIND) else None
        if project is None:
            return
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.on_activate(item))
        if self.is_loaded(path):
            menu.addAction("Unload from memory", lambda: self.unload_requested.emit(path))
        menu.addAction("Unpin" if project.get("pinned") else "Pin to top", lambda: self.toggle_pin(project))
        menu.addAction("Notes...", lambda: self.edit_notes(project))
        menu.addAction("Rename...", lambda: self.rename(project))
        menu.addSeparator()
        menu.addAction("Report compatibility...", lambda: self.report_compat(project))
        menu.addAction("Recount files", lambda: self._recount(project))
        menu.addAction("Show in Explorer", lambda: os.startfile(project["path"]))
        menu.addSeparator()
        menu.addAction("Remove from list", lambda: self.remove(project))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def toggle_pin(self, project):
        project["pinned"] = not project.get("pinned")
        self.store.save()
        log.info("%s '%s'", "Pinned" if project["pinned"] else "Unpinned", project["name"])
        self.refresh()

    def edit_notes(self, project):
        if edit_project_notes(self, project):
            self.store.save()
            self.refresh()

    def report_compat(self, project):
        url = compat_report_url(project)
        log.info("Opening a compatibility report for '%s' on GitHub", project["name"])
        webbrowser.open(url)

    def _recount(self, project):
        project.pop("file_count", None)
        self.refresh()

    def rename(self, project):
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=project["name"])
        if ok and name.strip():
            log.info("Renamed game '%s' -> '%s'", project["name"], name.strip())
            project["name"] = name.strip()
            self.store.save()
            self.refresh()

    def remove(self, project):
        if QMessageBox.question(self, "Remove", f"Remove '{project['name']}' from the list?\n"
                                "(Game files are not touched.)") == QMessageBox.Yes:
            if self.is_loaded(project["path"]):
                self.unload_requested.emit(project["path"])
            self.store.remove(project)
            log.info("Removed game '%s' from the list", project["name"])
            self.refresh()


def edit_project_notes(parent, project):
    """Ask for a game's notes. Returns True if they changed."""
    text, ok = QInputDialog.getMultiLineText(
        parent, f"Notes - {project['name']}",
        "Notes for this game (quirks, what works, where the good stuff is):", project.get("notes", ""))
    if not ok or text == project.get("notes", ""):
        return False
    project["notes"] = text.strip()
    log.info("Saved notes for '%s'", project["name"])
    return True


class ConsoleDock(QDockWidget):
    """Bottom panel showing the log as it happens."""

    COLORS = {logging.DEBUG: "#8a8a8a", logging.WARNING: "#e0a030",
              logging.ERROR: "#ef5350", logging.CRITICAL: "#ff4081"}

    def __init__(self, handler, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("console")
        self.handler = handler
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setMaximumBlockCount(5000)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)

        debug = QCheckBox("Show debug")
        debug.setToolTip("Also show low-level messages (e.g. assets that have no thumbnail).")
        debug.toggled.connect(lambda on: handler.setLevel(logging.DEBUG if on else logging.INFO))
        clear = QPushButton("Clear")
        clear.clicked.connect(self.text.clear)
        open_log = QPushButton("Open log file")
        open_log.clicked.connect(lambda: open_path(LOG_FILE))
        open_crash = QPushButton("Open crash log")
        open_crash.clicked.connect(lambda: open_path(CRASH_FILE))

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        for w in (debug, clear):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(open_log)
        bar.addWidget(open_crash)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 0, 4, 4)
        lay.addLayout(bar)
        lay.addWidget(self.text)
        self.setWidget(body)
        handler.bridge.message.connect(self.append)

    def append(self, text, level):
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.COLORS.get(level, self.palette().color(QPalette.Text))))
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text, fmt)
        if at_bottom:
            bar.setValue(bar.maximum())


def open_path(path):
    if not os.path.exists(path):
        QMessageBox.information(None, "Nothing yet", f"{os.path.basename(path)} does not exist yet "
                                "(nothing has been written to it).")
        return
    os.startfile(path)


def blank_icon(size):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    return QIcon(pix)


class MainWindow(QMainWindow):
    EXT = {"Mesh": "obj", "Texture2D": "png", "Sprite": "png", "TextAsset": "txt"}

    def __init__(self, log_handler=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)
        self.settings = Settings()
        self.env = None
        self.tex_finder = None
        self.current = None  # (type_name, name, ObjectReader)
        self.thread = None
        self.last_dir = self.settings.get("last_dir", os.path.expanduser("~"))
        self.loaded = {}  # norm path -> {"env", "groups", "files", "tex_finder", "thumbs", "stats"}
        self.loading_path = None
        self.favorites = set()   # asset_id()s
        self.items_by_key = {}   # key -> tree item
        self.grid_items = {}     # key -> grid item
        self.stats = {}          # key -> stats dict (per game)
        self.stats_remaining = 0
        self._windows = []       # open UV-layout windows

        # thumbnails / stats
        self.blank = blank_icon(THUMB_SIZE)
        self.blank_big = blank_icon(GRID_THUMB)
        self.thumb_cache = {}  # key -> QIcon (insertion order = age, capped)
        self.thumbs = ThumbnailWorker(GRID_THUMB)
        self.thumbs.ready.connect(self.on_thumbnail)
        self.thumb_timer = QTimer(self, singleShot=True, interval=120)
        self.thumb_timer.timeout.connect(self.queue_visible_thumbs)
        self.stats_worker = StatsWorker()
        self.stats_worker.ready.connect(self.on_stats)
        self.filter_timer = QTimer(self, singleShot=True, interval=250)
        self.filter_timer.timeout.connect(self.apply_filter)
        self.resort_timer = QTimer(self, singleShot=True, interval=1500)
        self.resort_timer.timeout.connect(self.resort)

        # left: buttons, search, type/view, list or grid
        back = QPushButton("\u2190 Projects")
        back.clicked.connect(self.show_home)
        self.notes_btn = QPushButton("Notes")
        self.notes_btn.setToolTip("Your notes for this game")
        self.notes_btn.clicked.connect(self.edit_current_notes)
        top_row = QHBoxLayout()
        top_row.addWidget(back, 1)
        top_row.addWidget(self.notes_btn)

        self.search = QLineEdit(placeholderText="Search...  e.g.  gun tris>1000 size>1mb")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(FILTER_HELP)
        self.search.textChanged.connect(lambda _: self.filter_timer.start())
        self.type_combo = QComboBox()
        for label, value in (("All types", "all"), ("Models (Mesh)", "Mesh"), ("Textures (Texture2D)", "Texture2D"),
                             ("Sprites", "Sprite"), ("Text (TextAsset)", "TextAsset"), ("\u2605 Favorites", "fav")):
            self.type_combo.addItem(label, value)
        self.type_combo.currentIndexChanged.connect(lambda _: self.apply_filter())
        self.list_btn = QToolButton(text="\u2630 List", checkable=True, checked=True)
        self.grid_btn = QToolButton(text="\u25a6 Grid", checkable=True)
        self.grid_btn.setToolTip("Big thumbnails - use the arrow keys to flip through quickly")
        view_group = QButtonGroup(self)
        view_group.setExclusive(True)
        view_group.addButton(self.list_btn)
        view_group.addButton(self.grid_btn)
        self.grid_btn.toggled.connect(self.set_grid_mode)
        type_row = QHBoxLayout()
        type_row.addWidget(self.type_combo, 1)
        type_row.addWidget(self.list_btn)
        type_row.addWidget(self.grid_btn)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Info", "Size"])
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.resizeSection(1, 105)
        header.resizeSection(2, 78)
        # Sorting is done by hand (so live stat updates don't reshuffle rows constantly).
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(0, Qt.AscendingOrder)
        header.sortIndicatorChanged.connect(lambda col, order: self.resort())
        self.tree.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self.on_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda pos: self.on_context_menu(self.tree, pos))
        self.tree.verticalScrollBar().valueChanged.connect(lambda _: self.thumb_timer.start())
        self.tree.itemExpanded.connect(lambda _: self.thumb_timer.start())

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setIconSize(QSize(GRID_THUMB, GRID_THUMB))
        self.grid.setGridSize(QSize(GRID_THUMB + 30, GRID_THUMB + 34))
        self.grid.setTextElideMode(Qt.ElideMiddle)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.currentItemChanged.connect(self.on_grid_current)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self.on_context_menu(self.grid, pos))
        self.grid.verticalScrollBar().valueChanged.connect(lambda _: self.thumb_timer.start())
        self.grid_dirty = True

        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.tree)
        self.list_stack.addWidget(self.grid)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(top_row)
        ll.addWidget(self.search)
        ll.addLayout(type_row)
        ll.addWidget(self.list_stack)

        # right: stacked previews
        self.mesh_view = MeshView()
        self.mesh_view.uv_layout_requested.connect(self.show_uv_layout)
        panel = self.mesh_view.panel
        panel.apply_texture.connect(self.apply_texture)
        panel.open_texture.connect(lambda data: self.goto_asset(thumb_key(data[2])))
        panel.save_texture.connect(self.export_item)
        panel.save_model.connect(self.export_selected_mesh)
        panel.open_blender.connect(self.open_in_blender)
        self.image_view = ImageView()
        self.image_view.save_requested.connect(self.save_shown_image)
        self.image_view.goto_requested.connect(self.goto_asset)
        self.text_view = QPlainTextEdit(readOnly=True)

        self.placeholder = QLabel("Loading...", alignment=Qt.AlignCenter)
        self.load_bar = QProgressBar()
        self.load_bar.setFixedWidth(420)
        self.load_bar.hide()
        self.placeholder_page = QWidget()
        pl = QVBoxLayout(self.placeholder_page)
        pl.addStretch()
        pl.addWidget(self.placeholder)
        pl.addWidget(self.load_bar, 0, Qt.AlignHCenter)
        pl.addStretch()

        self.stack = QStackedWidget()
        for w in (self.placeholder_page, self.mesh_view, self.image_view, self.text_view):
            self.stack.addWidget(w)

        self.viewer = QSplitter()
        self.viewer.addWidget(left)
        self.viewer.addWidget(self.stack)
        self.viewer.setSizes([430, 1070])

        self.store = ProjectStore()
        self.home = HomePage(self.store, self.is_loaded, self.settings)
        self.home.open_requested.connect(self.open_project)
        self.home.unload_requested.connect(self.unload_game)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.home)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)

        self.console = None
        if log_handler is not None:
            self.console = ConsoleDock(log_handler, self)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.console)
            self.resizeDocks([self.console], [170], Qt.Vertical)

        self._build_menu()
        if self.settings.get("view") == "grid":
            self.grid_btn.setChecked(True)
        self.statusBar().showMessage("Ready")
        log.info(APP_SHORT + " %s started (UnityPy %s)", __version__, UnityPy.__version__)

    # ---- projects / games
    def current_project(self):
        return self.store.get(self.loading_path) if self.loading_path else None

    def is_loaded(self, path):
        return norm_path(path) in self.loaded

    def unload_game(self, path):
        entry = self.loaded.pop(norm_path(path), None)
        if entry is None:
            return
        if self.env is entry["env"]:
            self._reset_viewer()
            self.placeholder.setText("This game was unloaded. Go back to Projects to open it again.")
            self.statusBar().showMessage("Unloaded")
        del entry
        import gc
        gc.collect()
        log.info("Unloaded %s from memory", path)
        self.home.refresh()

    def show_home(self):
        self.home.refresh()
        self.pages.setCurrentWidget(self.home)
        self.setWindowTitle(APP_TITLE)

    def open_project(self, path):
        if self.thread is not None and self.thread.isRunning():
            QMessageBox.information(self, "Busy", "Still loading the previous game, try again in a moment.")
            return
        project = self.store.get(path)
        name = project["name"] if project else os.path.basename(path)
        self.setWindowTitle(f"{APP_SHORT} - {name}")
        log.info("Opening game '%s'", name)
        self.load(path)

    def edit_current_notes(self):
        project = self.current_project()
        if project is None:
            QMessageBox.information(self, "Notes", "Notes are saved per game. Open a game from Projects first.")
            return
        if edit_project_notes(self, project):
            self.store.save()
            self._update_notes_button()

    def _update_notes_button(self):
        project = self.current_project()
        self.notes_btn.setEnabled(project is not None)
        self.notes_btn.setText("Notes \U0001F4DD" if project and project.get("notes") else "Notes")
        self.notes_btn.setToolTip(project.get("notes") or "Your notes for this game" if project else
                                  "Notes are saved per game (open a game from Projects)")

    def _build_menu(self):
        def add(menu, text, slot, key=None):
            act = QAction(text, self)
            if key:
                act.setShortcut(key)
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        m = self.menuBar().addMenu("&File")
        add(m, "Projects", self.show_home, "Ctrl+Home")
        m.addSeparator()
        add(m, "Open file...", self.open_file, "Ctrl+O")
        add(m, "Open game folder...", self.open_folder, "Ctrl+Shift+O")
        m.addSeparator()
        add(m, "Export all models...", lambda: self.export_all(("Mesh",)))
        add(m, "Export all textures...", lambda: self.export_all(("Texture2D", "Sprite")))
        add(m, "Export everything shown in the list...", self.export_shown)
        m.addSeparator()
        add(m, "Quit", self.close, "Ctrl+Q")

        a = self.menuBar().addMenu("&Asset")
        add(a, "Save selected...", self.export_selected, "Ctrl+S")
        add(a, "Toggle favorite", self.toggle_favorite_selected, "Ctrl+D")
        add(a, "Open model in Blender", self.open_in_blender, "Ctrl+B")
        add(a, "Show UV layout", self.show_uv_layout, "Ctrl+U")
        a.addSeparator()
        add(a, "Find (search box)", lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+F")
        add(a, "Set Blender location...", self.choose_blender)

        view = self.menuBar().addMenu("&View")
        add(view, "List view", lambda: self.list_btn.setChecked(True), "Ctrl+1")
        add(view, "Grid view", lambda: self.grid_btn.setChecked(True), "Ctrl+2")
        if self.console is not None:
            toggle = self.console.toggleViewAction()
            toggle.setShortcut("Ctrl+`")
            view.addAction(toggle)

        help_menu = self.menuBar().addMenu("&Help")
        online = QAction("Download community compatibility list", self, checkable=True)
        online.setChecked(self.settings.get("online_compat", True))
        online.setToolTip(f"Fetches compat.json from {REPO_URL} at startup")
        online.toggled.connect(self._set_online_compat)
        help_menu.addAction(online)
        help_menu.addAction("Project page on GitHub", lambda: webbrowser.open(REPO_URL))
        help_menu.addSeparator()
        help_menu.addAction("Open log file", lambda: open_path(LOG_FILE))
        help_menu.addAction("Open crash log", lambda: open_path(CRASH_FILE))
        help_menu.addAction("Open app folder", lambda: os.startfile(APP_DIR))

    def _set_online_compat(self, on):
        self.settings["online_compat"] = on
        self.settings.save()
        if on:
            self.home.fetch_compat()

    # ---- progress
    def set_progress(self, done, total):
        for bar in (self.progress, self.load_bar):
            if total <= 0:
                bar.setRange(0, 0)  # busy animation
            else:
                bar.setRange(0, total)
                bar.setValue(done)
            bar.show()

    def hide_progress(self):
        self.progress.hide()
        self.load_bar.hide()

    # ---- loading
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Unity asset file")
        if path:
            self.load(path)

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Open game folder (or any folder with Unity files)")
        if path:
            self.load(path)

    def _reset_viewer(self):
        self.thumbs.clear()
        self.stats_worker.start([])
        self.env = self.tex_finder = self.current = None
        self.thumb_cache, self.items_by_key, self.grid_items, self.stats = {}, {}, {}, {}
        self.mesh_view.poly = self.mesh_view.texture_img = None
        self.mesh_view.plotter.clear()
        self.tree.clear()
        self.grid.clear()
        self.stack.setCurrentWidget(self.placeholder_page)

    def load(self, path):
        if self.thread is not None and self.thread.isRunning():
            return
        self.pages.setCurrentWidget(self.viewer)
        self._reset_viewer()
        self.placeholder.setText("Loading...")
        self.loading_path = path
        self._update_notes_button()
        entry = self.loaded.get(norm_path(path))
        if entry is not None:
            log.info("Already loaded - reusing assets in memory for %s", path)
            self.on_loaded(entry["env"], entry["groups"], entry["files"])
            return
        self.set_progress(0, 0)
        self.thread = QThread()
        self.loader = Loader(path)
        self.loader.moveToThread(self.thread)
        self.thread.started.connect(self.loader.run)
        self.loader.progress.connect(self.statusBar().showMessage)
        self.loader.progress.connect(self.placeholder.setText)
        self.loader.progress_value.connect(self.set_progress)
        self.loader.finished.connect(self.on_loaded)
        self.loader.failed.connect(self.on_load_failed)
        self.loader.finished.connect(self.thread.quit)
        self.loader.failed.connect(self.thread.quit)
        self.thread.start()

    def on_loaded(self, env, groups, file_count):
        self.hide_progress()
        key = norm_path(self.loading_path)
        entry = self.loaded.get(key)
        if entry is None or entry["env"] is not env:
            entry = {"env": env, "groups": groups, "files": file_count,
                     "tex_finder": TextureFinder(env), "thumbs": {}, "stats": {}, "favorites": set()}
            self.loaded[key] = entry
        self.env = env
        self.tex_finder = entry["tex_finder"]
        self.thumb_cache = entry["thumbs"]
        self.stats = entry["stats"]
        project = self.current_project()
        self.favorites = set(project.get("favorites", [])) if project else entry["favorites"]
        text_icon = self.style().standardIcon(QStyle.SP_FileIcon)

        tops, total, jobs = [], 0, []
        for type_name, items in groups.items():
            if not items:
                continue
            top = SortItem([type_name, "", ""])
            top.setData(0, TYPE_ROLE, type_name)
            top.setData(0, SORT_ROLE, type_name.lower())
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            children = []
            for name, obj in items:
                key_ = thumb_key(obj)
                child = SortItem([name, "", fmt_size(obj.byte_size)])
                child.setData(0, Qt.UserRole, (type_name, name, obj))
                child.setData(0, SORT_ROLE, name.lower())
                child.setData(2, SORT_ROLE, obj.byte_size)
                if type_name in THUMB_TYPES:
                    child.setIcon(0, self.thumb_cache.get(key_, self.blank))
                else:
                    child.setIcon(0, text_icon)
                if asset_id(obj) in self.favorites:
                    self._mark_favorite(child, True)
                stats = self.stats.get(key_)
                if stats is not None:
                    self._apply_stats(child, stats)
                else:
                    jobs.append((key_, type_name, obj))
                self.items_by_key[key_] = child
                children.append(child)
            top.addChildren(children)
            tops.append(top)
            total += len(items)
        self.tree.addTopLevelItems(tops)
        self.resort()

        # Measure everything in the background: models first, then textures, sprites, text.
        order = {t: i for i, t in enumerate(SHOWN_TYPES)}
        jobs.sort(key=lambda j: order.get(j[1], 9))
        self.stats_remaining = len(jobs)
        self.stats_worker.start(jobs)

        self.store.set_counts(self.loading_path, files=file_count, assets=total)
        self.placeholder.setText("Pick something from the list on the left.")
        self.statusBar().showMessage(f"Loaded {total:,} assets from {file_count} file(s)")
        self.grid_dirty = True
        self.apply_filter()

    def on_load_failed(self, tb):
        self.hide_progress()
        self.statusBar().showMessage("Load failed - see the console")
        self.placeholder.setText("Load failed.")
        QMessageBox.critical(self, "Load failed", tb[-3000:])

    # ---- stats, sorting, filtering
    def _apply_stats(self, item, stats):
        item.setText(1, stats.get("info", ""))
        item.setData(1, SORT_ROLE, stats.get("sort", -1))
        item.setText(2, fmt_size(stats.get("size")))
        item.setData(2, SORT_ROLE, stats.get("size", -1))

    def on_stats(self, batch, remaining):
        for key, stats in batch:
            self.stats[key] = stats
            item = self.items_by_key.get(key)
            if item is not None:
                self._apply_stats(item, stats)
        self.stats_remaining = remaining
        if remaining:
            self.statusBar().showMessage(f"Measuring assets for sort/search... {remaining:,} left", 2000)
        else:
            log.info("Finished measuring all assets (tris, sizes)")
        if self.tree.header().sortIndicatorSection() in (1, 2) and not self.resort_timer.isActive():
            self.resort_timer.start()
        if AssetFilter(self.search.text()).conditions and not self.filter_timer.isActive():
            self.filter_timer.start()

    def resort(self):
        header = self.tree.header()
        self.tree.sortItems(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self.grid_dirty = True
        if self.list_stack.currentWidget() is self.grid:
            self.rebuild_grid()
        self.thumb_timer.start()

    def apply_filter(self):
        filt = AssetFilter(self.search.text())
        want = self.type_combo.currentData()
        self.tree.setUpdatesEnabled(False)
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                type_name = top.data(0, TYPE_ROLE)
                group_ok = want in ("all", "fav") or want == type_name
                shown = 0
                if group_ok:
                    for j in range(top.childCount()):
                        child = top.child(j)
                        _t, name, obj = child.data(0, Qt.UserRole)
                        ok = filt.matches(name, type_name, self.stats.get(thumb_key(obj)))
                        if ok and want == "fav":
                            ok = asset_id(obj) in self.favorites
                        child.setHidden(not ok)
                        shown += ok
                count = top.childCount()
                top.setText(0, f"{type_name} ({shown:,})" if shown == count else f"{type_name} ({shown:,} of {count:,})")
                searching = want == "fav" or bool(self.search.text().strip())
                top.setHidden(not group_ok or (searching and not shown))
                if want not in ("all", "fav") and group_ok:
                    top.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)
        if filt.conditions and self.stats_remaining:
            self.statusBar().showMessage(
                f"Still measuring {self.stats_remaining:,} assets - more results will appear", 4000)
        self.grid_dirty = True
        if self.list_stack.currentWidget() is self.grid:
            self.rebuild_grid()
        self.thumb_timer.start()

    def visible_assets(self):
        """(tree item, data) for every row that passes the current filter, in list order."""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.isHidden():
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if not child.isHidden():
                    out.append((child, child.data(0, Qt.UserRole)))
        return out

    # ---- grid view
    def set_grid_mode(self, grid):
        self.list_stack.setCurrentWidget(self.grid if grid else self.tree)
        self.settings["view"] = "grid" if grid else "list"
        self.settings.save()
        if grid:
            self.rebuild_grid()
        self.thumb_timer.start()

    def rebuild_grid(self):
        if not self.grid_dirty:
            return
        self.grid_dirty = False
        rows = self.visible_assets()
        with QSignalBlocker(self.grid):
            self.grid.clear()
            self.grid_items = {}
            for _item, data in rows[:GRID_LIMIT]:
                type_name, name, obj = data
                key = thumb_key(obj)
                fav = asset_id(obj) in self.favorites
                it = QListWidgetItem(self.thumb_cache.get(key, self.blank_big), ("\u2605 " if fav else "") + name)
                it.setData(Qt.UserRole, data)
                stats = self.stats.get(key) or {}
                it.setToolTip(f"{name}\n{type_name}  {stats.get('info', '')}  {fmt_size(stats.get('size'))}".strip())
                self.grid.addItem(it)
                self.grid_items[key] = it
        if len(rows) > GRID_LIMIT:
            self.statusBar().showMessage(f"Grid shows the first {GRID_LIMIT:,} of {len(rows):,} - "
                                         "use search or the type filter to narrow it down", 6000)
        if self.current:
            current = self.grid_items.get(thumb_key(self.current[2]))
            if current is not None:
                with QSignalBlocker(self.grid):
                    self.grid.setCurrentItem(current)
        self.thumb_timer.start()

    def on_grid_current(self, item, _previous):
        data = item.data(Qt.UserRole) if item else None
        if not data:
            return
        tree_item = self.items_by_key.get(thumb_key(data[2]))
        if tree_item is not None:
            with QSignalBlocker(self.tree):
                self.tree.setCurrentItem(tree_item)
        self.show_asset(data)

    # ---- thumbnails
    def queue_visible_thumbs(self):
        """Ask for thumbnails of what's on screen (plus a little beyond)."""
        if self.env is None:
            return
        jobs = []
        if self.list_stack.currentWidget() is self.grid:
            vp = self.grid.viewport()
            first = max(self.grid.indexAt(QPoint(8, 8)).row(), 0)
            last = self.grid.indexAt(QPoint(vp.width() - 8, vp.height() - 8)).row()
            if last < 0:
                last = first + 150
            for row in range(first, min(self.grid.count(), last + 40)):
                data = self.grid.item(row).data(Qt.UserRole)
                if data and data[0] in THUMB_TYPES and thumb_key(data[2]) not in self.thumb_cache:
                    jobs.append((thumb_key(data[2]), data[0], data[2]))
        else:
            height = self.tree.viewport().height()
            item = self.tree.itemAt(QPoint(4, 4))
            extra = 0
            while item is not None and extra < 20:
                if self.tree.visualItemRect(item).top() > height:
                    extra += 1
                data = item.data(0, Qt.UserRole)
                if data and data[0] in THUMB_TYPES and thumb_key(data[2]) not in self.thumb_cache:
                    jobs.append((thumb_key(data[2]), data[0], data[2]))
                item = self.tree.itemBelow(item)
        self.thumbs.request_visible(jobs)

    def on_thumbnail(self, key, qimg):
        icon = QIcon(QPixmap.fromImage(qimg)) if not qimg.isNull() else self.blank
        self.thumb_cache[key] = icon
        while len(self.thumb_cache) > THUMB_CACHE_MAX:  # forget the oldest to cap memory
            old = next(iter(self.thumb_cache))
            del self.thumb_cache[old]
            self._set_icon(old, None)
        self._set_icon(key, icon)

    def _set_icon(self, key, icon):
        item = self.items_by_key.get(key)
        if item is not None:
            item.setIcon(0, icon or self.blank)
        grid_item = self.grid_items.get(key)
        if grid_item is not None:
            grid_item.setIcon(icon or self.blank_big)
        if icon is not None:
            self.mesh_view.panel.set_icon(key, icon)
            self.image_view.set_link_icon(key, icon)

    def _request_icons(self, keys_types_objs, setter):
        """Use cached icons now, request the rest at high priority."""
        missing = []
        for key, type_name, obj in keys_types_objs:
            if key in self.thumb_cache:
                setter(key, self.thumb_cache[key])
            else:
                missing.append((key, type_name, obj))
        self.thumbs.request_priority(missing)

    # ---- preview
    def on_select(self):
        items = self.tree.selectedItems()
        data = items[0].data(0, Qt.UserRole) if items else None
        if not data:
            return
        grid_item = self.grid_items.get(thumb_key(data[2]))
        if grid_item is not None:
            with QSignalBlocker(self.grid):
                self.grid.setCurrentItem(grid_item)
        self.show_asset(data)

    def show_asset(self, data):
        self.current = data
        type_name, name, obj = data
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with UNITY_LOCK:
                asset = obj.read()
                if type_name == "Mesh":
                    self.show_mesh(name, obj, asset)
                elif type_name in ("Texture2D", "Sprite"):
                    self.show_texture(name, asset_image(asset), obj, asset)
                elif type_name == "TextAsset":
                    script = asset.m_Script
                    if isinstance(script, bytes):
                        script = script.decode("utf-8", "replace")
                    self.text_view.setPlainText(script[:MAX_TEXT_CHARS])
                    self.stack.setCurrentWidget(self.text_view)
        except Exception as e:
            log.exception("Could not preview %s '%s'", type_name, name)
            self.stack.setCurrentWidget(self.text_view)
            self.text_view.setPlainText(f"Could not preview {name}:\n\n{e}\n\n{traceback.format_exc()}")
        finally:
            QApplication.restoreOverrideCursor()

    def show_mesh(self, name, obj, mesh):
        poly = unity_mesh_to_polydata(mesh)
        users, materials = [], []
        try:
            users, materials = self.tex_finder.info(obj)
        except Exception:
            pass
        tex = None
        main_reader = TextureFinder.main_texture(materials)
        if main_reader is not None:
            try:
                tex = asset_image(main_reader.read())
            except Exception:
                pass
        self.stack.setCurrentWidget(self.mesh_view)
        self.mesh_view.show_mesh(poly, tex)
        n_tex = sum(len(m["textures"]) for m in materials)
        log.info("Model '%s': %s verts, %s tris, %d material(s), %d texture(s)%s", name,
                 f"{poly.n_points:,}", f"{poly.n_cells:,}", len(materials), n_tex,
                 "" if tex is not None else " - no texture found")

        submeshes = len(mesh.m_SubMeshes or [])
        channels = self.mesh_view.uv_channels()
        rows = [
            f"<b>Vertices:</b> {poly.n_points:,}",
            f"<b>Triangles:</b> {poly.n_cells:,}",
            f"<b>Submeshes:</b> {submeshes}",
            f"<b>UV sets:</b> {', '.join(channels) if channels else 'none'}",
            f"<b>File:</b> {obj.assets_file.name}",
        ]
        if getattr(obj, "container", None):
            rows.append(f"<b>Path:</b> {obj.container}")
        if users:
            shown = ", ".join(sorted(set(users))[:8])
            more = f" (+{len(set(users)) - 8} more)" if len(set(users)) > 8 else ""
            rows.append(f"<b>Used by:</b> {shown}{more}")
        if asset_id(obj) in self.favorites:
            rows.insert(0, "<span style='color:#f4c542'>\u2605 Favorite</span>")
        jobs = self.mesh_view.panel.set_info(name, "<br>".join(rows), materials, self.blank_big)
        self._request_icons(jobs, self.mesh_view.panel.set_icon)

    def show_texture(self, name, img, obj=None, asset=None):
        log.info("Texture '%s': %dx%d %s", name, img.width, img.height, img.mode)
        self.image_view.show_image(img, ("\u2605 " if obj is not None and asset_id(obj) in self.favorites else "") + name)
        self.stack.setCurrentWidget(self.image_view)
        self.statusBar().showMessage(f"{name}  {img.width}x{img.height}")
        if obj is None or self.tex_finder is None:
            self.image_view.set_links("", [], "")
            return
        entries, jobs = [], []
        if obj.type.name == "Sprite":
            # A sprite is a piece of a texture (sprite sheet): link to it.
            try:
                tex_ptr = asset.m_RD.texture
                reader = tex_ptr.deref()
                key = thumb_key(reader)
                entries.append((key, f"{reader.peek_name() or 'texture'}\n(sprite sheet)", self.blank_big))
                jobs.append((key, "Texture2D", reader))
            except Exception:
                pass
            title, empty = "Sprite sheet", "Couldn't find the texture this sprite comes from."
        else:
            try:
                keys = self.tex_finder.texture_users(obj)
            except Exception:
                log.exception("Finding models that use %s failed", name)
                keys = set()
            for key in keys:
                item = self.items_by_key.get(key)
                if item is None:
                    continue
                _t, mesh_name, mesh_obj = item.data(0, Qt.UserRole)
                entries.append((key, mesh_name, self.blank_big))
                jobs.append((key, "Mesh", mesh_obj))
            entries.sort(key=lambda e: e[1].lower())
            title = f"Used by {len(entries)} model(s)"
            empty = "No models found that use this texture (UI images and sprites often aren't on models)."
        self.image_view.set_links(title, entries, empty)
        self._request_icons(jobs, self.image_view.set_link_icon)

    def goto_asset(self, key):
        """Select an asset in the list/grid (clearing the search if it's filtered out)."""
        item = self.items_by_key.get(key)
        if item is None:
            self.statusBar().showMessage("That asset isn't in this game's list (it may be in a file that wasn't loaded)", 5000)
            return
        if item.isHidden() or (item.parent() is not None and item.parent().isHidden()):
            with QSignalBlocker(self.search), QSignalBlocker(self.type_combo):
                self.search.clear()
                self.type_combo.setCurrentIndex(0)
            self.apply_filter()
        if item.parent() is not None:
            item.parent().setExpanded(True)
        self.tree.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self.tree.setCurrentItem(item)  # triggers on_select -> preview (+ grid sync)
        grid_item = self.grid_items.get(key)
        if grid_item is not None:
            self.grid.scrollToItem(grid_item, QAbstractItemView.PositionAtCenter)

    def show_uv_layout(self):
        poly = self.mesh_view.poly
        channel = self.mesh_view.uv_combo.currentText()
        if poly is None or channel not in poly.point_data:
            QMessageBox.information(self, "UV layout", "Open a model that has UVs first.")
            return
        name = self.current[1] if self.current else "model"
        img = render_uv_layout(poly, channel, self.mesh_view.texture_img)
        window = ImageWindow(img, f"UV layout - {name} ({channel})", self)
        window.show()
        self._windows = [w for w in self._windows if w.isVisible()] + [window]

    # ---- favorites
    def _mark_favorite(self, item, fav):
        name = item.data(0, Qt.UserRole)[1]
        item.setText(0, ("\u2605 " if fav else "") + name)
        item.setForeground(0, QBrush(QColor("#f4c542")) if fav else QBrush())

    def selected_assets(self):
        widget = self.list_stack.currentWidget()
        if widget is self.grid:
            datas = [i.data(Qt.UserRole) for i in self.grid.selectedItems()]
        else:
            datas = [i.data(0, Qt.UserRole) for i in self.tree.selectedItems()]
        datas = [d for d in datas if d]
        return datas or ([self.current] if self.current else [])

    def toggle_favorite_selected(self):
        self.toggle_favorites(self.selected_assets())

    def toggle_favorites(self, datas):
        if not datas:
            return
        make_fav = any(asset_id(d[2]) not in self.favorites for d in datas)
        for _t, name, obj in datas:
            aid = asset_id(obj)
            (self.favorites.add if make_fav else self.favorites.discard)(aid)
            key = thumb_key(obj)
            item = self.items_by_key.get(key)
            if item is not None:
                self._mark_favorite(item, make_fav)
            grid_item = self.grid_items.get(key)
            if grid_item is not None:
                grid_item.setText(("\u2605 " if make_fav else "") + name)
        project = self.current_project()
        if project is not None:
            project["favorites"] = sorted(self.favorites)
            self.store.save()
        log.info("%s %d asset(s) %s favorites", "Added" if make_fav else "Removed", len(datas),
                 "to" if make_fav else "from")
        if self.type_combo.currentData() == "fav":
            self.apply_filter()

    # ---- context menu
    def on_context_menu(self, widget, pos):
        if widget is self.grid:
            item = self.grid.itemAt(pos)
            data = item.data(Qt.UserRole) if item else None
        else:
            item = self.tree.itemAt(pos)
            data = item.data(0, Qt.UserRole) if item else None
        if not data:
            return
        selected = self.selected_assets()
        if data not in selected:
            selected = [data]
        menu = QMenu(self)
        if data[0] in ("Texture2D", "Sprite") and self.mesh_view.poly is not None:
            menu.addAction("Apply as texture to current model", lambda: self.apply_texture(data))
        if data[0] == "Mesh":
            menu.addAction("Open in Blender", lambda: self.open_in_blender(data))
        fav = all(asset_id(d[2]) in self.favorites for d in selected)
        menu.addAction(("Remove from favorites" if fav else "Add to favorites") + "   Ctrl+D",
                       lambda: self.toggle_favorites(selected))
        menu.addSeparator()
        menu.addAction("Save...", lambda: self.export_item(data))
        if len(selected) > 1:
            menu.addAction(f"Export {len(selected)} selected...", lambda: self.export_many(selected))
        menu.exec(widget.viewport().mapToGlobal(pos))

    def apply_texture(self, data):
        try:
            with UNITY_LOCK:
                img = asset_image(data[2].read())
            self.mesh_view.set_texture(img)
            self.stack.setCurrentWidget(self.mesh_view)
        except Exception as e:
            QMessageBox.warning(self, "Texture", str(e))

    # ---- Blender
    def choose_blender(self):
        path, _ = QFileDialog.getOpenFileName(self, "Where is blender.exe?", self.settings.get("blender_path", ""),
                                              "Blender (blender.exe);;Programs (*.exe)")
        if path:
            self.settings["blender_path"] = path
            self.settings.save()
            log.info("Blender location set to %s", path)
        return path or None

    def open_in_blender(self, data=None):
        data = data or self.current
        if not data or data[0] != "Mesh":
            QMessageBox.information(self, "Open in Blender", "Select a model first.")
            return
        blender = find_blender(self.settings.get("blender_path"))
        if blender is None:
            if QMessageBox.question(self, "Blender not found",
                                    "Couldn't find Blender on this PC.\n\nPoint to blender.exe yourself?") != QMessageBox.Yes:
                return
            blender = self.choose_blender()
            if not blender:
                return
        _t, name, obj = data
        out_dir = os.path.join(tempfile.gettempdir(), APP_SHORT)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{safe_filename(name)}.glb")
        try:
            with UNITY_LOCK:
                mesh = obj.read()
                materials = self.tex_finder.info(obj)[1] if self.tex_finder else []
                write_glb(mesh, materials, path)
            subprocess.Popen([blender, "--python-expr", BLENDER_IMPORT.format(path=path)])
            log.info("Opened '%s' in Blender (%s)", name, blender)
            self.statusBar().showMessage(f"Opening {name} in Blender...", 5000)
        except Exception as e:
            log.exception("Opening '%s' in Blender failed", name)
            QMessageBox.warning(self, "Open in Blender", str(e))

    # ---- export
    def ask_save_path(self, default_name, filter_text):
        path, chosen = QFileDialog.getSaveFileName(
            self, "Save", os.path.join(self.last_dir, default_name), filter_text)
        if path:
            self.last_dir = os.path.dirname(path)
            self.settings["last_dir"] = self.last_dir
            self.settings.save()
        return path, chosen

    def export_selected(self):
        datas = self.selected_assets()
        if len(datas) > 1:
            self.export_many(datas)
        elif datas:
            self.export_item(datas[0])

    def export_selected_mesh(self):
        if self.current and self.current[0] == "Mesh":
            self.export_item(self.current)

    def save_shown_image(self):
        img = self.image_view.image
        if img is None:
            return
        name = self.current[1] if self.current else "texture"
        path, _ = self.ask_save_path(f"{safe_filename(name)}.png", "PNG image (*.png)")
        if path:
            try:
                img.save(path)
                self.statusBar().showMessage(f"Saved {path}")
                log.info("Saved %s", path)
            except Exception as e:
                log.exception("Saving %s failed", path)
                QMessageBox.warning(self, "Save failed", str(e))

    def export_item(self, data):
        type_name, name, obj = data
        filters = {
            "Mesh": "Wavefront OBJ + textures (*.obj);;glTF binary, textures inside (*.glb)",
            "Texture2D": "PNG image (*.png)",
            "Sprite": "PNG image (*.png)",
            "TextAsset": "All files (*)",
        }[type_name]
        ext = self.settings.get("model_format", "obj") if type_name == "Mesh" else self.EXT[type_name]
        path, chosen = self.ask_save_path(f"{safe_filename(name)}.{ext}", filters)
        if not path:
            return
        if type_name == "Mesh" and not path.lower().endswith((".obj", ".glb")):
            path += ".glb" if "glb" in chosen else ".obj"
        try:
            written = self.write_asset(type_name, obj, path)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(w) for w in written))
            for w in written:
                log.info("Saved %s", w)
        except Exception as e:
            log.exception("Saving %s failed", name)
            QMessageBox.warning(self, "Save failed", str(e))

    def write_asset(self, type_name, obj, path):
        """Save one asset to `path` (.obj/.glb for models); returns the list of files written."""
        with UNITY_LOCK:
            asset = obj.read()
            if type_name == "Mesh":
                if path.lower().endswith(".glb"):
                    materials = []
                    if self.tex_finder is not None:
                        try:
                            materials = self.tex_finder.info(obj)[1]
                        except Exception:
                            pass
                    return write_glb(asset, materials, path)
                return self.write_mesh(obj, asset, path)
            if type_name in ("Texture2D", "Sprite"):
                asset_image(asset).save(path)
                return [path]
            script = asset.m_Script
            mode, enc = ("wb", None) if isinstance(script, bytes) else ("w", "utf-8")
            with open(path, mode, encoding=enc) as f:
                f.write(script)
            return [path]

    def write_mesh(self, obj, mesh, path):
        """Write <name>.obj + <name>.mtl + textures so it opens textured in Blender."""
        folder = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        materials = []
        if self.tex_finder is not None:
            try:
                materials = self.tex_finder.info(obj)[1]
            except Exception:
                pass
        written, mat_names, mtl = [path], [], []
        for i, mat in enumerate(materials):
            mat_name = f"{safe_filename(mat['name'] or 'material')}_{i}"
            mat_names.append(mat_name)
            mtl += [f"newmtl {mat_name}", "Kd 1 1 1"]
            have_diffuse = False
            for prop, tex_name, reader in mat["textures"]:
                png = f"{stem}_{safe_filename(tex_name)}.png"
                try:
                    png_path = os.path.join(folder, png)
                    if png_path not in written:
                        asset_image(reader.read()).save(png_path)
                        written.append(png_path)
                except Exception:
                    continue
                if not have_diffuse and prop not in NORMAL_PROPS:
                    mtl.append(f"map_Kd {png}")
                    have_diffuse = True
                elif prop in NORMAL_PROPS:
                    mtl.append(f"map_Bump {png}")
            mtl.append("")
        with surface_submeshes(mesh) as skipped:
            text = export_mesh_obj(mesh, mat_names or None)
        if skipped:
            log.info("Skipped %d line/point part(s) of %s (OBJ export is surfaces only)", skipped, mesh.m_Name)
        if not text:
            raise ValueError("Mesh has no vertex data to export.")
        if mat_names:
            text = "\n".join(f"mtllib {stem}.mtl" if line.startswith("mtllib ") else line
                             for line in text.split("\n"))
            mtl_path = os.path.join(folder, stem + ".mtl")
            with open(mtl_path, "w", encoding="utf-8") as f:
                f.write("\n".join(mtl))
            written.append(mtl_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return written

    def export_many(self, items):
        self._export_with_dialog(items)

    def export_all(self, type_names):
        if self.env is None:
            return
        items = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                data = top.child(j).data(0, Qt.UserRole)
                if data and data[0] in type_names:
                    items.append(data)
        self._export_with_dialog(items)

    def export_shown(self):
        self._export_with_dialog([data for _item, data in self.visible_assets()])

    def _export_with_dialog(self, items):
        if not items:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        dlg = ExportDialog(len(items), any(d[0] == "Mesh" for d in items), self.settings, self)
        if dlg.exec():
            folder, model_format, keep = dlg.values()
            self._export_to_folder(items, folder, model_format, keep)

    def _export_to_folder(self, items, folder, model_format="obj", keep_structure=False):
        log.info("Exporting %d item(s) to %s (models as %s%s)", len(items), folder, model_format.upper(),
                 ", keeping the game's folders" if keep_structure else "")
        ok = fail = 0
        used = set()
        self.set_progress(0, len(items))
        for n, (type_name, name, obj) in enumerate(items, 1):
            ext = model_format if type_name == "Mesh" else self.EXT[type_name]
            sub = export_subfolder(type_name, obj, name) if keep_structure else ""
            target = os.path.normpath(os.path.join(folder, sub))
            base = safe_filename(name)
            fname, i = base, 1
            while (target.lower(), fname.lower()) in used:
                i += 1
                fname = f"{base}_{i}"
            used.add((target.lower(), fname.lower()))
            try:
                os.makedirs(target, exist_ok=True)
                self.write_asset(type_name, obj, os.path.join(target, f"{fname}.{ext}"))
                ok += 1
            except Exception as e:
                fail += 1
                log.warning("Could not export %s: %s", name, e)
            if n % 10 == 0 or n == len(items):
                self.set_progress(n, len(items))
                self.statusBar().showMessage(f"Exporting ... {ok} done, {fail} failed")
                QApplication.processEvents()
        self.hide_progress()
        self.statusBar().showMessage(f"Exported {ok} item(s) ({fail} failed) to {folder}")
        log.info("Exported %d item(s), %d failed", ok, fail)

    def closeEvent(self, event):
        self.thumbs.clear()
        self.stats_worker.start([])
        self.settings["last_dir"] = self.last_dir
        self.settings.save()
        self.mesh_view.plotter.close()
        super().closeEvent(event)


def square_icon_image(img):
    """Pad an image to a square (transparent borders) so icons never look stretched."""
    img = img.convert("RGBA")
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return square


def load_app_icon():
    if not os.path.isfile(ICON_FILE):
        return QIcon()
    try:
        square = square_icon_image(Image.open(ICON_FILE))
    except Exception:
        log.exception("Could not read %s", ICON_FILE)
        return QIcon()
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(pil_to_pixmap(square.resize((size, size), Image.LANCZOS)))
    return icon


def self_test(game_path=None):
    """Check the libraries work (used by build.py on the packaged exe). Returns an exit code."""
    failures = []

    def check(name, fn):
        try:
            detail = fn()
            log.info("self-test OK    %s%s", name, f" ({detail})" if detail else "")
        except Exception as e:
            failures.append(name)
            log.error("self-test FAIL  %s: %s: %s", name, type(e).__name__, e)

    def imports():
        import importlib
        mods = ("texture2ddecoder", "etcpak", "astc_encoder", "lz4.block", "brotli")
        for mod in mods:
            importlib.import_module(mod)
        return ", ".join(mods)

    def render():
        plotter = pv.Plotter(off_screen=True, window_size=(64, 64))
        plotter.add_mesh(pv.Sphere())
        shape = plotter.screenshot(return_img=True).shape
        plotter.close()
        return f"offscreen image {shape}"

    def window():
        MainWindow(None).close()

    def game():
        result = {}
        loader = Loader(game_path)
        loader.finished.connect(lambda env, groups, files: result.update(groups=groups, files=files))
        loader.run()
        if "groups" not in result:
            raise RuntimeError("loading failed (see log above)")
        counts = {}
        for type_name, decode in (("Mesh", lambda o: unity_mesh_to_polydata(o.read())),
                                  ("Texture2D", lambda o: asset_image(o.read()).load())):
            ok = 0
            items = result["groups"][type_name][:25]
            for name, obj in items:
                try:
                    decode(obj)
                    ok += 1
                except Exception as e:
                    log.warning("self-test: could not decode %s '%s': %s", type_name, name, e)
            if items and not ok:
                raise RuntimeError(f"none of the first {len(items)} {type_name} assets decoded")
            counts[type_name] = f"{ok}/{len(items)}"
        return f"{result['files']} files, decoded meshes {counts.get('Mesh')}, textures {counts.get('Texture2D')}"

    check("imports", imports)
    check("3D rendering", render)
    check("main window", window)
    if game_path:
        check(f"load {game_path}", game)
    log.info("self-test %s", "PASSED" if not failures else f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


def main():
    handler = setup_logging()
    install_crash_handlers()
    if "--self-test" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--self-test"]
        QApplication(sys.argv)
        sys.exit(self_test(args[0] if args else None))
    if sys.platform == "win32":
        # Own taskbar entry/icon instead of grouping under python.exe.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_SHORT)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(load_app_icon())
    win = MainWindow(handler)
    win.show()
    if len(sys.argv) > 1:
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
