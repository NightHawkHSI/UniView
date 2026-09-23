"""UniView - Unity Asset Viewer. Browse meshes, textures and text assets from Unity games.

Starts on a projects page with a box per saved game (add one by folder or let it
find Unity games in your Steam libraries). Opening a game scans its whole folder
for Unity files; pick an item in the tree and it previews on the right. Meshes render in 3D (with their texture when it
can be found through a MeshRenderer), textures show as images.
"""

__version__ = "1.0.0"
APP_SHORT = "UniView"
APP_TITLE = "UniView - Unity Asset Viewer"

import faulthandler
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
import pyvista as pv
import UnityPy
from PIL import Image
from PySide6.QtCore import (
    QEvent, QFileInfo, QObject, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QIcon, QImage, QPainter,
    QPalette, QPen, QPixmap, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog,
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
THUMB_SIZE = 40
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
    finished = Signal(object, object, int)  # env, {type_name: [(name, ObjectReader)]}, file count
    failed = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            started = time.time()
            self.progress.emit(f"Scanning {self.path} for Unity files ...")
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
                log.info("Loading %d/%d  %s  (%.1f MB)", n, len(files), rel, os.path.getsize(path) / 1e6)
                try:
                    env.load_files([path])
                except Exception as e:
                    failed += 1  # one broken/encrypted file shouldn't stop the rest
                    log.warning("Could not load %s: %s: %s", rel, type(e).__name__, e)
            log.info("Indexing objects...")
            groups = {t: [] for t in SHOWN_TYPES}
            for i, obj in enumerate(env.objects):
                type_name = obj.type.name
                if type_name not in groups:
                    continue
                try:
                    name = obj.peek_name() or f"<unnamed {obj.path_id}>"
                except Exception:
                    name = f"<{type_name} {obj.path_id}>"
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
            return reader.read().image


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
    if handler.m_UV0 and len(handler.m_UV0) == len(points):
        poly.active_texture_coordinates = np.asarray(handler.m_UV0, dtype=np.float32)[:, :2]
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


def make_thumbnail(type_name, obj, size):
    with UNITY_LOCK:
        asset = obj.read()
        if type_name == "Mesh":
            _, points, tris = mesh_arrays(asset)
        else:
            img = asset.image
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
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip() or "unnamed"


# --------------------------------------------------------------------------- UI

def wheel_steps(event):
    """Scroll amount in 'notches' (mouse wheel = 1 per notch, touchpads give fractions)."""
    delta = event.angleDelta().y() or event.pixelDelta().y() * 4
    return delta / 120


class MeshInfoPanel(QWidget):
    """Right-hand panel for a model: stats, source file, materials and their textures."""

    apply_texture = Signal(object)  # ("Texture2D", name, ObjectReader)
    open_texture = Signal(object)
    save_texture = Signal(object)
    save_model = Signal()

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
        hint = QLabel("Click a texture to put it on the model, double-click to view it.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")

        save_tex = QPushButton("Save selected texture...")
        save_tex.clicked.connect(self._save_selected)
        save_model = QPushButton("Save model + textures...")
        save_model.setToolTip("Writes an .obj, .mtl and the .png textures (opens textured in Blender).")
        save_model.clicked.connect(self.save_model)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.addWidget(self.title)
        lay.addWidget(self.details)
        lay.addWidget(QLabel("<b>Materials &amp; textures</b>"))
        lay.addWidget(self.textures, 1)
        lay.addWidget(hint)
        lay.addWidget(save_tex)
        lay.addWidget(save_model)
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
        menu.addAction("View texture", lambda: self.open_texture.emit(data))
        menu.addAction("Save texture (PNG)...", lambda: self.save_texture.emit(data))
        menu.exec(self.textures.viewport().mapToGlobal(pos))


class MeshView(QWidget):
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

        self.info = QLabel()
        bar = QHBoxLayout()
        for w in (self.wire, self.edges, self.use_tex, self.flip_v, self.tex_alpha, self.use_colors,
                  zoom_in, zoom_out, reset):
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

    def show_mesh(self, poly, texture_img):
        self.poly = poly
        self.texture_img = texture_img
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
    """Texture viewer: wheel to zoom, drag to pan, save as PNG."""

    save_requested = Signal()

    def __init__(self, parent=None):
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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)

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
        return sorted(self.projects, key=lambda p: (-p.get("last_opened", 0), p["name"].lower()))


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
    """Draws a game box: icon, name, file/asset counts. Green = loaded, red = not loaded."""

    STATE_ROLE = Qt.UserRole + 1
    SUB_ROLE = Qt.UserRole + 2
    STYLES = {  # state: (border, fill)
        "loaded": ("#43a047", QColor(67, 160, 71, 60)),
        "unloaded": ("#e53935", QColor(229, 57, 53, 40)),
        "missing": ("#888888", QColor(128, 128, 128, 35)),
        "action": ("#666666", QColor(0, 0, 0, 0)),
    }
    SIZE = QSize(176, 200)
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
    ADD, FIND = "__add__", "__find__"

    def __init__(self, store, is_loaded, parent=None):
        super().__init__(parent)
        self.store = store
        self.is_loaded = is_loaded
        self.icons = QFileIconProvider()
        self._counting = set()
        self._counted.connect(self._on_counted)

        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        hint = QLabel("Pick a game to browse its models and textures.  "
                      "<span style='color:#43a047'>Green</span> = already loaded (opens instantly), "
                      "<span style='color:#e53935'>red</span> = not loaded yet.")
        hint.setStyleSheet("color: gray;")

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setGridSize(QSize(188, 212))
        self.grid.setUniformItemSizes(True)
        self.grid.setFocusPolicy(Qt.NoFocus)
        self.grid.setMouseTracking(True)
        self.grid.setItemDelegate(CardDelegate(self.grid))
        self.grid.setStyleSheet("QListWidget { border: none; background: transparent; }")
        self.grid.itemClicked.connect(self.on_activate)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self.on_context_menu)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addWidget(self.grid, 1)
        self.refresh()

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
            item.setToolTip(path)
            if not os.path.isdir(path):
                state, sub = "missing", "Folder missing"
            else:
                state = "loaded" if self.is_loaded(path) else "unloaded"
                files = project.get("file_count")
                if files is None:
                    self._count_files(path)
                parts = [f"{files:,} files" if files is not None else "counting files..."]
                if project.get("asset_count") is not None:
                    parts.append(f"{project['asset_count']:,} assets")
                sub = " · ".join(parts) + "\n" + ("Loaded" if state == "loaded" else "Not loaded")
            item.setData(CardDelegate.STATE_ROLE, state)
            item.setData(CardDelegate.SUB_ROLE, sub)
            self.grid.addItem(item)
        self.grid.addItem(self._action_item("＋ Add game", QStyle.SP_FileDialogNewFolder, self.ADD))
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
        if not path:
            return
        path = os.path.normpath(path)
        if self.store.has(path):
            QMessageBox.information(self, "Add game", "That game is already added.")
            return
        if not unity_data_dir(path) and not find_unity_files(path):
            QMessageBox.warning(self, "Add game", "No Unity asset files found in that folder.")
            return
        name, ok = QInputDialog.getText(self, "Add game", "Name:", text=os.path.basename(path))
        if ok and name.strip():
            self.store.add(name.strip(), path)
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
        menu.addAction("Rename...", lambda: self.rename(project))
        menu.addAction("Recount files", lambda: self._recount(project))
        menu.addAction("Show in Explorer", lambda: os.startfile(project["path"]))
        menu.addSeparator()
        menu.addAction("Remove from list", lambda: self.remove(project))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

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
    def __init__(self, log_handler=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)
        self.env = None
        self.tex_finder = None
        self.current = None  # (type_name, name, ObjectReader)
        self.thread = None
        self.last_dir = os.path.expanduser("~")
        self.loaded = {}  # norm path -> {"env", "groups", "files", "tex_finder", "thumbs"}
        self.loading_path = None

        # thumbnails
        self.blank = blank_icon(THUMB_SIZE)
        self.blank_big = blank_icon(64)
        self.thumb_cache = {}  # key -> QIcon
        self.thumb_items = {}  # key -> tree item
        self.thumbs = ThumbnailWorker(64)
        self.thumbs.ready.connect(self.on_thumbnail)
        self.thumb_timer = QTimer(self, singleShot=True, interval=120)
        self.thumb_timer.timeout.connect(self.queue_visible_thumbs)

        # left: search + tree
        self.search = QLineEdit(placeholderText="Filter by name...")
        self.search.textChanged.connect(self.apply_filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self.on_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.verticalScrollBar().valueChanged.connect(lambda _: self.thumb_timer.start())
        self.tree.itemExpanded.connect(lambda _: self.thumb_timer.start())
        back = QPushButton("← Projects")
        back.clicked.connect(self.show_home)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(back)
        ll.addWidget(self.search)
        ll.addWidget(self.tree)

        # right: stacked previews
        self.mesh_view = MeshView()
        panel = self.mesh_view.panel
        panel.apply_texture.connect(self.apply_texture)
        panel.open_texture.connect(self.view_texture)
        panel.save_texture.connect(self.export_item)
        panel.save_model.connect(self.export_selected_mesh)
        self.image_view = ImageView()
        self.image_view.save_requested.connect(self.save_shown_image)
        self.text_view = QPlainTextEdit(readOnly=True)
        self.placeholder = QLabel("Loading...", alignment=Qt.AlignCenter)
        self.stack = QStackedWidget()
        for w in (self.placeholder, self.mesh_view, self.image_view, self.text_view):
            self.stack.addWidget(w)

        self.viewer = QSplitter()
        self.viewer.addWidget(left)
        self.viewer.addWidget(self.stack)
        self.viewer.setSizes([380, 1120])

        self.store = ProjectStore()
        self.home = HomePage(self.store, self.is_loaded)
        self.home.open_requested.connect(self.open_project)
        self.home.unload_requested.connect(self.unload_game)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.home)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        self.console = None
        if log_handler is not None:
            self.console = ConsoleDock(log_handler, self)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.console)
            self.resizeDocks([self.console], [170], Qt.Vertical)

        self._build_menu()
        self.statusBar().showMessage("Ready")
        log.info(APP_SHORT + " %s started (UnityPy %s)", __version__, UnityPy.__version__)

    def is_loaded(self, path):
        return norm_path(path) in self.loaded

    def unload_game(self, path):
        entry = self.loaded.pop(norm_path(path), None)
        if entry is None:
            return
        if self.env is entry["env"]:
            self.thumbs.clear()
            self.env = self.tex_finder = self.current = None
            self.thumb_cache, self.thumb_items = {}, {}
            self.mesh_view.poly = self.mesh_view.texture_img = None
            self.mesh_view.plotter.clear()
            self.tree.clear()
            self.placeholder.setText("This game was unloaded. Go back to Projects to open it again.")
            self.stack.setCurrentWidget(self.placeholder)
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

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, slot, key in (
            ("Projects", self.show_home, "Ctrl+Home"),
            (None, None, None),
            ("Open file...", self.open_file, "Ctrl+O"),
            ("Open game folder...", self.open_folder, "Ctrl+Shift+O"),
            (None, None, None),
            ("Save selected...", self.export_selected, "Ctrl+S"),
            ("Export all meshes (OBJ)...", lambda: self.export_all(("Mesh",)), None),
            ("Export all textures (PNG)...", lambda: self.export_all(("Texture2D", "Sprite")), None),
            (None, None, None),
            ("Quit", self.close, "Ctrl+Q"),
        ):
            if text is None:
                m.addSeparator()
                continue
            act = QAction(text, self)
            if key:
                act.setShortcut(key)
            act.triggered.connect(slot)
            m.addAction(act)

        if self.console is not None:
            view = self.menuBar().addMenu("&View")
            toggle = self.console.toggleViewAction()
            toggle.setShortcut("Ctrl+`")
            view.addAction(toggle)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Open log file", lambda: open_path(LOG_FILE))
        help_menu.addAction("Open crash log", lambda: open_path(CRASH_FILE))
        help_menu.addAction("Open app folder", lambda: os.startfile(APP_DIR))

    # ---- loading
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Unity asset file")
        if path:
            self.load(path)

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Open game folder (or any folder with Unity files)")
        if path:
            self.load(path)

    def load(self, path):
        if self.thread is not None and self.thread.isRunning():
            return
        self.pages.setCurrentWidget(self.viewer)
        self.thumbs.clear()
        self.thumb_cache, self.thumb_items = {}, {}
        self.env = self.tex_finder = self.current = None
        self.mesh_view.poly = self.mesh_view.texture_img = None
        self.mesh_view.plotter.clear()
        self.tree.clear()
        self.placeholder.setText("Loading...")
        self.stack.setCurrentWidget(self.placeholder)
        self.loading_path = path
        entry = self.loaded.get(norm_path(path))
        if entry is not None:
            log.info("Already loaded - reusing assets in memory for %s", path)
            self.on_loaded(entry["env"], entry["groups"], entry["files"])
            return
        self.thread = QThread()
        self.loader = Loader(path)
        self.loader.moveToThread(self.thread)
        self.thread.started.connect(self.loader.run)
        self.loader.progress.connect(self.statusBar().showMessage)
        self.loader.finished.connect(self.on_loaded)
        self.loader.failed.connect(self.on_load_failed)
        self.loader.finished.connect(self.thread.quit)
        self.loader.failed.connect(self.thread.quit)
        self.thread.start()

    def on_loaded(self, env, groups, file_count):
        key = norm_path(self.loading_path)
        entry = self.loaded.get(key)
        if entry is None or entry["env"] is not env:
            entry = {"env": env, "groups": groups, "files": file_count,
                     "tex_finder": TextureFinder(env), "thumbs": {}}
            self.loaded[key] = entry
        self.env = env
        self.tex_finder = entry["tex_finder"]
        self.thumb_cache = entry["thumbs"]
        text_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        total = 0
        for type_name, items in groups.items():
            if not items:
                continue
            top = QTreeWidgetItem([f"{type_name} ({len(items)})"])
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            for name, obj in items:
                child = QTreeWidgetItem([name])
                child.setData(0, Qt.UserRole, (type_name, name, obj))
                if type_name in THUMB_TYPES:
                    child.setIcon(0, self.blank)
                    self.thumb_items[thumb_key(obj)] = child
                else:
                    child.setIcon(0, text_icon)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            total += len(items)
        for key_, child in self.thumb_items.items():
            if key_ in self.thumb_cache:
                child.setIcon(0, self.thumb_cache[key_])
        self.store.set_counts(self.loading_path, files=file_count, assets=total)
        self.placeholder.setText("Pick something from the list on the left.")
        self.statusBar().showMessage(f"Loaded {total:,} assets from {file_count} file(s)")
        self.apply_filter(self.search.text())

    def on_load_failed(self, tb):
        self.statusBar().showMessage("Load failed - see the console")
        self.placeholder.setText("Load failed.")
        QMessageBox.critical(self, "Load failed", tb[-3000:])

    def apply_filter(self, text):
        text = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                child.setHidden(bool(text) and text not in child.text(0).lower())
        self.thumb_timer.start()

    # ---- thumbnails
    def queue_visible_thumbs(self):
        """Ask for thumbnails of the rows on screen (plus a few below)."""
        if self.env is None:
            return
        height = self.tree.viewport().height()
        item = self.tree.itemAt(QPoint(4, 4))
        jobs, extra = [], 0
        while item is not None and extra < 20:
            if self.tree.visualItemRect(item).top() > height:
                extra += 1
            data = item.data(0, Qt.UserRole)
            if data and data[0] in THUMB_TYPES:
                key = thumb_key(data[2])
                if key not in self.thumb_cache:
                    jobs.append((key, data[0], data[2]))
            item = self.tree.itemBelow(item)
        self.thumbs.request_visible(jobs)

    def on_thumbnail(self, key, qimg):
        icon = QIcon(QPixmap.fromImage(qimg)) if not qimg.isNull() else self.blank
        self.thumb_cache[key] = icon
        item = self.thumb_items.get(key)
        if item is not None:
            item.setIcon(0, icon)
        self.mesh_view.panel.set_icon(key, icon)

    # ---- preview
    def on_select(self):
        items = self.tree.selectedItems()
        data = items[0].data(0, Qt.UserRole) if items else None
        if not data:
            return
        self.current = data
        type_name, name, obj = data
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with UNITY_LOCK:
                asset = obj.read()
                if type_name == "Mesh":
                    self.show_mesh(name, obj, asset)
                elif type_name in ("Texture2D", "Sprite"):
                    self.show_texture(name, asset.image)
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
                tex = main_reader.read().image
            except Exception:
                pass
        self.stack.setCurrentWidget(self.mesh_view)
        self.mesh_view.show_mesh(poly, tex)
        n_tex = sum(len(m["textures"]) for m in materials)
        log.info("Model '%s': %s verts, %s tris, %d material(s), %d texture(s)%s", name,
                 f"{poly.n_points:,}", f"{poly.n_cells:,}", len(materials), n_tex,
                 "" if tex is not None else " - no texture found")

        submeshes = len(mesh.m_SubMeshes or [])
        rows = [
            f"<b>Vertices:</b> {poly.n_points:,}",
            f"<b>Triangles:</b> {poly.n_cells:,}",
            f"<b>Submeshes:</b> {submeshes}",
            f"<b>UVs:</b> {'yes' if poly.active_texture_coordinates is not None else 'no'}",
            f"<b>File:</b> {obj.assets_file.name}",
        ]
        if getattr(obj, "container", None):
            rows.append(f"<b>Path:</b> {obj.container}")
        if users:
            shown = ", ".join(sorted(set(users))[:8])
            more = f" (+{len(set(users)) - 8} more)" if len(set(users)) > 8 else ""
            rows.append(f"<b>Used by:</b> {shown}{more}")
        jobs = self.mesh_view.panel.set_info(name, "<br>".join(rows), materials, self.blank_big)
        for key, *_ in jobs:
            if key in self.thumb_cache:
                self.mesh_view.panel.set_icon(key, self.thumb_cache[key])
        self.thumbs.request_priority([j for j in jobs if j[0] not in self.thumb_cache])

    def show_texture(self, name, img):
        log.info("Texture '%s': %dx%d %s", name, img.width, img.height, img.mode)
        self.image_view.show_image(img, name)
        self.stack.setCurrentWidget(self.image_view)
        self.statusBar().showMessage(f"{name}  {img.width}x{img.height}")

    def on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        data = item.data(0, Qt.UserRole) if item else None
        if not data:
            return
        selected = [i.data(0, Qt.UserRole) for i in self.tree.selectedItems()]
        selected = [d for d in selected if d]
        menu = QMenu(self)
        if data[0] in ("Texture2D", "Sprite") and self.mesh_view.poly is not None:
            menu.addAction("Apply as texture to current model", lambda: self.apply_texture(data))
        menu.addAction("Save...", lambda: self.export_item(data))
        if len(selected) > 1:
            menu.addAction(f"Save {len(selected)} selected to folder...",
                           lambda: self.export_many(selected))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def apply_texture(self, data):
        try:
            with UNITY_LOCK:
                img = data[2].read().image
            self.mesh_view.set_texture(img)
            self.stack.setCurrentWidget(self.mesh_view)
        except Exception as e:
            QMessageBox.warning(self, "Texture", str(e))

    def view_texture(self, data):
        try:
            with UNITY_LOCK:
                img = data[2].read().image
            self.show_texture(data[1], img)
        except Exception as e:
            QMessageBox.warning(self, "Texture", str(e))

    # ---- export
    def ask_save_path(self, default_name, filter_text):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save", os.path.join(self.last_dir, default_name), filter_text)
        if path:
            self.last_dir = os.path.dirname(path)
        return path

    def export_selected(self):
        if self.current:
            self.export_item(self.current)

    def export_selected_mesh(self):
        if self.current and self.current[0] == "Mesh":
            self.export_item(self.current)

    def save_shown_image(self):
        img = self.image_view.image
        if img is None:
            return
        name = self.current[1] if self.current else "texture"
        path = self.ask_save_path(f"{safe_filename(name)}.png", "PNG image (*.png)")
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
        ext, filt = {
            "Mesh": ("obj", "Wavefront OBJ (*.obj)"),
            "Texture2D": ("png", "PNG image (*.png)"),
            "Sprite": ("png", "PNG image (*.png)"),
            "TextAsset": ("txt", "All files (*)"),
        }[type_name]
        path = self.ask_save_path(f"{safe_filename(name)}.{ext}", filt)
        if not path:
            return
        try:
            written = self.write_asset(type_name, obj, path)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(w) for w in written))
            for w in written:
                log.info("Saved %s", w)
        except Exception as e:
            log.exception("Saving %s failed", name)
            QMessageBox.warning(self, "Save failed", str(e))

    def write_asset(self, type_name, obj, path):
        """Save one asset to `path`; returns the list of files written."""
        with UNITY_LOCK:
            asset = obj.read()
            if type_name == "Mesh":
                return self.write_mesh(obj, asset, path)
            if type_name in ("Texture2D", "Sprite"):
                asset.image.save(path)
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
                        reader.read().image.save(png_path)
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
        folder = QFileDialog.getExistingDirectory(self, "Save selected to", self.last_dir)
        if folder:
            self.last_dir = folder
            self._export_to_folder(items, folder)

    def export_all(self, type_names):
        if self.env is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Export to", self.last_dir)
        if not folder:
            return
        self.last_dir = folder
        items = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                data = top.child(j).data(0, Qt.UserRole)
                if data and data[0] in type_names:
                    items.append(data)
        self._export_to_folder(items, folder)

    def _export_to_folder(self, items, folder):
        log.info("Exporting %d item(s) to %s", len(items), folder)
        ok = fail = 0
        used = set()
        ext = {"Mesh": "obj", "Texture2D": "png", "Sprite": "png", "TextAsset": "txt"}
        for type_name, name, obj in items:
            base = safe_filename(name)
            fname, n = base, 1
            while fname.lower() in used:
                n += 1
                fname = f"{base}_{n}"
            used.add(fname.lower())
            try:
                self.write_asset(type_name, obj, os.path.join(folder, f"{fname}.{ext[type_name]}"))
                ok += 1
            except Exception as e:
                fail += 1
                log.warning("Could not export %s: %s", name, e)
            if (ok + fail) % 25 == 0:
                self.statusBar().showMessage(f"Exporting ... {ok} done, {fail} failed")
                QApplication.processEvents()
        self.statusBar().showMessage(f"Exported {ok} item(s) ({fail} failed) to {folder}")
        log.info("Exported %d item(s), %d failed", ok, fail)

    def closeEvent(self, event):
        self.thumbs.clear()
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
                                  ("Texture2D", lambda o: o.read().image.load())):
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
