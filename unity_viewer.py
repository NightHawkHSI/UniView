"""UniView - Game Asset Viewer. Browse models, textures and text assets from Unity, Source,
Source 2 and Unreal games - and any other engine someone writes a plugin for.

Starts on a projects page with a box per saved game (add one by folder or let it find games
in your Steam libraries). Each game is read by an engine plugin (engines/ for the built-in
ones, plugins/ for your own - see PLUGINS.md); pick an item in the list and it previews on
the right. Models render in 3D with their texture when the plugin can find it, textures
show as images.
"""

__version__ = "2.0.0"
APP_SHORT = "UniView"
APP_TITLE = "UniView - Game Asset Viewer"

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
from datetime import datetime
from html import escape as html_escape

os.environ.setdefault("QT_API", "pyside6")

import numpy as np
import pyvista as pv
from PIL import Image
from PySide6.QtCore import (
    QEvent, QFileInfo, QObject, QPoint, QRect, QRectF, QSignalBlocker, QSize, Qt, QThread, QTimer, QUrl,
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
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSplitter,
    QStackedWidget, QStyle, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PIL import ImageDraw
from pyvistaqt import QtInteractor

import engines
from engines.sdk import IMAGE_KINDS, KIND_LABELS, KINDS, NORMAL, THUMB_KINDS, Progress, main_texture

MAX_TEXT_CHARS = 200_000
THUMB_SIZE = 40      # icon size in the list
GRID_THUMB = 96      # thumbnail size generated (grid view uses it 1:1)
THUMB_CACHE_MAX = 4000
GRID_LIMIT = 20000
REPO_URL = "https://github.com/NightHawkHSI/UniView"
COMPAT_URL = "https://raw.githubusercontent.com/NightHawkHSI/UniView/main/compat.json"
SORT_ROLE = Qt.UserRole + 10   # value used when sorting a tree column
TYPE_ROLE = Qt.UserRole + 11   # asset kind stored on group rows

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
PLUGINS_DIR = os.path.join(APP_DIR, "plugins")

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


# --------------------------------------------------------------------------- loading

class Loader(QObject):
    """Opens a game with its engine plugin off the UI thread."""

    progress = Signal(str)
    progress_value = Signal(int, int)  # done, total (total 0 = busy)
    finished = Signal(object)          # GameSession
    failed = Signal(str)

    def __init__(self, path, plugin, options=None):
        super().__init__()
        self.path = path
        self.plugin = plugin
        self.options = options or {}

    def run(self):
        try:
            started = time.time()
            log.info("Opening %s with the %s plugin", self.path, self.plugin.name)
            self.progress_value.emit(0, 0)
            session = self.plugin.open(self.path, Progress(self.progress.emit, self.progress_value.emit, self.options))
            self.progress.emit("Sorting ...")
            session.assets.sort(key=lambda a: a.name.lower())
            counts = {}
            for asset in session.assets:
                counts[asset.kind] = counts.get(asset.kind, 0) + 1
            log.info("Loaded in %.1fs: %s", time.time() - started,
                     ", ".join(f"{n:,} {KIND_LABELS.get(k, k).lower()}" for k, n in counts.items()) or "nothing")
            for warning in session.warnings:
                log.warning("%s", warning)
            self.finished.emit(session)
        except Exception:
            tb = traceback.format_exc()
            log.error("Loading failed:\n%s", tb.rstrip())
            self.failed.emit(tb)


# --------------------------------------------------------------------------- mesh conversion

def meshdata_to_polydata(md):
    """MeshData (already Y-up, CCW) -> pyvista PolyData with UV sets and vertex colors."""
    tris = md.triangles
    faces = np.hstack([np.full((len(tris), 1), 3, dtype=np.int64), tris]).ravel()
    poly = pv.PolyData(md.points.copy(), faces)
    for name, uv in md.uvs.items():
        poly.point_data[name] = uv
    if md.uvs:
        poly.active_texture_coordinates = poly.point_data[next(iter(md.uvs))]
    if md.colors is not None:
        poly.point_data["vertex_colors"] = (np.clip(md.colors, 0, 1) * 255).astype(np.uint8)
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


def make_thumbnail(session, asset, size):
    with session.lock:
        if asset.kind == "model":
            md = session.mesh(asset)
        else:
            img = session.image(asset)
    if asset.kind == "model":
        return render_mesh_thumbnail(md.points, md.triangles, size)
    img = img.convert("RGBA")
    scale = size / max(img.width, img.height)
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    # Pixel-art look for tiny textures, smooth for big ones.
    return img.resize(new_size, Image.NEAREST if scale > 1 else Image.LANCZOS)


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
                key, session, asset = (self._priority or self._visible).pop(0)
            try:
                qimg = pil_to_qimage(make_thumbnail(session, asset, self.size))
            except Exception as e:
                log.debug("No thumbnail for %s '%s': %s", asset.kind, asset.name, e)
                qimg = QImage()
            try:
                self.ready.emit(key, qimg)
            except RuntimeError:
                return  # window closed


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
            for key, session, asset in batch_jobs:
                try:
                    with session.lock:
                        stats = session.stats(asset)
                    if stats.get("size") is None:
                        stats["size"] = asset.size
                    batch.append((key, stats))
                except Exception as e:
                    log.debug("No stats for %s '%s': %s", asset.kind, asset.name, e)
                    batch.append((key, {"size": asset.size, "info": "?", "sort": -1}))
            with self._cv:
                if gen != self._gen:
                    continue  # a different game was opened meanwhile
                remaining = len(self._jobs)
            try:
                self.ready.emit(batch, remaining)
            except RuntimeError:
                return  # window closed


FILTER_HELP = """Search by name, or add filters (no spaces around the sign):
  tris>1000     triangles (models)
  verts<500     vertices (models)
  size>1mb      data size (kb, mb, gb)
  w>=512 h>=512 width / height (textures, sprites)
  type:model    type (model, texture, sprite, text, audio, file)
Example:  gun tris>2000 size<5mb"""
FILTER_FIELDS = {"tris": "tris", "verts": "verts", "size": "size",
                 "w": "w", "width": "w", "h": "h", "height": "h"}
FILTER_TOKEN = re.compile(r"^([a-z]+)(>=|<=|>|<|=|:)(.+)$", re.I)
FILTER_OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le, "=": operator.eq}
FILTER_UNITS = {"": 1, "k": 1e3, "m": 1e6, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
TYPE_ALIASES = {"mesh": "model", "tex": "texture", "texture2d": "texture", "textasset": "text",
                "sound": "audio", "other": "file"}


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

    def matches(self, name, kind, stats):
        low = name.lower()
        if any(word not in low for word in self.words):
            return False
        if self.type and not kind.startswith(self.type):
            return False
        for field, op, number in self.conditions:
            value = (stats or {}).get(field)
            if value is None or not op(value, number):
                return False
        return True


# --------------------------------------------------------------------------- export helpers

def export_subfolder(asset):
    """Folder that mirrors where the asset lives in the game (its path, else source file + kind)."""
    if asset.path:
        parts = asset.path.replace("\\", "/").strip("/").split("/")
        stem = os.path.splitext(parts[-1])[0]
        base = os.path.basename(asset.name.replace("\\", "/"))
        folder = parts[:-1] + ([stem] if stem.lower() != base.lower() else [])
    else:
        folder = [asset.source or "unknown", KIND_LABELS.get(asset.kind, asset.kind)]
    return os.path.join(*[safe_filename(part)[:80] for part in folder if part] or ["."])


def material_for(materials, md, index):
    """Material of submesh `index`, or None."""
    if not materials:
        return None
    slot = md.material_slots[index] if index < len(md.material_slots) else index
    return materials[min(slot, len(materials) - 1)]


def display_texture(md, materials):
    """Texture Asset to show on the model: the base texture of the material covering the most triangles."""
    for j in sorted(range(len(md.submeshes)), key=lambda j: -len(md.submeshes[j])):
        mat = material_for(materials, md, j)
        tex = mat.main_texture() if mat is not None else None
        if tex is not None:
            return tex.asset
    return main_texture(materials)


def write_glb(session, md, materials, path):
    """Binary glTF: positions, normals, UVs, vertex colors and embedded base-color textures.

    One primitive per submesh, each with its material. Opens directly in Blender.
    """
    count = len(md.points)
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

    attributes = {"POSITION": add_accessor(md.points, "VEC3", bounds=True)}
    if md.normals is not None:
        normals = md.normals.astype(np.float32)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.where(lengths > 1e-8, normals / np.maximum(lengths, 1e-8), [0, 1, 0]).astype(np.float32)
        attributes["NORMAL"] = add_accessor(normals, "VEC3")
    if md.uvs:
        uv = next(iter(md.uvs.values())).copy()
        uv[:, 1] = 1.0 - uv[:, 1]  # glTF's UV origin is top-left
        attributes["TEXCOORD_0"] = add_accessor(uv, "VEC2")
    if md.colors is not None and len(md.colors) == count:
        attributes["COLOR_0"] = add_accessor(np.clip(md.colors, 0, 1).astype(np.float32), "VEC4")

    gl_materials, images, textures, texture_index, mat_index = [], [], [], {}, {}
    for mat in materials:
        entry = {"name": mat.name or "material", "doubleSided": True,
                 "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}
        tex = mat.main_texture()
        if tex is not None:
            key = tex.asset.key
            if key not in texture_index:
                texture_index[key] = None
                try:
                    png = io.BytesIO()
                    with session.lock:
                        img = session.image(tex.asset)
                    img.convert("RGBA").save(png, "PNG")
                    images.append({"bufferView": add_view(png.getvalue()), "mimeType": "image/png",
                                   "name": tex.name or "texture"})
                    textures.append({"source": len(images) - 1, "sampler": 0})
                    texture_index[key] = len(textures) - 1
                except Exception as e:
                    log.warning("Could not embed a texture in %s: %s", os.path.basename(path), e)
            if texture_index[key] is not None:
                entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_index[key]}
        mat_index[id(mat)] = len(gl_materials)
        gl_materials.append(entry)

    primitives = []
    for j, tris in enumerate(md.submeshes):
        if not len(tris):
            continue
        indices = np.asarray(tris, dtype=np.uint32).ravel()
        prim = {"attributes": attributes, "mode": 4,
                "indices": add_accessor(indices, "SCALAR", 5125, 34963)}
        mat = material_for(materials, md, j)
        if mat is not None:
            prim["material"] = mat_index[id(mat)]
        primitives.append(prim)
    if not primitives:
        raise ValueError("Mesh has no triangles to export.")

    name = md.name or os.path.splitext(os.path.basename(path))[0]
    gltf = {
        "asset": {"version": "2.0", "generator": f"{APP_SHORT} {__version__}"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": primitives}],
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


def write_obj(session, md, materials, path):
    """<name>.obj + <name>.mtl + PNG textures, so it opens textured in Blender."""
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    written, mtl, mat_names = [path], [], {}
    for i, mat in enumerate(materials):
        mat_name = f"{safe_filename(mat.name or 'material')}_{i}"
        mat_names[id(mat)] = mat_name
        mtl += [f"newmtl {mat_name}", "Kd 1 1 1"]
        have_diffuse = False
        for tex in mat.textures:
            png = f"{stem}_{safe_filename(tex.name)}.png"
            png_path = os.path.join(folder, png)
            try:
                if png_path not in written:
                    with session.lock:
                        img = session.image(tex.asset)
                    img.save(png_path)
                    written.append(png_path)
            except Exception:
                continue
            if tex.role == NORMAL:
                mtl.append(f"map_Bump {png}")
            elif not have_diffuse:
                mtl.append(f"map_Kd {png}")
                have_diffuse = True
        mtl.append("")

    uv = next(iter(md.uvs.values()), None)
    lines = [f"# {APP_SHORT} {__version__}"]
    if materials:
        lines.append(f"mtllib {stem}.mtl")
    lines.append(f"o {safe_filename(md.name or stem)}")
    lines += [f"v {x:.6g} {y:.6g} {z:.6g}" for x, y, z in md.points.tolist()]
    if uv is not None:
        lines += [f"vt {u:.6g} {v:.6g}" for u, v in uv.tolist()]
    if md.normals is not None:
        lines += [f"vn {x:.6g} {y:.6g} {z:.6g}" for x, y, z in md.normals.tolist()]
    if uv is not None:
        fmt = "{0}/{0}/{0}" if md.normals is not None else "{0}/{0}"
    else:
        fmt = "{0}//{0}" if md.normals is not None else "{0}"
    for j, tris in enumerate(md.submeshes):
        mat = material_for(materials, md, j)
        lines.append(f"g part{j}")
        if mat is not None:
            lines.append(f"usemtl {mat_names[id(mat)]}")
        for a, b, c in (np.asarray(tris) + 1).tolist():
            lines.append(f"f {fmt.format(a)} {fmt.format(b)} {fmt.format(c)}")
    if materials:
        mtl_path = os.path.join(folder, stem + ".mtl")
        with open(mtl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(mtl))
        written.append(mtl_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return written


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
            f"**Engine:** {engine_info_text(project) or '?'}\n"
            f"**Game files / assets:** {project.get('file_count', '?')} / {project.get('asset_count', '?')}\n\n"
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

    apply_texture = Signal(object)  # texture Asset
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
        """Fill the panel; returns the texture Assets that need thumbnails."""
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
            header = QListWidgetItem(f"Material: {mat.name or '(unnamed)'}")
            header.setFlags(Qt.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.textures.addItem(header)
            if not mat.textures:
                none = QListWidgetItem("    (no textures)")
                none.setFlags(Qt.NoItemFlags)
                self.textures.addItem(none)
            for tex in mat.textures:
                item = QListWidgetItem(blank_icon, f"{tex.name}\n{tex.slot}")
                item.setData(Qt.UserRole, tex.asset)
                item.setToolTip(f"{tex.asset.name}\nslot: {tex.slot}"
                                + (f"\nfile: {tex.asset.source}" if tex.asset.source else ""))
                self.textures.addItem(item)
                self._items.setdefault(tex.asset.key, []).append(item)
                jobs.append(tex.asset)
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
        self.override = None     # texture the user put on the whole model
        self.parts = []          # [(PolyData of one submesh, its texture or None)]
        self._textures = {}      # (id(img), alpha, flip) -> pv.Texture

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

        # Animation playback (shown while an animation plays on the model)
        self.animator = None
        self.anim_time = 0.0
        self.anim_timer = QTimer(self, interval=33)
        self.anim_timer.timeout.connect(self._anim_tick)
        self.anim_play = QPushButton("\u23f8 Pause")
        self.anim_play.clicked.connect(self._anim_toggle)
        self.anim_slider = QSlider(Qt.Horizontal)
        self.anim_slider.setRange(0, 1000)
        self.anim_slider.sliderMoved.connect(self._anim_seek)
        self.anim_label = QLabel()
        anim_stop = QPushButton("Stop animation")
        anim_stop.clicked.connect(self.stop_animation)
        self.anim_bar = QWidget()
        ab = QHBoxLayout(self.anim_bar)
        ab.setContentsMargins(0, 0, 0, 0)
        ab.addWidget(self.anim_play)
        ab.addWidget(self.anim_slider, 1)
        ab.addWidget(self.anim_label)
        ab.addWidget(anim_stop)
        self.anim_bar.hide()
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
        lay.addWidget(self.anim_bar)
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
        self._home_camera()
        self.plotter.render()

    def _home_camera(self):
        """Models are Y-up: look from the front-right, slightly above."""
        self.plotter.view_xy()
        self.plotter.camera.azimuth = 35
        self.plotter.camera.elevation = 20
        self.plotter.reset_camera()

    def uv_channels(self):
        if self.poly is None:
            return []
        return sorted(k for k in self.poly.point_data.keys() if re.fullmatch(r"UV\d", k))

    def set_uv_channel(self, name):
        if self.poly is None or name not in self.poly.point_data:
            return
        for mesh in [self.poly] + [part for part, _img in self.parts]:
            mesh.active_texture_coordinates = mesh.point_data[name]
        self.redraw()

    # ---- animation
    def play_animation(self, animator, title=""):
        """Animate the shown model: animator.points_at(t) gives the posed vertices."""
        self.animator = animator
        self.anim_time = 0.0
        self._rest_points = self.poly.points.copy() if self.poly is not None else None
        self.anim_bar.show()
        self.anim_label.setToolTip(title)
        self.anim_play.setText("\u23f8 Pause")
        for mesh in [self.poly] + [part for part, _img in self.parts]:
            for name in ("Normals", "Normals_"):
                if mesh is not None and name in mesh.point_data:
                    mesh.point_data.remove(name)  # stale normals would freeze the lighting
        self.redraw()  # flat shading while animating: lighting follows the moving vertices
        self._anim_apply()
        self._home_camera()
        self.plotter.camera.zoom(0.75)  # room for arms and legs to move
        self.plotter.render()
        self.anim_timer.start()

    def stop_animation(self):
        self.anim_timer.stop()
        if self.animator is not None and getattr(self, "_rest_points", None) is not None and self.poly is not None:
            self._set_points(self._rest_points)
            self.plotter.render()
        self.animator = None
        self.anim_bar.hide()
        self.redraw()

    def _set_points(self, pts):
        meshes = [(self.poly, pts)] + [(part, pts[used]) for (part, _img), used
                                       in zip(self.parts, getattr(self, "_part_vertices", []))]
        for mesh, pts in meshes:
            mesh.points = pts
            # Face normals for lighting (the Qt view doesn't derive them itself in flat mode).
            tris = mesh.faces.reshape(-1, 4)[:, 1:]
            n = np.cross(pts[tris[:, 1]] - pts[tris[:, 0]], pts[tris[:, 2]] - pts[tris[:, 0]])
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
            mesh.cell_data["Normals"] = n.astype(np.float32)
            mesh.cell_data.active_normals_name = "Normals"

    def _anim_apply(self):
        if self.animator is None or self.poly is None:
            return
        try:
            self._set_points(self.animator.points_at(self.anim_time))
        except Exception:
            log.exception("Animation frame failed")
            self.stop_animation()
            return
        length = self.animator.length
        if not self.anim_slider.isSliderDown():
            self.anim_slider.setValue(int(1000 * (self.anim_time % length) / length))
        self.anim_label.setText(f"{self.anim_time % length:.2f} / {length:.2f} s")
        self.plotter.render()

    def _anim_tick(self):
        self.anim_time += self.anim_timer.interval() / 1000.0
        self._anim_apply()

    def _anim_toggle(self):
        if self.anim_timer.isActive():
            self.anim_timer.stop()
            self.anim_play.setText("\u25b6 Play")
        else:
            self.anim_timer.start()
            self.anim_play.setText("\u23f8 Pause")

    def _anim_seek(self, value):
        if self.animator is not None:
            self.anim_time = value / 1000.0 * self.animator.length
            self._anim_apply()

    def show_mesh(self, poly, texture_img, parts=None):
        """parts: [(faces array (M, 3), texture image or None)] to texture each submesh on its own."""
        self.anim_timer.stop()
        self.animator = None
        self.anim_bar.hide()
        self.poly = poly
        self.texture_img = texture_img
        self.override = None
        self._textures = {}
        self.parts = []
        if parts and len({id(img) for _f, img in parts if img is not None}) > 1:
            self._part_vertices = []
            for tris, img in parts:
                # Each part keeps only its own vertices (big maps have hundreds of parts).
                used, local = np.unique(tris, return_inverse=True)
                faces = np.hstack([np.full((len(tris), 1), 3, dtype=np.int64),
                                   local.reshape(-1, 3)]).ravel()
                part = pv.PolyData(poly.points[used], faces)
                for key in poly.point_data.keys():
                    part.point_data[key] = poly.point_data[key][used]
                if poly.active_texture_coordinates is not None:
                    part.active_texture_coordinates = np.asarray(poly.active_texture_coordinates)[used]
                self.parts.append((part, img))
                self._part_vertices.append(used)
        with QSignalBlocker(self.uv_combo):
            self.uv_combo.clear()
            channels = self.uv_channels()
            self.uv_combo.addItems(channels or ["none"])
            self.uv_combo.setEnabled(len(channels) > 1)
        tex_note = (f"{len(self.parts)} textured parts" if self.parts else
                    "textured" if texture_img is not None else "no texture found")
        self.info.setText(f"{poly.n_points:,} verts  {poly.n_cells:,} tris  ({tex_note})")
        self.redraw(reset_camera=True)

    def set_texture(self, img):
        self.override = self.texture_img = img
        self.redraw()

    def _texture(self, img):
        alpha, flip = self.tex_alpha.isChecked(), self.flip_v.isChecked()
        key = (id(img), alpha, flip)
        if key not in self._textures:
            arr = np.asarray(img.convert("RGBA" if alpha else "RGB"))
            if flip:
                arr = arr[::-1]
            self._textures[key] = pv.Texture(np.ascontiguousarray(arr))
        return self._textures[key]

    def redraw(self, *_, reset_camera=False):
        self.plotter.clear_actors()  # clear() would also drop the lights
        if not self.plotter.renderer.lights:
            self.plotter.enable_lightkit()
        if self.poly is None:
            return
        animating = self.animator is not None
        kwargs = dict(
            style="wireframe" if self.wire.isChecked() else "surface",
            show_edges=self.edges.isChecked(),
            smooth_shading=not animating,
            split_sharp_edges=not animating,
        )
        has_uv = self.poly.active_texture_coordinates is not None
        if self.use_tex.isChecked() and has_uv and self.parts and self.override is None:
            # Each part with its own material's texture.
            for part, img in self.parts:
                part_kwargs = dict(kwargs)
                if img is not None:
                    part_kwargs["texture"] = self._texture(img)
                elif self.use_colors.isChecked() and "vertex_colors" in part.point_data:
                    part_kwargs.update(scalars="vertex_colors", rgba=True)
                else:
                    part_kwargs["color"] = "#c8c8c8"
                self.plotter.add_mesh(part, **part_kwargs)
            if reset_camera:
                self._home_camera()
            self.plotter.render()
            return
        if self.use_tex.isChecked() and self.texture_img is not None and has_uv:
            kwargs["texture"] = self._texture(self.texture_img)
        elif self.use_colors.isChecked() and "vertex_colors" in self.poly.point_data:
            kwargs.update(scalars="vertex_colors", rgba=True)
        else:
            kwargs["color"] = "#c8c8c8"
        self.plotter.add_mesh(self.poly, **kwargs)
        if reset_camera:
            self._home_camera()
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


class AudioView(QWidget):
    """Sound player: play/pause, seek, volume, save."""

    save_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self.settings = settings
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(settings.get("volume", 0.7))
        self.player.setAudioOutput(self.output)
        self._file_index = 0

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #e0a030;")

        self.play_btn = QPushButton("\u25b6 Play")
        self.play_btn.setMinimumWidth(110)
        self.play_btn.clicked.connect(self.toggle)
        stop = QPushButton("\u25a0 Stop")
        stop.clicked.connect(self.player.stop)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("0:00 / 0:00")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(self.output.volume() * 100))
        self.volume.setFixedWidth(120)
        self.volume.valueChanged.connect(self._set_volume)
        self.autoplay = QCheckBox("Autoplay")
        self.autoplay.setToolTip("Start playing as soon as a sound is picked (handy for flipping through sounds)")
        self.autoplay.setChecked(settings.get("autoplay", False))
        self.autoplay.toggled.connect(lambda on: (settings.__setitem__("autoplay", on), settings.save()))
        save = QPushButton("Save sound...")
        save.clicked.connect(self.save_requested)

        controls = QHBoxLayout()
        for w in (self.play_btn, stop):
            controls.addWidget(w)
        controls.addWidget(self.seek, 1)
        controls.addWidget(self.time)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Volume"))
        controls.addWidget(self.volume)
        controls.addWidget(self.autoplay)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(self.title)
        lay.addWidget(self.details)
        lay.addSpacing(12)
        lay.addLayout(controls)
        lay.addWidget(self.status)
        lay.addStretch()
        lay.addWidget(save, 0, Qt.AlignLeft)

        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(self._duration)
        self.player.playbackStateChanged.connect(self._state)
        self.player.errorOccurred.connect(lambda _e, text: self.status.setText(f"Can't play this sound: {text}"))

    @staticmethod
    def _fmt(ms):
        sec = max(0, ms) // 1000
        return f"{sec // 60}:{sec % 60:02d}"

    def _set_volume(self, value):
        self.output.setVolume(value / 100)
        self.settings["volume"] = value / 100
        self.settings.save()

    def _position(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time.setText(f"{self._fmt(ms)} / {self._fmt(self.player.duration())}")

    def _duration(self, ms):
        self.seek.setRange(0, ms)
        self.time.setText(f"{self._fmt(self.player.position())} / {self._fmt(ms)}")

    def _state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        self.play_btn.setText("\u23f8 Pause" if state == QMediaPlayer.PlayingState else "\u25b6 Play")

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def load(self, name, data, ext, rows):
        """Play-ready bytes (wav/mp3/ogg...) -> a temp file the player opens."""
        self.stop()
        folder = os.path.join(tempfile.gettempdir(), APP_SHORT)
        os.makedirs(folder, exist_ok=True)
        self._file_index ^= 1  # alternate files: the previous one may still be locked by the player
        path = os.path.join(folder, f"preview_{self._file_index}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        self.title.setText(name)
        self.details.setText("<br>".join(f"<b>{html_escape(k)}:</b> {html_escape(v)}" for k, v in rows)
                             + f"<br><b>Plays as:</b> {ext.upper()}, {fmt_size(len(data))}")
        self.status.setText("")
        self.seek.setRange(0, 0)
        self.time.setText("0:00 / 0:00")
        self.player.setSource(QUrl.fromLocalFile(path))
        if self.autoplay.isChecked():
            self.player.play()

    def show_error(self, name, message, rows):
        self.stop()
        self.title.setText(name)
        self.details.setText("<br>".join(f"<b>{html_escape(k)}:</b> {html_escape(v)}" for k, v in rows))
        self.status.setText(f"{message}\n\nSave sound... still exports the file as it is stored in the game.")
        self.seek.setRange(0, 0)
        self.time.setText("")


class AnimationView(QWidget):
    """An animation clip: its summary, and the models it can be played on."""

    play_requested = Signal(object)  # model Asset

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.targets = QComboBox()
        self.targets.setMinimumWidth(320)
        self.play = QPushButton("\u25b6 Play on model")
        self.play.clicked.connect(self._play)
        self.note = QLabel()
        self.note.setStyleSheet("color: gray;")
        row = QHBoxLayout()
        row.addWidget(QLabel("Model:"))
        row.addWidget(self.targets, 1)
        row.addWidget(self.play)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.note)
        lay.addWidget(self.text, 1)

    def show_clip(self, text, targets, note=""):
        self.text.setPlainText(text)
        self.targets.clear()
        for model in targets:
            self.targets.addItem(model.name, model)
        self.play.setEnabled(bool(targets))
        self.targets.setEnabled(bool(targets))
        self.note.setText(note or ("Pick a model to play this animation on (best matches first)." if targets else
                                   "No model with a matching skeleton was found for this animation."))

    def _play(self):
        model = self.targets.currentData()
        if model is not None:
            self.play_requested.emit(model)


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
EXE_SKIP = ("crash", "unins", "setup", "redist", "launcher", "helper", "bootstrap", "report")


def game_exe(game_dir):
    """The game's main .exe (for its icon), or None."""
    from engines.unity import unity_data_dir
    data = unity_data_dir(game_dir)
    if data:
        exe = data[: -len("_Data")] + ".exe"
        if os.path.isfile(exe):
            return exe
    if not os.path.isdir(game_dir):
        return None
    # Top level first (Unity, Unreal, Source), then Source 2's game/bin/win64.
    for folder in (game_dir, os.path.join(game_dir, "game", "bin", "win64")):
        try:
            exes = [f for f in os.listdir(folder) if f.lower().endswith(".exe")]
        except OSError:
            continue
        exes = [f for f in exes if not any(s in f.lower() for s in EXE_SKIP)]
        if exes:
            folder_name = os.path.basename(os.path.normpath(game_dir)).lower()
            exes.sort(key=lambda f: (folder_name.replace(" ", "") not in f.lower().replace(" ", ""), len(f)))
            return os.path.join(folder, exes[0])
    return None


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


def find_steam_games():
    """[(name, path, plugin)] for every game in the Steam libraries that an engine plugin recognizes."""
    games = []
    for common in steam_library_dirs():
        for name in sorted(os.listdir(common)):
            path = os.path.join(common, name)
            if not os.path.isdir(path):
                continue
            plugin, score = engines.detect(path)
            if plugin is not None and score >= 50:
                games.append((name, path, plugin))
    return games


def version_key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def project_info(project):
    return {"engine_version": project.get("engine_version", ""), "detail": project.get("engine_detail", "")}


def engine_info_text(project):
    """'Unity 2019.4.40f1 · IL2CPP', 'Source · tf, hl2', ... for a project (or '' if unknown)."""
    plugin = engines.get(project.get("engine") or "")
    info = project_info(project)
    parts = []
    if plugin is not None:
        parts.append(plugin.label(info))
    elif project.get("engine"):
        parts.append(f"{project['engine']} (plugin missing)")
    if info["detail"]:
        parts.append(info["detail"])
    return " \u00b7 ".join(p for p in parts if p)


def engine_group(project):
    """Group title for 'Group by engine version'."""
    plugin = engines.get(project.get("engine") or "")
    if plugin is None:
        return "Unknown engine"
    try:
        return plugin.short_version(project_info(project)) or plugin.name
    except Exception:
        return plugin.name


def detect_project_engine(path, engine_id=None):
    """{'engine', 'engine_version', 'engine_detail'} for a game folder (reads headers only)."""
    plugin = engines.get(engine_id) if engine_id else None
    if plugin is None:
        plugin, _score = engines.detect(path)
    if plugin is None:
        return {"engine": "", "engine_version": "", "engine_detail": ""}
    info = plugin.game_info(path) or {}
    return {"engine": plugin.id, "engine_version": info.get("engine_version", ""),
            "engine_detail": info.get("detail", "")}


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
        # Projects saved by UniView 1.x were always Unity games.
        migrated = False
        for project in self.projects:
            if "unity_version" in project and "engine" not in project:
                project["engine"] = "unity"
                project["engine_version"] = project.pop("unity_version")
                project["engine_detail"] = project.pop("backend", "")
                migrated = True
        if migrated:
            self.save()

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

    def update(self, path, **fields):
        project = self.get(path)
        if project is not None:
            project.update(fields)
            self.save()

    def all_tags(self):
        return sorted({t for p in self.projects for t in p.get("tags", [])}, key=str.lower)

    def add(self, name, path, engine=None):
        if not self.has(path):
            project = {"name": name, "path": path, "last_opened": 0}
            if engine:
                project["engine"] = engine
            self.projects.append(project)
            self.save()
            log.info("Added game '%s' (%s)", name, path)

    def remove(self, project):
        self.projects.remove(project)
        self.save()

    def touch(self, project):
        project["last_opened"] = time.time()
        self.save()

    SORTS = {
        "recent": lambda p: (-p.get("last_opened", 0), p["name"].lower()),
        "name": lambda p: p["name"].lower(),
        "engine": lambda p: (p.get("engine") or "~", tuple(-n for n in version_key(p.get("engine_version")))),
        "assets": lambda p: (-(p.get("asset_count") or 0), -(p.get("file_count") or 0)),
    }

    def sorted(self, by="recent"):
        """Pinned games first, then by the chosen order (most recently opened by default)."""
        key = self.SORTS.get(by, self.SORTS["recent"])
        return sorted(self.projects, key=lambda p: (not p.get("pinned"), key(p), p["name"].lower()))


class SteamPickDialog(QDialog):
    """Checklist of games found in Steam libraries that an engine plugin can read."""

    def __init__(self, games, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Games found in Steam")
        self.resize(520, 560)
        self.list = QListWidget()
        icons = QFileIconProvider()
        for name, path, plugin in games:
            item = QListWidgetItem(f"{name}    ({plugin.name})")
            item.setData(Qt.UserRole, (name, path, plugin.id))
            exe = game_exe(path)
            if exe:
                item.setIcon(icons.icon(QFileInfo(exe)))
            if store.has(path):
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setText(f"{name}    ({plugin.name}, already added)")
            else:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
            item.setToolTip(path)
            self.list.addItem(item)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the games to add (the engine plugin that will read each is in brackets):"))
        lay.addWidget(self.list)
        lay.addWidget(buttons)

    def chosen(self):
        """[(name, path, engine id)]"""
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.flags() & Qt.ItemIsEnabled and item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out


class CardDelegate(QStyledItemDelegate):
    """Draws a game box: icon, name, counts, compatibility. Green = loaded, red = not loaded."""

    STATE_ROLE = Qt.UserRole + 1
    SUB_ROLE = Qt.UserRole + 2
    PIN_ROLE = Qt.UserRole + 3
    NOTES_ROLE = Qt.UserRole + 4
    TAGS_ROLE = Qt.UserRole + 5
    STYLES = {  # state: (border, fill)
        "loaded": ("#43a047", QColor(67, 160, 71, 60)),
        "unloaded": ("#e53935", QColor(229, 57, 53, 40)),
        "missing": ("#888888", QColor(128, 128, 128, 35)),
        "action": ("#666666", QColor(0, 0, 0, 0)),
    }
    SIZE = QSize(176, 244)
    ICON = 80

    def sizeHint(self, option, index):
        return index.data(Qt.SizeHintRole) or self.SIZE  # group headers set their own (full-width) size

    def paint(self, painter, option, index):
        state = index.data(self.STATE_ROLE) or "action"
        if state == "header":
            self._paint_header(painter, option, index)
            return
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

        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() * 0.9))
        tags = index.data(self.TAGS_ROLE)
        tag_height = 0
        if tags:
            # Tags sit on the bottom line of the box, cut short with "..." if they don't fit.
            metrics = QFontMetrics(small)
            tag_height = metrics.height() + 2
            tag_rect = QRect(text_rect.left(), text_rect.bottom() - metrics.height(),
                             text_rect.width(), metrics.height())
            painter.setFont(small)
            painter.setPen(QColor("#5aa0e6"))
            text = "  ".join(f"#{t}" for t in tags)
            painter.drawText(tag_rect, Qt.AlignHCenter | Qt.AlignVCenter,
                             metrics.elidedText(text, Qt.ElideRight, tag_rect.width()))

        sub = index.data(self.SUB_ROLE)
        if sub:
            used = QFontMetrics(name_font).boundingRect(text_rect, flags, name).height()
            painter.setFont(small)
            painter.setPen(QColor("#a0a0a0"))
            painter.drawText(text_rect.adjusted(0, used + 4, 0, -tag_height), flags, sub)
        painter.restore()

    def _paint_header(self, painter, option, index):
        rect = option.rect.adjusted(4, 0, -4, 0)
        painter.save()
        font = QFont(option.font)
        font.setBold(True)
        font.setPointSizeF(option.font.pointSizeF() * 1.15)
        painter.setFont(font)
        painter.setPen(option.palette.color(QPalette.Text))
        text = index.data(Qt.DisplayRole) or ""
        painter.drawText(rect.adjusted(0, 0, 0, -4), Qt.AlignLeft | Qt.AlignBottom, text)
        painter.setPen(QPen(QColor("#555555"), 1))
        y = rect.bottom() - 1
        painter.drawLine(rect.left(), y, rect.right(), y)
        painter.restore()


GAME_FILTER_HELP = (
    "Type words to match a game's name, tags, notes or engine.\n"
    "Narrow it down with:\n"
    "  tag:lowpoly      has this tag\n"
    "  engine:unity     engine (unity, source, source2, unreal, or a plugin's id)\n"
    "  version:2019     engine version starts with 2019 (version:2019.4, version:ue ...)\n"
    "  backend:il2cpp   engine details (IL2CPP / Mono, pak v11, mod folders ...)\n"
    "  status:works     community compatibility (works / partial / broken)\n"
    "Combine them, e.g.  tag:fps engine:unity version:2022 mono"
)


def clean_tag(text):
    return " ".join(text.replace("#", " ").split()).strip(" ,")


class TagsDialog(QDialog):
    """Tick existing tags or type new ones for a game."""

    def __init__(self, project, known_tags, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tags - {project['name']}")
        self.resize(340, 420)
        current = set(project.get("tags", []))
        self.list = QListWidget()
        for tag in sorted(set(known_tags) | current, key=str.lower):
            item = QListWidgetItem(tag)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if tag in current else Qt.Unchecked)
            self.list.addItem(item)
        self.new = QLineEdit(placeholderText="New tags, separated by commas")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tags group and filter games on the Projects page\n"
                             "(genre, art style, 'dead game', 'has good models'...)."))
        lay.addWidget(self.list)
        lay.addWidget(self.new)
        lay.addWidget(buttons)
        self.new.setFocus()

    def tags(self):
        tags = [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]
        known = {self.list.item(i).text().lower(): self.list.item(i).text() for i in range(self.list.count())}
        for text in self.new.text().split(","):
            tag = clean_tag(text)
            tag = known.get(tag.lower(), tag)  # reuse the existing spelling
            if tag and tag.lower() not in {t.lower() for t in tags}:
                tags.append(tag)
        return sorted(tags, key=str.lower)


class HomePage(QWidget):
    """Start page: a box per saved game plus 'Add game' / 'Find games' boxes."""

    open_requested = Signal(str)  # project path
    unload_requested = Signal(str)
    _counted = Signal(str, int)
    _detected = Signal(str, object)
    _compat_fetched = Signal(int)
    _games_found = Signal(object)
    ADD, FIND = "__add__", "__find__"
    UNTAGGED = "__untagged__"
    UNKNOWN_ENGINE = "__unknown_engine__"
    GROUPS = (("No grouping", ""), ("Group by tag", "tag"), ("Group by engine", "engine"),
              ("Group by engine version", "version"), ("Group by compatibility", "status"),
              ("Group by loaded / not loaded", "state"))
    SORTS = (("Recently opened", "recent"), ("Name", "name"), ("Engine / version (newest)", "engine"),
             ("Most assets", "assets"))
    HEADER_HEIGHT = 34
    SPACING = 6

    def __init__(self, store, is_loaded, settings, parent=None):
        super().__init__(parent)
        self.store = store
        self.is_loaded = is_loaded
        self.settings = settings
        self.icons = QFileIconProvider()
        self.compat = load_compat()
        self._counting = set()
        self._detecting = set()
        self._headers = []
        self._counted.connect(self._on_counted)
        self._detected.connect(self._on_detected)
        self._compat_fetched.connect(self._on_compat_fetched)
        self._games_found.connect(self._on_games_found)
        self._finding = False
        # Background counts/version checks finish in bursts - redraw once per burst.
        self.refresh_timer = QTimer(self, singleShot=True, interval=150)
        self.refresh_timer.timeout.connect(self.refresh)

        title = QLabel(APP_TITLE)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        hint = QLabel("Pick a game to browse its models and textures.  "
                      "<span style='color:#43a047'>Green</span> = already loaded (opens instantly), "
                      "<span style='color:#e53935'>red</span> = not loaded yet.  "
                      "Drag a game folder here to add it.")
        hint.setStyleSheet("color: gray;")

        # Catalog bar: search, tag filter, grouping and sort order.
        self.search = QLineEdit(placeholderText="Search games...  e.g.  shooter tag:lowpoly engine:unity version:2019")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip(GAME_FILTER_HELP)
        self.search.textChanged.connect(lambda _: self.refresh_timer.start())
        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumWidth(130)
        self.tag_combo.setToolTip("Show only games with this tag (right-click a game → Tags... to tag it)")
        self.tag_combo.currentIndexChanged.connect(lambda _: self.refresh())
        self.engine_combo = QComboBox()
        self.engine_combo.setMinimumWidth(130)
        self.engine_combo.setToolTip("Show only games made with this engine")
        self.engine_combo.currentIndexChanged.connect(lambda _: self._catalog_changed("home_engine",
                                                                                      self.engine_combo))
        self._engine_filter = settings.get("home_engine", "")
        self.group_combo = QComboBox()
        for label, value in self.GROUPS:
            self.group_combo.addItem(label, value)
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(settings.get("home_group", "engine"))))
        self.group_combo.currentIndexChanged.connect(lambda _: self._catalog_changed("home_group",
                                                                                     self.group_combo))
        self.sort_combo = QComboBox()
        for label, value in self.SORTS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(settings.get("home_sort", "recent"))))
        self.sort_combo.currentIndexChanged.connect(lambda _: self._catalog_changed("home_sort",
                                                                                    self.sort_combo))
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: gray;")
        bar = QHBoxLayout()
        bar.addWidget(self.search, 1)
        bar.addWidget(self.engine_combo)
        bar.addWidget(self.tag_combo)
        bar.addWidget(self.group_combo)
        bar.addWidget(QLabel("Sort:"))
        bar.addWidget(self.sort_combo)
        bar.addWidget(self.count_label)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setSpacing(self.SPACING)
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
        lay.addLayout(bar)
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
            if kind == QEvent.Resize:
                self._resize_headers()
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

    def _catalog_changed(self, key, combo):
        self.settings[key] = combo.currentData()
        self.settings.save()
        self.refresh()

    def _update_engine_combo(self):
        """'All engines' + one entry per engine the saved games use, with counts."""
        current = self.engine_combo.currentData() if self.engine_combo.count() else self._engine_filter
        counts = {}
        for project in self.store.projects:
            engine = project.get("engine") or self.UNKNOWN_ENGINE
            counts[engine] = counts.get(engine, 0) + 1
        with QSignalBlocker(self.engine_combo):
            self.engine_combo.clear()
            self.engine_combo.addItem("All engines", "")
            named = [(getattr(engines.get(e), "name", e), e) for e in counts if e != self.UNKNOWN_ENGINE]
            for name, engine in sorted(named, key=lambda x: x[0].lower()):
                self.engine_combo.addItem(f"{name}  ({counts[engine]})", engine)
            if self.UNKNOWN_ENGINE in counts:
                self.engine_combo.addItem(f"Unknown engine  ({counts[self.UNKNOWN_ENGINE]})", self.UNKNOWN_ENGINE)
            self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(current)))

    def show_engine(self, engine_id):
        self.refresh()
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData(engine_id)))

    def _update_tag_combo(self):
        current = self.tag_combo.currentData()
        tags = self.store.all_tags()
        with QSignalBlocker(self.tag_combo):
            self.tag_combo.clear()
            self.tag_combo.addItem("All tags", "")
            for tag in tags:
                n = sum(tag in p.get("tags", []) for p in self.store.projects)
                self.tag_combo.addItem(f"#{tag}  ({n})", tag)
            self.tag_combo.addItem("Untagged", self.UNTAGGED)
            self.tag_combo.setCurrentIndex(max(0, self.tag_combo.findData(current)))

    def _card_item(self, project):
        path = project["path"]
        exe = game_exe(path) if os.path.isdir(path) else None
        icon = (self.icons.icon(QFileInfo(exe)) if exe
                else self.style().standardIcon(QStyle.SP_DirIcon))
        item = QListWidgetItem(icon, project["name"])
        item.setData(Qt.UserRole, path)
        compat = self.compat_entry(path)
        engine = engine_info_text(project)
        tip = [path]
        if not os.path.isdir(path):
            state, lines = "missing", ["Folder missing"]
        else:
            state = "loaded" if self.is_loaded(path) else "unloaded"
            files = project.get("file_count")
            parts = [f"{files:,} files" if files is not None else "counting files..."]
            if project.get("asset_count") is not None:
                parts.append(f"{project['asset_count']:,} assets")
            lines = [" · ".join(parts), "Loaded" if state == "loaded" else "Not loaded",
                     engine or ("detecting engine..." if "engine" not in project else "Unknown engine")]
        if engine:
            tip.append(engine)
        if compat and compat.get("status") in COMPAT_STATUS:
            lines.append(COMPAT_STATUS[compat["status"]])
            if compat.get("notes"):
                tip.append(f"Community notes: {compat['notes']}")
        if project.get("tags"):
            tip.append("Tags: " + ", ".join(project["tags"]))
        if project.get("notes"):
            tip.append(f"Your notes: {project['notes']}")
        if project.get("last_opened"):
            tip.append(f"Last opened: {datetime.fromtimestamp(project['last_opened']):%Y-%m-%d %H:%M}")
        item.setToolTip("\n\n".join(tip))
        item.setData(CardDelegate.STATE_ROLE, state)
        item.setData(CardDelegate.SUB_ROLE, "\n".join(lines))
        item.setData(CardDelegate.PIN_ROLE, bool(project.get("pinned")))
        item.setData(CardDelegate.NOTES_ROLE, bool(project.get("notes")))
        item.setData(CardDelegate.TAGS_ROLE, project.get("tags") or None)
        return item

    def _header_width(self):
        return max(100, self.grid.viewport().width() - 2 * self.SPACING - 2)

    def _header_item(self, text):
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        item.setData(CardDelegate.STATE_ROLE, "header")
        item.setSizeHint(QSize(self._header_width(), self.HEADER_HEIGHT))
        self._headers.append(item)
        return item

    def _resize_headers(self):
        width = self._header_width()
        for item in self._headers:
            if item.sizeHint().width() != width:
                item.setSizeHint(QSize(width, self.HEADER_HEIGHT))

    def _groups_of(self, project, group_by):
        """[(sort key, group title)] a game belongs to (a game with several tags is in several groups)."""
        if group_by == "tag":
            return [((0, t.lower()), f"#{t}") for t in project.get("tags", [])] or [((1,), "Untagged")]
        if group_by == "engine":
            plugin = engines.get(project.get("engine") or "")
            return [((0, plugin.name.lower()), plugin.name)] if plugin else [((1,), "Unknown engine")]
        if group_by == "version":
            title = engine_group(project)
            if title == "Unknown engine":
                return [((1,), title)]
            return [((0, project.get("engine") or "", tuple(-n for n in version_key(title))), title)]
        if group_by == "status":
            status = (self.compat_entry(project["path"]) or {}).get("status")
            order = list(COMPAT_STATUS)
            if status in COMPAT_STATUS:
                return [((order.index(status),), COMPAT_STATUS[status])]
            return [((len(order),), "Not rated yet")]
        if group_by == "state":
            if not os.path.isdir(project["path"]):
                return [((2,), "Folder missing")]
            if self.is_loaded(project["path"]):
                return [((0,), "Loaded")]
            return [((1,), "Not loaded")]
        return [((0,), "")]

    def _matches(self, project, terms):
        """Plain words match anywhere (name, tags, notes, engine...); tag:/engine:/version:/backend:/status: narrow it."""
        compat = self.compat_entry(project["path"]) or {}
        tags = [t.lower() for t in project.get("tags", [])]
        fields = {
            "tag": tags,
            "engine": [(project.get("engine") or "").lower(),
                       (getattr(engines.get(project.get("engine") or ""), "name", "") or "").lower()],
            "version": [(project.get("engine_version") or "").lower()],
            "unity": [(project.get("engine_version") or "").lower()] if project.get("engine") == "unity" else [],
            "backend": [w.strip(",") for w in (project.get("engine_detail") or "").lower().split()],
            "status": [(compat.get("status") or "").lower()],
        }
        haystack = " ".join([project["name"], os.path.basename(os.path.normpath(project["path"])),
                             project.get("notes", ""), engine_info_text(project), compat.get("status", ""),
                             " ".join("#" + t for t in tags)]).lower()
        for term in terms:
            key, sep, value = term.partition(":")
            if sep and key in fields:
                if not any(v.startswith(value) for v in fields[key] if v):
                    return False
            elif term not in haystack:
                return False
        return True

    def refresh(self):
        self.refresh_timer.stop()
        self._update_tag_combo()
        self._update_engine_combo()
        self.grid.clear()
        self._headers = []
        group_by = self.group_combo.currentData()
        tag = self.tag_combo.currentData()
        engine_filter = self.engine_combo.currentData()
        terms = self.search.text().lower().split()
        shown = []
        for project in self.store.sorted(self.sort_combo.currentData()):
            path = project["path"]
            if os.path.isdir(path):
                if "engine" not in project or "engine_version" not in project:
                    self._detect_engine(path, project.get("engine"))
                elif project.get("file_count") is None:
                    self._count_files(path, project.get("engine"))
            if tag == self.UNTAGGED and project.get("tags"):
                continue
            if tag and tag != self.UNTAGGED and tag not in project.get("tags", []):
                continue
            if engine_filter and (project.get("engine") or self.UNKNOWN_ENGINE) != engine_filter:
                continue
            if self._matches(project, terms):
                shown.append(project)

        if group_by:
            groups = {}  # title -> (sort key, [projects])
            for project in shown:
                for order, title in self._groups_of(project, group_by):
                    groups.setdefault(title, (order, []))[1].append(project)
            for title, (_order, projects) in sorted(groups.items(), key=lambda kv: (kv[1][0], kv[0].lower())):
                self.grid.addItem(self._header_item(f"{title}   ({len(projects)})"))
                for project in projects:
                    self.grid.addItem(self._card_item(project))
            self.grid.addItem(self._header_item("Add more"))
        else:
            for project in shown:
                self.grid.addItem(self._card_item(project))
        total = len(self.store.projects)
        self.count_label.setText(f"{len(shown)} of {total} games" if len(shown) != total else f"{total} games")
        self.grid.addItem(self._action_item("＋ Add game", QStyle.SP_FileDialogNewFolder, self.ADD))
        self.grid.addItem(self._action_item("Find games in Steam",
                                            QStyle.SP_FileDialogContentsView, self.FIND))

    def _count_files(self, path, engine_id):
        """Count a game's data files in the background (listing / header sniffing, no loading)."""
        plugin = engines.get(engine_id or "")
        if path in self._counting or plugin is None:
            return
        self._counting.add(path)

        def work():
            try:
                n = plugin.count_files(path)
            except Exception:
                log.exception("Counting files in %s failed", path)
                n = 0
            self._counted.emit(path, n)

        threading.Thread(target=work, daemon=True, name="count-files").start()

    def _on_counted(self, path, n):
        self._counting.discard(path)
        log.info("Counted %d game data files in %s", n, path)
        self.store.set_counts(path, files=n)
        self.refresh_timer.start()

    def _detect_engine(self, path, engine_id=None):
        """Find which engine a game uses (and its version) in the background - file listings/headers only."""
        if path in self._detecting:
            return
        self._detecting.add(path)

        def work():
            try:
                info = detect_project_engine(path, engine_id)
            except Exception:
                log.exception("Detecting the engine of %s failed", path)
                info = {"engine": engine_id or "", "engine_version": "", "engine_detail": ""}
            self._detected.emit(path, info)

        threading.Thread(target=work, daemon=True, name="detect-engine").start()

    def _on_detected(self, path, info):
        self._detecting.discard(path)
        project = self.store.get(path)
        if project is None:
            return
        if project.get("engine_locked") and project.get("engine"):
            info["engine"] = project["engine"]
        self.store.update(path, **info)
        log.info("%s: %s", os.path.basename(os.path.normpath(path)),
                 engine_info_text(project) or "no engine plugin recognizes this game")
        self.refresh_timer.start()

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
            plugin, _score = engines.detect(path)
            if plugin is None:
                names = ", ".join(p.name for p in engines.plugins())
                QMessageBox.warning(self, "Add game", f"None of the engine plugins ({names}) recognize:\n{path}\n\n"
                                    "Pick the game's install folder, or add a plugin for its engine "
                                    "(Help \u2192 Engine plugins).")
                continue
            name = os.path.basename(path)
            if len(paths) == 1:
                name, ok = QInputDialog.getText(self, "Add game", "Name:", text=name)
                if not ok or not name.strip():
                    continue
            self.store.add(name.strip(), path, plugin.id)
            added += 1
        if added:
            self.refresh()

    def find_games(self):
        """Scan the Steam libraries in the background (it reads many folders) and then ask."""
        if self._finding:
            return
        self._finding = True
        log.info("Searching Steam libraries for games the engine plugins can read...")
        self.count_label.setText("Searching Steam libraries...")

        def work():
            try:
                games = find_steam_games()
            except Exception:
                log.exception("Searching the Steam libraries failed")
                games = []
            self._games_found.emit(games)

        threading.Thread(target=work, daemon=True, name="find-games").start()

    def _on_games_found(self, games):
        self._finding = False
        self.refresh()
        log.info("Found %d supported game(s) in Steam libraries", len(games))
        if not games:
            QMessageBox.information(self, "Find games", "No games found in your Steam libraries that the "
                                    "engine plugins recognize.")
            return
        dlg = SteamPickDialog(games, self.store, self)
        if dlg.exec():
            for name, path, engine_id in dlg.chosen():
                self.store.add(name, path, engine_id)
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
        menu.addAction("Tags...", lambda: self.edit_tags(project))
        for tag in project.get("tags", []):
            menu.addAction(f"Show only #{tag}", lambda t=tag: self.show_tag(t))
        if engines.get(project.get("engine") or ""):
            menu.addAction(f"Show only {engines.get(project['engine']).name} games",
                           lambda e=project["engine"]: self.show_engine(e))
        menu.addAction("Rename...", lambda: self.rename(project))
        plugin = engines.get(project.get("engine") or "")
        if plugin is not None and plugin.options:
            menu.addAction(f"Engine settings ({plugin.name})...", lambda: self.edit_engine_options(project, plugin))
        engine_menu = menu.addMenu("Read with engine")
        engine_menu.setToolTipsVisible(True)
        for plugin in engines.plugins():
            act = engine_menu.addAction(plugin.name, lambda p=plugin: self.set_engine(project, p.id))
            act.setCheckable(True)
            act.setChecked(project.get("engine") == plugin.id)
            act.setToolTip(plugin.description)
        engine_menu.addSeparator()
        engine_menu.addAction("Detect automatically", lambda: self.set_engine(project, None))
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

    def edit_tags(self, project):
        dlg = TagsDialog(project, self.store.all_tags(), self)
        if dlg.exec():
            tags = dlg.tags()
            if tags != project.get("tags", []):
                if tags:
                    project["tags"] = tags
                else:
                    project.pop("tags", None)
                self.store.save()
                log.info("Tags for '%s': %s", project["name"], ", ".join(tags) or "(none)")
                self.refresh()

    def show_tag(self, tag):
        self.refresh()  # make sure the tag is in the list
        self.tag_combo.setCurrentIndex(max(0, self.tag_combo.findData(tag)))

    def report_compat(self, project):
        url = compat_report_url(project)
        log.info("Opening a compatibility report for '%s' on GitHub", project["name"])
        webbrowser.open(url)

    def _recount(self, project):
        for key in ("file_count", "engine_version", "engine_detail"):
            project.pop(key, None)
        if not project.get("engine_locked"):
            project.pop("engine", None)
        self.refresh()

    def edit_engine_options(self, project, plugin):
        dlg = EngineOptionsDialog(project, plugin, self)
        if dlg.exec():
            project.setdefault("engine_options", {})[plugin.id] = dlg.values()
            self.store.save()
            log.info("Saved %s settings for '%s'", plugin.name, project["name"])
            if self.is_loaded(project["path"]):
                self.unload_requested.emit(project["path"])  # reload with the new settings next time
            self.refresh()

    def set_engine(self, project, engine_id):
        """Force a game to be read by one engine plugin (None = detect automatically again)."""
        if self.is_loaded(project["path"]):
            self.unload_requested.emit(project["path"])
        for key in ("file_count", "asset_count", "engine_version", "engine_detail", "engine"):
            project.pop(key, None)
        if engine_id:
            project["engine"] = engine_id
            project["engine_locked"] = True
        else:
            project.pop("engine_locked", None)
        self.store.save()
        log.info("'%s' will be read with: %s", project["name"],
                 engines.get(engine_id).name if engine_id else "the detected engine")
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


def project_options(project, plugin):
    """The settings the user saved for this game's engine plugin (Engine settings...)."""
    return dict((project.get("engine_options") or {}).get(plugin.id, {}))


class EngineOptionsDialog(QDialog):
    """Per-game settings an engine plugin asks for (e.g. Unreal AES keys)."""

    def __init__(self, project, plugin, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{plugin.name} settings - {project['name']}")
        self.resize(560, 0)
        current = project_options(project, plugin)
        self.fields = {}
        form = QFormLayout(self)
        for opt in plugin.options:
            if opt.get("multiline"):
                field = QPlainTextEdit(current.get(opt["id"], ""))
                field.setFixedHeight(90)
            else:
                field = QLineEdit(current.get(opt["id"], ""))
            field.setToolTip(opt.get("help", ""))
            self.fields[opt["id"]] = field
            form.addRow(opt.get("label", opt["id"]) + ":", field)
            if opt.get("help"):
                hint = QLabel(opt["help"])
                hint.setWordWrap(True)
                hint.setStyleSheet("color: gray;")
                form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {key: (f.toPlainText() if isinstance(f, QPlainTextEdit) else f.text()).strip()
                for key, f in self.fields.items()}


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

    def __init__(self, log_handler=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)
        self.settings = Settings()
        self.session = None      # GameSession of the game being viewed
        self.current = None      # Asset shown on the right
        self.thread = None
        self.last_dir = self.settings.get("last_dir", os.path.expanduser("~"))
        self.loaded = {}  # norm path -> {"session", "thumbs", "stats", "favorites"}
        self.loading_path = None
        self.favorites = set()   # Asset.uid values
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
        self.type_combo.addItem("All types", "all")
        for kind in KINDS:
            self.type_combo.addItem(KIND_LABELS[kind], kind)
        self.type_combo.addItem("\u2605 Favorites", "fav")
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
        self.tree.setTextElideMode(Qt.ElideLeft)  # long paths: keep the file name visible
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
        panel.open_texture.connect(lambda asset: self.goto_asset(asset.key))
        panel.save_texture.connect(self.export_item)
        panel.save_model.connect(self.export_selected_mesh)
        panel.open_blender.connect(self.open_in_blender)
        self.image_view = ImageView()
        self.image_view.save_requested.connect(self.save_shown_image)
        self.image_view.goto_requested.connect(self.goto_asset)
        self.text_view = QPlainTextEdit(readOnly=True)
        self.anim_view = AnimationView()
        self.anim_view.play_requested.connect(self.play_animation)
        self.audio_view = AudioView(self.settings)
        self.audio_view.save_requested.connect(lambda: self.current and self.export_item(self.current))

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
        for w in (self.placeholder_page, self.mesh_view, self.image_view, self.text_view, self.audio_view,
                  self.anim_view):
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
        self.engine_label = QLabel()
        self.engine_label.setStyleSheet("color: gray; padding: 0 6px;")
        self.statusBar().addPermanentWidget(self.engine_label)

        self.console = None
        if log_handler is not None:
            self.console = ConsoleDock(log_handler, self)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.console)
            self.resizeDocks([self.console], [170], Qt.Vertical)

        self._build_menu()
        if self.settings.get("view") == "grid":
            self.grid_btn.setChecked(True)
        self.statusBar().showMessage("Ready")
        log.info(APP_SHORT + " %s started - engine plugins: %s", __version__,
                 ", ".join(f"{p.name} {p.version}" for p in engines.plugins()) or "none")

    # ---- projects / games
    def current_project(self):
        return self.store.get(self.loading_path) if self.loading_path else None

    def is_loaded(self, path):
        return norm_path(path) in self.loaded

    def unload_game(self, path):
        entry = self.loaded.pop(norm_path(path), None)
        if entry is None:
            return
        entry["session"].close()
        if self.session is entry["session"]:
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
        self.engine_label.clear()

    def focus_search(self):
        search = self.home.search if self.pages.currentWidget() is self.home else self.search
        search.setFocus()
        search.selectAll()

    def open_project(self, path):
        if self.thread is not None and self.thread.isRunning():
            QMessageBox.information(self, "Busy", "Still loading the previous game, try again in a moment.")
            return
        project = self.store.get(path)
        name = project["name"] if project else os.path.basename(path)
        if project is not None and not project.get("engine"):
            self.store.update(path, **detect_project_engine(path))  # only reads listings/headers
        info = engine_info_text(project) if project else ""
        self.setWindowTitle(f"{APP_SHORT} - {name}" + (f"  ({info})" if info else ""))
        self.engine_label.setText(info)
        log.info("Opening game '%s'%s", name, f" ({info})" if info else "")
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
        add(m, "Export all models...", lambda: self.export_all(("model",)))
        add(m, "Export all textures...", lambda: self.export_all(IMAGE_KINDS))
        add(m, "Export everything shown in the list...", self.export_shown)
        m.addSeparator()
        add(m, "Quit", self.close, "Ctrl+Q")

        a = self.menuBar().addMenu("&Asset")
        add(a, "Save selected...", self.export_selected, "Ctrl+S")
        add(a, "Toggle favorite", self.toggle_favorite_selected, "Ctrl+D")
        add(a, "Open model in Blender", self.open_in_blender, "Ctrl+B")
        add(a, "Show UV layout", self.show_uv_layout, "Ctrl+U")
        a.addSeparator()
        add(a, "Find (search box)", self.focus_search, "Ctrl+F")
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
        help_menu.addAction("Engine plugins...", lambda: PluginsDialog(self).exec())
        help_menu.addAction("Install sound decoder (vgmstream)...", self.install_sound_decoder)
        help_menu.addAction("Open plugins folder", open_plugins_folder)
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
        path, _ = QFileDialog.getOpenFileName(self, "Open a game file (.assets, _dir.vpk, .pak ...)")
        if path:
            self.load(path)

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Open game folder")
        if path:
            self.load(path)

    def _reset_viewer(self):
        self.thumbs.clear()
        self.stats_worker.start([])
        self.session = self.current = None
        self.thumb_cache, self.items_by_key, self.grid_items, self.stats = {}, {}, {}, {}
        self.mesh_view.poly = self.mesh_view.texture_img = self.mesh_view.override = None
        self.mesh_view.parts = []
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
        if self.current_project() is None:
            self.engine_label.clear()
        entry = self.loaded.get(norm_path(path))
        if entry is not None:
            log.info("Already loaded - reusing assets in memory for %s", path)
            self.on_loaded(entry["session"])
            return
        project = self.current_project()
        plugin = engines.get(project.get("engine") or "") if project else None
        if plugin is None:
            plugin, _score = engines.detect(path)
        if plugin is None:
            self.placeholder.setText("No engine plugin recognizes this game.")
            QMessageBox.warning(self, "Unknown engine",
                                "None of the engine plugins recognize:\n" + path + "\n\nInstalled: "
                                + ", ".join(p.name for p in engines.plugins())
                                + "\n\nSee Help \u2192 Engine plugins to add one.")
            return
        self.set_progress(0, 0)
        self.thread = QThread()
        options = project_options(project, plugin) if project else {}
        self.loader = Loader(path, plugin, options)
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

    def on_loaded(self, session):
        self.hide_progress()
        key = norm_path(self.loading_path)
        entry = self.loaded.get(key)
        if entry is None or entry["session"] is not session:
            entry = {"session": session, "thumbs": {}, "stats": {}, "favorites": set()}
            session.start_background()
            self.loaded[key] = entry
            if session.warnings:
                QTimer.singleShot(0, lambda: QMessageBox.information(
                    self, "Loaded with notes", "\n\n".join(session.warnings)))
        self.session = session
        self.thumb_cache = entry["thumbs"]
        self.stats = entry["stats"]
        project = self.current_project()
        self.favorites = set(project.get("favorites", [])) if project else entry["favorites"]
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        audio_icon = self.style().standardIcon(QStyle.SP_MediaVolume)

        by_kind = {}
        for asset in session.assets:
            by_kind.setdefault(asset.kind, []).append(asset)
        tops, total, jobs = [], 0, []
        for kind in list(KINDS) + sorted(k for k in by_kind if k not in KINDS):
            items = by_kind.get(kind)
            if not items:
                continue
            label = KIND_LABELS.get(kind, kind)
            top = SortItem([label, "", ""])
            top.setData(0, TYPE_ROLE, kind)
            top.setData(0, SORT_ROLE, f"{KINDS.index(kind) if kind in KINDS else 99:02d}")
            top.setFlags(top.flags() & ~Qt.ItemIsSelectable)
            children = []
            for asset in items:
                child = SortItem([asset.name, "", fmt_size(asset.size)])
                child.setData(0, Qt.UserRole, asset)
                child.setData(0, SORT_ROLE, asset.name.lower())
                child.setData(2, SORT_ROLE, asset.size if asset.size is not None else -1)
                child.setIcon(0, self.thumb_cache.get(asset.key, self.blank) if kind in THUMB_KINDS else
                              audio_icon if kind == "audio" else file_icon)
                if asset.uid in self.favorites:
                    self._mark_favorite(child, True)
                stats = self.stats.get(asset.key)
                if stats is not None:
                    self._apply_stats(child, stats)
                else:
                    jobs.append((asset.key, session, asset))
                self.items_by_key[asset.key] = child
                children.append(child)
            top.addChildren(children)
            tops.append(top)
            total += len(items)
        self.tree.addTopLevelItems(tops)
        self.resort()

        # Measure everything in the background: models first, then textures, sprites, text...
        order = {k: i for i, k in enumerate(KINDS)}
        jobs.sort(key=lambda j: order.get(j[2].kind, 99))
        self.stats_remaining = len(jobs)
        self.stats_worker.start(jobs)

        self.store.set_counts(self.loading_path, files=session.file_count, assets=total)
        if project is not None:
            updates = {}
            if session.engine_version and not project.get("engine_version"):
                updates["engine_version"] = session.engine_version
            if not project.get("engine"):
                updates["engine"] = session.plugin.id
            if updates:
                self.store.update(self.loading_path, **updates)
                self.engine_label.setText(engine_info_text(project))
        self.placeholder.setText("Pick something from the list on the left." if total else
                                 "This game loaded, but the plugin found nothing to show.")
        self.statusBar().showMessage(f"Loaded {total:,} assets from {session.file_count} file(s)")
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
                kind = top.data(0, TYPE_ROLE)
                label = KIND_LABELS.get(kind, kind)
                group_ok = want in ("all", "fav") or want == kind
                shown = 0
                if group_ok:
                    for j in range(top.childCount()):
                        child = top.child(j)
                        asset = child.data(0, Qt.UserRole)
                        ok = filt.matches(asset.name, kind, self.stats.get(asset.key))
                        if ok and want == "fav":
                            ok = asset.uid in self.favorites
                        child.setHidden(not ok)
                        shown += ok
                count = top.childCount()
                top.setText(0, f"{label} ({shown:,})" if shown == count else f"{label} ({shown:,} of {count:,})")
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
            file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
            for _item, asset in rows[:GRID_LIMIT]:
                key = asset.key
                fav = asset.uid in self.favorites
                icon = self.thumb_cache.get(key, self.blank_big) if asset.kind in THUMB_KINDS else file_icon
                it = QListWidgetItem(icon, ("\u2605 " if fav else "") + os.path.basename(asset.name))
                it.setData(Qt.UserRole, asset)
                stats = self.stats.get(key) or {}
                it.setToolTip(f"{asset.name}\n{KIND_LABELS.get(asset.kind, asset.kind)}  {stats.get('info', '')}  "
                              f"{fmt_size(stats.get('size'))}".strip())
                self.grid.addItem(it)
                self.grid_items[key] = it
        if len(rows) > GRID_LIMIT:
            self.statusBar().showMessage(f"Grid shows the first {GRID_LIMIT:,} of {len(rows):,} - "
                                         "use search or the type filter to narrow it down", 6000)
        if self.current:
            current = self.grid_items.get(self.current.key)
            if current is not None:
                with QSignalBlocker(self.grid):
                    self.grid.setCurrentItem(current)
        self.thumb_timer.start()

    def on_grid_current(self, item, _previous):
        data = item.data(Qt.UserRole) if item else None
        if not data:
            return
        tree_item = self.items_by_key.get(data.key)
        if tree_item is not None:
            with QSignalBlocker(self.tree):
                self.tree.setCurrentItem(tree_item)
        self.show_asset(data)

    # ---- thumbnails
    def queue_visible_thumbs(self):
        """Ask for thumbnails of what's on screen (plus a little beyond)."""
        if self.session is None:
            return
        jobs = []
        if self.list_stack.currentWidget() is self.grid:
            vp = self.grid.viewport()
            first = max(self.grid.indexAt(QPoint(8, 8)).row(), 0)
            last = self.grid.indexAt(QPoint(vp.width() - 8, vp.height() - 8)).row()
            if last < 0:
                last = first + 150
            for row in range(first, min(self.grid.count(), last + 40)):
                asset = self.grid.item(row).data(Qt.UserRole)
                if asset and asset.kind in THUMB_KINDS and asset.key not in self.thumb_cache:
                    jobs.append((asset.key, self.session, asset))
        else:
            height = self.tree.viewport().height()
            item = self.tree.itemAt(QPoint(4, 4))
            extra = 0
            while item is not None and extra < 20:
                if self.tree.visualItemRect(item).top() > height:
                    extra += 1
                asset = item.data(0, Qt.UserRole)
                if asset and asset.kind in THUMB_KINDS and asset.key not in self.thumb_cache:
                    jobs.append((asset.key, self.session, asset))
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

    def _request_icons(self, assets, setter):
        """Use cached icons now, request the rest at high priority."""
        missing = []
        for asset in assets:
            if asset.key in self.thumb_cache:
                setter(asset.key, self.thumb_cache[asset.key])
            elif asset.kind in THUMB_KINDS:
                missing.append((asset.key, self.session, asset))
        self.thumbs.request_priority(missing)

    # ---- preview
    def on_select(self):
        items = self.tree.selectedItems()
        data = items[0].data(0, Qt.UserRole) if items else None
        if not data:
            return
        grid_item = self.grid_items.get(data.key)
        if grid_item is not None:
            with QSignalBlocker(self.grid):
                self.grid.setCurrentItem(grid_item)
        self.show_asset(data)

    def show_asset(self, asset):
        self.current = asset
        session = self.session
        self.audio_view.stop()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with session.lock:
                if asset.kind == "model":
                    self.show_mesh(asset)
                elif asset.kind in IMAGE_KINDS:
                    self.show_texture(asset, session.image(asset))
                elif asset.kind == "audio":
                    self.stack.setCurrentWidget(self.audio_view)
                    rows = list(session.describe(asset)) + [("Size", fmt_size(asset.size))]
                    try:
                        data, ext = session.audio(asset)
                        self.audio_view.load(asset.name, data, ext, rows)
                    except Exception as e:
                        if not isinstance(e, NotImplementedError):
                            log.exception("Couldn't decode the sound '%s'", asset.name)
                        self.audio_view.show_error(asset.name, str(e), rows)
                elif asset.kind == "animation":
                    text = session.text(asset)
                    try:
                        targets = session.animation_targets(asset)
                    except Exception:
                        log.exception("Finding models for animation '%s' failed", asset.name)
                        targets = []
                    self.anim_view.show_clip(text, targets)
                    self.stack.setCurrentWidget(self.anim_view)
                elif asset.kind == "text":
                    text = session.text(asset)
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", "replace")
                    self.text_view.setPlainText(text[:MAX_TEXT_CHARS])
                    self.stack.setCurrentWidget(self.text_view)
                else:
                    rows = [f"{label}: {value}" for label, value in session.describe(asset)]
                    self.text_view.setPlainText(
                        f"{asset.name}\n\n" + "\n".join(rows) + f"\nSize: {fmt_size(asset.size)}\n\n"
                        "No preview for this kind of file. Right-click \u2192 Save... exports it as-is.")
                    self.stack.setCurrentWidget(self.text_view)
        except Exception as e:
            log.exception("Could not preview %s '%s'", asset.kind, asset.name)
            self.stack.setCurrentWidget(self.text_view)
            self.text_view.setPlainText(f"Could not preview {asset.name}:\n\n{e}\n\n{traceback.format_exc()}")
        finally:
            QApplication.restoreOverrideCursor()

    def install_sound_decoder(self):
        from engines import extdecode
        existing = extdecode.vgmstream_path()
        text = ("vgmstream is a free, open-source decoder for game audio formats (Wwise .wem/.bnk, FMOD "
                ".bank/.fsb and many more). UniView will download the official Windows build from GitHub "
                f"into:\n{os.path.join(extdecode.tools_dir(), 'vgmstream')}")
        if existing:
            text = f"vgmstream is already installed:\n{existing}\n\nDownload the latest version again?"
        if QMessageBox.question(self, "Install sound decoder", text) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            exe = extdecode.install_vgmstream()
            QMessageBox.information(self, "Install sound decoder", f"Installed:\n{exe}")
        except Exception as e:
            log.exception("Installing vgmstream failed")
            QMessageBox.warning(self, "Install sound decoder", f"Download failed: {e}")
        finally:
            QApplication.restoreOverrideCursor()

    def play_animation(self, model):
        """Show `model` and play the current animation clip on it."""
        clip = self.current
        session = self.session
        if clip is None or clip.kind != "animation" or session is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with session.lock:
                animator = session.animate(model, clip)
                self.show_mesh(model)
            self.mesh_view.play_animation(animator, clip.name)
            log.info("Playing '%s' on '%s' (%d animated bones)", clip.name, model.name,
                     getattr(animator, "matched", 0))
        except Exception as e:
            log.exception("Playing '%s' on '%s' failed", clip.name, model.name)
            QMessageBox.warning(self, "Play animation", str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def _materials(self, asset):
        """Materials of a model, or [] (logged) if the plugin fails."""
        try:
            with self.session.lock:
                return self.session.materials(asset)
        except Exception:
            log.exception("Finding the materials of '%s' failed", asset.name)
            return []

    def show_mesh(self, asset):
        session = self.session
        md = session.mesh(asset)
        poly = meshdata_to_polydata(md)
        materials = []
        if not session.materials_ready():
            # Still indexing in the background: show the bare model now, textures once it's done.
            self.statusBar().showMessage("Finding materials and textures (first time for this game)...")
            self._retry_when_indexed(self.current)
        else:
            materials = self._materials(asset)
        # Group the submeshes by the texture they show: one drawn part per texture.
        groups = {}  # texture key (None = untextured) -> (texture Asset, [triangle arrays])
        if materials and len(md.submeshes) > 1:
            for j, tris in enumerate(md.submeshes):
                mat = material_for(materials, md, j)
                main = mat.main_texture() if mat is not None else None
                key = main.asset.key if main is not None else None
                groups.setdefault(key, (main.asset if main is not None else None, []))[1].append(tris)
        # Big scenes (maps) use hundreds of textures: show them smaller to keep memory in check.
        max_side = 256 if len(groups) > 32 else 1024
        images = {}  # texture asset key -> display image (or None if it failed)

        def image_of(tex_asset):
            if tex_asset is None:
                return None
            if tex_asset.key not in images:
                try:
                    img = session.image(tex_asset)
                    if max(img.size) > max_side:
                        img = img.copy()
                        img.thumbnail((max_side, max_side))
                    images[tex_asset.key] = img
                except Exception as e:
                    log.debug("Texture '%s' failed: %s", tex_asset.name, e)
                    images[tex_asset.key] = None
            return images[tex_asset.key]

        tex = image_of(display_texture(md, materials))
        parts = [(np.concatenate(tris_list) if len(tris_list) > 1 else tris_list[0], image_of(tex_asset))
                 for tex_asset, tris_list in groups.values()]
        self.stack.setCurrentWidget(self.mesh_view)
        self.mesh_view.show_mesh(poly, tex, parts)
        n_tex = sum(len(m.textures) for m in materials)
        log.info("Model '%s': %s verts, %s tris, %d material(s), %d texture(s)%s", asset.name,
                 f"{poly.n_points:,}", f"{poly.n_cells:,}", len(materials), n_tex,
                 "" if tex is not None else " - no texture found")
        if md.skipped:
            log.info("Skipped %d line/point part(s) of '%s'", md.skipped, asset.name)

        channels = self.mesh_view.uv_channels()
        rows = [
            f"<b>Vertices:</b> {poly.n_points:,}",
            f"<b>Triangles:</b> {poly.n_cells:,}",
            f"<b>Submeshes:</b> {len(md.submeshes)}",
            f"<b>UV sets:</b> {', '.join(channels) if channels else 'none'}",
        ]
        try:
            rows += [f"<b>{html_escape(label)}:</b> {html_escape(value)}" for label, value in session.describe(asset)]
        except Exception:
            log.exception("describe() failed for '%s'", asset.name)
        if asset.uid in self.favorites:
            rows.insert(0, "<span style='color:#f4c542'>\u2605 Favorite</span>")
        jobs = self.mesh_view.panel.set_info(asset.name, "<br>".join(rows), materials, self.blank_big)
        self._request_icons(jobs, self.mesh_view.panel.set_icon)

    def _retry_when_indexed(self, data):
        session = self.session

        def check():
            if self.current is not data or self.session is not session:
                return  # user moved on
            if not session.materials_ready():
                QTimer.singleShot(250, check)
                return
            self.statusBar().clearMessage()
            self.show_asset(data)

        QTimer.singleShot(250, check)

    def show_texture(self, asset, img):
        name = asset.name
        log.info("Texture '%s': %dx%d %s", name, img.width, img.height, img.mode)
        self.image_view.show_image(img, ("\u2605 " if asset.uid in self.favorites else "") + name)
        self.stack.setCurrentWidget(self.image_view)
        self.statusBar().showMessage(f"{name}  {img.width}x{img.height}")
        try:
            title, links, empty = self.session.related(asset)
        except Exception:
            log.exception("Finding what's related to %s failed", name)
            title, links, empty = "", [], ""
        entries = [(a.key, os.path.basename(a.name) + ("\n(sprite sheet)" if asset.kind == "sprite" else ""),
                    self.blank_big) for a in links]
        self.image_view.set_links(title, entries, empty)
        self._request_icons(links, self.image_view.set_link_icon)

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
        name = self.current.name if self.current else "model"
        img = render_uv_layout(poly, channel, self.mesh_view.texture_img)
        window = ImageWindow(img, f"UV layout - {name} ({channel})", self)
        window.show()
        self._windows = [w for w in self._windows if w.isVisible()] + [window]

    # ---- favorites
    def _mark_favorite(self, item, fav):
        name = item.data(0, Qt.UserRole).name
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
        make_fav = any(a.uid not in self.favorites for a in datas)
        for asset in datas:
            (self.favorites.add if make_fav else self.favorites.discard)(asset.uid)
            item = self.items_by_key.get(asset.key)
            if item is not None:
                self._mark_favorite(item, make_fav)
            grid_item = self.grid_items.get(asset.key)
            if grid_item is not None:
                grid_item.setText(("\u2605 " if make_fav else "") + os.path.basename(asset.name))
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
        if data.kind in IMAGE_KINDS and self.mesh_view.poly is not None:
            menu.addAction("Apply as texture to current model", lambda: self.apply_texture(data))
        if data.kind == "model":
            menu.addAction("Open in Blender", lambda: self.open_in_blender(data))
        fav = all(d.uid in self.favorites for d in selected)
        menu.addAction(("Remove from favorites" if fav else "Add to favorites") + "   Ctrl+D",
                       lambda: self.toggle_favorites(selected))
        menu.addSeparator()
        menu.addAction("Save...", lambda: self.export_item(data))
        if len(selected) > 1:
            menu.addAction(f"Export {len(selected)} selected...", lambda: self.export_many(selected))
        menu.exec(widget.viewport().mapToGlobal(pos))

    def apply_texture(self, data):
        try:
            with self.session.lock:
                img = self.session.image(data)
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
        if not data or data.kind != "model":
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
        name = data.name
        out_dir = os.path.join(tempfile.gettempdir(), APP_SHORT)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{safe_filename(os.path.basename(name))}.glb")
        try:
            self.write_asset(data, path)
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
        if self.current and self.current.kind == "model":
            self.export_item(self.current)

    def save_shown_image(self):
        img = self.image_view.image
        if img is None:
            return
        name = os.path.basename(self.current.name) if self.current else "texture"
        path, _ = self.ask_save_path(f"{safe_filename(name)}.png", "PNG image (*.png)")
        if path:
            try:
                img.save(path)
                self.statusBar().showMessage(f"Saved {path}")
                log.info("Saved %s", path)
            except Exception as e:
                log.exception("Saving %s failed", path)
                QMessageBox.warning(self, "Save failed", str(e))

    @staticmethod
    def export_ext(asset, model_format="obj"):
        if asset.kind == "model":
            return model_format
        if asset.kind in IMAGE_KINDS:
            return "png"
        if asset.kind == "audio" and asset.ext in ("vsnd_c", ""):
            return "wav"  # decoded on save (the real extension is used if it differs)
        return asset.ext or ("txt" if asset.kind == "text" else "bin")

    def export_item(self, asset):
        if asset.kind == "model":
            filters = "Wavefront OBJ + textures (*.obj);;glTF binary, textures inside (*.glb)"
        elif asset.kind in IMAGE_KINDS:
            filters = "PNG image (*.png)"
        else:
            filters = "All files (*)"
        ext = self.export_ext(asset, self.settings.get("model_format", "obj"))
        base = safe_filename(os.path.splitext(os.path.basename(asset.name))[0]
                             if asset.kind in ("text", "file", "audio") else os.path.basename(asset.name))
        path, chosen = self.ask_save_path(f"{base}.{ext}", filters)
        if not path:
            return
        if asset.kind == "model" and not path.lower().endswith((".obj", ".glb")):
            path += ".glb" if "glb" in chosen else ".obj"
        try:
            written = self.write_asset(asset, path)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(w) for w in written))
            for w in written:
                log.info("Saved %s", w)
        except Exception as e:
            log.exception("Saving %s failed", asset.name)
            QMessageBox.warning(self, "Save failed", str(e))

    def write_asset(self, asset, path):
        """Save one asset to `path` (.obj/.glb for models); returns the list of files written."""
        session = self.session
        with session.lock:
            if asset.kind == "model":
                md = session.mesh(asset)
                materials = self._materials(asset)  # waits for background indexing if needed
                if path.lower().endswith(".glb"):
                    return write_glb(session, md, materials, path)
                return write_obj(session, md, materials, path)
            if asset.kind in IMAGE_KINDS:
                session.image(asset).save(path)
                return [path]
            if asset.kind == "audio":
                try:
                    data, ext = session.audio(asset)
                    path = os.path.splitext(path)[0] + "." + ext
                    with open(path, "wb") as f:
                        f.write(data)
                    return [path]
                except NotImplementedError:
                    pass  # not decodable: save the stored file below
            try:
                data = session.raw(asset)
            except NotImplementedError:
                text = session.text(asset)
                data = text.encode("utf-8") if isinstance(text, str) else text
            with open(path, "wb") as f:
                f.write(data)
            return [path]

    def export_many(self, items):
        self._export_with_dialog(items)

    def export_all(self, kinds):
        if self.session is None:
            return
        items = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                asset = top.child(j).data(0, Qt.UserRole)
                if asset and asset.kind in kinds:
                    items.append(asset)
        self._export_with_dialog(items)

    def export_shown(self):
        self._export_with_dialog([data for _item, data in self.visible_assets()])

    def _export_with_dialog(self, items):
        if not items:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        dlg = ExportDialog(len(items), any(a.kind == "model" for a in items), self.settings, self)
        if dlg.exec():
            folder, model_format, keep = dlg.values()
            self._export_to_folder(items, folder, model_format, keep)

    def _export_to_folder(self, items, folder, model_format="obj", keep_structure=False):
        log.info("Exporting %d item(s) to %s (models as %s%s)", len(items), folder, model_format.upper(),
                 ", keeping the game's folders" if keep_structure else "")
        ok = fail = 0
        used = set()
        self.set_progress(0, len(items))
        for n, asset in enumerate(items, 1):
            name = asset.name
            ext = self.export_ext(asset, model_format)
            sub = export_subfolder(asset) if keep_structure else ""
            target = os.path.normpath(os.path.join(folder, sub))
            stem = os.path.basename(name.replace("\\", "/"))
            if asset.kind in ("text", "file", "audio") and stem.lower().endswith("." + ext.lower()):
                stem = stem[: -len(ext) - 1]
            base = safe_filename(stem)
            fname, i = base, 1
            while (target.lower(), fname.lower()) in used:
                i += 1
                fname = f"{base}_{i}"
            used.add((target.lower(), fname.lower()))
            try:
                os.makedirs(target, exist_ok=True)
                self.write_asset(asset, os.path.join(target, f"{fname}.{ext}"))
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


def open_plugins_folder():
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    os.startfile(PLUGINS_DIR)


class PluginsDialog(QDialog):
    """Help -> Engine plugins: what's installed, what failed to load, how to add one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Engine plugins")
        self.resize(720, 460)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Engine", "Version", "From", "About"])
        tree.setRootIsDecorated(False)
        tree.setWordWrap(True)
        loaded = {p.name: p for p in engines.plugins()}
        for name, origin, ok, message in engines.load_report():
            plugin = loaded.get(name)
            about = plugin.description if (ok and plugin) else f"Failed to load: {message}"
            item = QTreeWidgetItem([name, plugin.version if (ok and plugin) else "-", origin, about])
            item.setToolTip(3, about)
            if not ok:
                item.setForeground(3, QBrush(QColor("#ef5350")))
            tree.addTopLevelItem(item)
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        info = QLabel(
            "Each game is read by an engine plugin. To support another engine (or a game with its own "
            f"formats), put a <code>.py</code> file in the <b>plugins</b> folder next to {APP_SHORT} "
            "and restart. Start from <code>plugins/_template.py</code> and see "
            f"<a href='{REPO_URL}/blob/main/PLUGINS.md'>PLUGINS.md</a>. A plugin with the same id as a "
            "built-in one replaces it. Right-click a game → <i>Read with engine</i> to pick one by hand.")
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        folder = QPushButton("Open plugins folder")
        folder.clicked.connect(open_plugins_folder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.addButton(folder, QDialogButtonBox.ActionRole)
        lay = QVBoxLayout(self)
        lay.addWidget(tree, 1)
        lay.addWidget(info)
        lay.addWidget(buttons)


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
        mods = ("UnityPy", "texture2ddecoder", "etcpak", "astc_encoder", "lz4.block", "brotli")
        for mod in mods:
            importlib.import_module(mod)
        return ", ".join(mods)

    def plugins():
        failed = [f"{name}: {msg}" for name, _origin, ok, msg in engines.load_report() if not ok]
        if failed:
            raise RuntimeError("; ".join(failed))
        return ", ".join(p.id for p in engines.plugins())

    def render():
        plotter = pv.Plotter(off_screen=True, window_size=(64, 64))
        plotter.add_mesh(pv.Sphere())
        shape = plotter.screenshot(return_img=True).shape
        plotter.close()
        return f"offscreen image {shape}"

    def window():
        MainWindow(None).close()

    def game():
        plugin, _score = engines.detect(game_path)
        if plugin is None:
            raise RuntimeError("no engine plugin recognizes this folder")
        result = {}
        loader = Loader(game_path, plugin)
        loader.finished.connect(lambda session: result.update(session=session))
        loader.run()
        if "session" not in result:
            raise RuntimeError("loading failed (see log above)")
        session = result["session"]
        counts = {}
        for kind, decode in (("model", lambda a: meshdata_to_polydata(session.mesh(a))),
                             ("texture", lambda a: session.image(a).load())):
            ok = 0
            items = [a for a in session.assets if a.kind == kind][:25]
            for asset in items:
                try:
                    with session.lock:
                        decode(asset)
                    ok += 1
                except Exception as e:
                    log.warning("self-test: could not decode %s '%s': %s", kind, asset.name, e)
            if items and not ok:
                raise RuntimeError(f"none of the first {len(items)} {kind} assets decoded")
            counts[kind] = f"{ok}/{len(items)}"
        session.close()
        return (f"{plugin.name}: {session.file_count} files, decoded models {counts.get('model')}, "
                f"textures {counts.get('texture')}")

    check("imports", imports)
    check("engine plugins", plugins)
    check("3D rendering", render)
    check("main window", window)
    if game_path:
        check(f"load {game_path}", game)
    log.info("self-test %s", "PASSED" if not failures else f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


def main():
    handler = setup_logging()
    install_crash_handlers()
    engines.load_plugins(PLUGINS_DIR)
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
