"""Background workers: game loading, list thumbnails and asset stats."""

import threading
import time
import traceback

import numpy as np
import pyvista as pv
from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from engines.sdk import KIND_LABELS, Progress
from uniview.constants import log
from uniview.jobs import JobQueue
from uniview.util import pil_to_qimage


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
            self.progress_value.emit(0, 0)  # busy until the plugin reports numbers
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

class ThumbnailWorker(QObject):
    """Background thread that makes thumbnails. Panel jobs go before list jobs."""

    ready = Signal(object, QImage)  # key, image (null image = no preview)

    def __init__(self, size):
        super().__init__()
        self.size = size
        self._jobs = JobQueue()
        threading.Thread(target=self._run, daemon=True, name="thumbnails").start()

    def request_visible(self, jobs):
        """Replace the list jobs (only what's on screen matters). A job is (key, session, asset)."""
        self._jobs.replace(jobs)

    def request_priority(self, jobs):
        self._jobs.put_urgent(jobs)

    def clear(self):
        self._jobs.clear()

    def _run(self):
        while True:
            _gen, [(key, session, asset)] = self._jobs.take()
            try:
                qimg = pil_to_qimage(make_thumbnail(session, asset, self.size))
            except Exception as e:
                log.debug("No thumbnail for %s '%s': %s", asset.kind, asset.name, e)
                qimg = QImage()
            try:
                self.ready.emit(key, qimg)
            except RuntimeError:
                return  # window closed


class StatsWorker(QObject):
    """Background thread that measures every asset (tris, pixel size, bytes) for sort/filter."""

    ready = Signal(object, int)  # [(key, stats)], jobs remaining

    def __init__(self):
        super().__init__()
        self._jobs = JobQueue()
        threading.Thread(target=self._run, daemon=True, name="asset-stats").start()

    def start(self, jobs):
        """Measure these (key, session, asset) jobs instead of any still waiting; [] just cancels."""
        self._jobs.replace(jobs, cancel=True)

    def _run(self):
        while True:
            gen, batch_jobs = self._jobs.take(40)
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
                time.sleep(0)  # hand the GIL to the window thread if it's waiting (keeps the UI smooth)
            remaining = self._jobs.pending_if_current(gen)
            if remaining is None:
                continue  # a different game was opened meanwhile
            try:
                self.ready.emit(batch, remaining)
            except RuntimeError:
                return  # window closed
