"""The 3D model view and its info panel."""

import re
import time

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from pyvistaqt import QtInteractor

from uniview.constants import log
from uniview.util import fmt_distance, wheel_steps


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
            if mat.color is not None:
                swatch = QPixmap(16, 16)
                swatch.fill(QColor.fromRgbF(*[min(1.0, max(0.0, c)) for c in mat.color[:3]]))
                header.setIcon(QIcon(swatch))
            if mat.properties:
                header.setToolTip("\n".join(f"{k}: {v}" for k, v in mat.properties[:60]))
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
        self.fly = QCheckBox("Fly (F)")
        self.fly.setToolTip("Walk/fly through the scene like a game camera:\n"
                            "WASD move, Q/E down/up, drag to look around, Shift faster,\n"
                            "mouse wheel changes speed, double-click a spot to jump there.\n"
                            "F toggles it, Esc leaves it.")
        self.fly.toggled.connect(self.set_fly)
        self.uv_combo = QComboBox()
        self.uv_combo.setToolTip("Which UV set maps the texture.\n"
                                 "UV0 is the normal one; UV1 is often the baked-lighting (lightmap) layout.")
        self.uv_combo.currentTextChanged.connect(self.set_uv_channel)
        uv_layout = QPushButton("UV layout")
        uv_layout.setToolTip("Show the UV triangles drawn over the texture (Ctrl+U).")
        uv_layout.clicked.connect(self.uv_layout_requested)

        self.info = QLabel()

        # Fly camera: keys held, mouse-look drag, speed (units/second), yaw/pitch (degrees)
        self._keys = set()
        self._look = None
        self._fly_speed = 1.0
        self._fly_base = 1.0     # the scene's default speed; the slider goes from 1/100x to 100x of it
        self._yaw = self._pitch = 0.0
        self._fly_last = 0.0
        self._fly_timer = QTimer(self, interval=16)
        self._fly_timer.timeout.connect(self._fly_tick)
        self.fly_hint = QLabel()
        self.fly_hint.setStyleSheet("color: #9ecbff;")
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(0, self.SPEED_STEPS)
        self.speed_slider.setValue(self.SPEED_STEPS // 2)
        self.speed_slider.setFixedWidth(180)
        self.speed_slider.setToolTip("Camera speed (the mouse wheel changes it too while flying)")
        self.speed_slider.valueChanged.connect(self._speed_from_slider)
        self.speed_label = QLabel()
        self.speed_label.setMinimumWidth(70)
        slower, faster = QPushButton("\u2212"), QPushButton("+")
        for btn, step in ((slower, -1), (faster, 1)):
            btn.setFixedWidth(28)
            btn.setToolTip("Slower" if step < 0 else "Faster")
            btn.clicked.connect(lambda _=False, st=step: self.speed_slider.setValue(
                self.speed_slider.value() + st * self.SPEED_STEPS // 20))
        self.fly_bar = QWidget()
        fb = QHBoxLayout(self.fly_bar)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.addWidget(QLabel("Speed:"))
        fb.addWidget(slower)
        fb.addWidget(self.speed_slider)
        fb.addWidget(faster)
        fb.addWidget(self.speed_label)
        fb.addWidget(self.fly_hint, 1)
        self.fly_bar.hide()
        self.plotter.interactor.setFocusPolicy(Qt.StrongFocus)

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
                  zoom_in, zoom_out, reset, self.fly, QLabel("UV:"), self.uv_combo, uv_layout):
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
        lay.addWidget(self.fly_bar)
        lay.addWidget(self.anim_bar)
        lay.addWidget(split, 1)

    def eventFilter(self, obj, event):
        if obj is self.plotter.interactor:
            kind = event.type()
            if kind == QEvent.Wheel:
                if self.fly.isChecked():
                    # One wheel notch = one slider step (x1.25 speed).
                    self.speed_slider.setValue(self.speed_slider.value() + round(wheel_steps(event)))
                else:
                    self.zoom(1.2 ** wheel_steps(event))
                return True
            if kind == QEvent.KeyPress and event.key() == Qt.Key_F and not event.isAutoRepeat() \
                    and not event.modifiers() & ~Qt.KeypadModifier:
                self.fly.toggle()
                return True
            if self.fly.isChecked() and self._fly_event(event):
                return True
        return super().eventFilter(obj, event)

    def zoom(self, factor):
        if self.poly is None or factor <= 0:
            return
        if self.fly.isChecked():
            self._fly_move(self._fly_dir() * self._fly_speed * (0.5 if factor > 1 else -0.5))
            return
        self.plotter.camera.zoom(factor)
        self.plotter.render()

    # ---- fly camera
    FLY_MOVES = {Qt.Key_W: (1, 0, 0), Qt.Key_Up: (1, 0, 0), Qt.Key_S: (-1, 0, 0), Qt.Key_Down: (-1, 0, 0),
                 Qt.Key_D: (0, 1, 0), Qt.Key_Right: (0, 1, 0), Qt.Key_A: (0, -1, 0), Qt.Key_Left: (0, -1, 0),
                 Qt.Key_E: (0, 0, 1), Qt.Key_Space: (0, 0, 1), Qt.Key_Q: (0, 0, -1), Qt.Key_Control: (0, 0, -1)}
    FLY_FOV = 75.0  # degrees; wider than the orbit view, like a game camera
    LOOK_DEGREES_PER_PIXEL = 0.25

    def set_fly(self, on):
        """Game-style camera: move with the keyboard, look around with the mouse, get inside places."""
        with QSignalBlocker(self.fly):
            self.fly.setChecked(bool(on))
        renderer, camera = self.plotter.renderer, self.plotter.camera
        self._keys.clear()
        self._look = None
        if on:
            camera.view_angle = self.FLY_FOV
            # Keep walls drawn right up to the camera (VTK otherwise clips close geometry away).
            renderer.SetNearClippingPlaneTolerance(0.0001)
            self._fly_home()
            self.plotter.interactor.setFocus()
            self.fly_bar.show()
            self._update_fly_hint()
            self._fly_view()
        else:
            self._fly_timer.stop()
            camera.view_angle = 30.0
            renderer.SetNearClippingPlaneTolerance(0.0)
            self.fly_bar.hide()
            renderer.ResetCameraClippingRange()
            self.plotter.render()

    SPEED_STEPS = 40            # slider positions; each one is x1.25, so 1/100x .. 100x around the middle
    SPEED_RATIO = 100.0 ** (2.0 / 40)

    def _speed_from_slider(self, value):
        self._fly_speed = self._fly_base * self.SPEED_RATIO ** (value - self.SPEED_STEPS // 2)
        self._update_fly_hint()

    def _update_fly_hint(self):
        self.speed_label.setText(f"{fmt_distance(self._fly_speed)}/s")
        self.fly_hint.setText("WASD move \u00b7 Q/E down/up \u00b7 drag to look \u00b7 Shift faster \u00b7 "
                              "wheel = speed \u00b7 double-click to jump there "
                              "\u00b7 F / Esc to leave")

    def _fly_home(self):
        """Start over the busy part of the scene: huge ground/water planes or skyboxes would otherwise
        put the camera miles away and make it far too fast. Speed: the area in ~10 seconds."""
        self._sync_fly()
        if self.poly is None or not self.poly.n_points:
            return
        pts = np.asarray(self.poly.points)
        if len(pts) > 200_000:
            pts = pts[np.random.default_rng(0).choice(len(pts), 200_000, replace=False)]
        lo, hi = np.percentile(pts, [2, 98], axis=0)
        size = float(np.linalg.norm(hi - lo)) or float(self.poly.length) or 1.0
        # Keep the user's slider position across scenes; it's relative to each scene's size.
        self._fly_base = max(size * 0.1, 1e-4)
        self._speed_from_slider(self.speed_slider.value())
        camera = self.plotter.camera
        camera.position = tuple((lo + hi) / 2 - self._fly_dir() * size * 0.75)

    def _sync_fly(self):
        """Yaw/pitch from where the camera currently looks (models are Y-up); level the horizon."""
        camera = self.plotter.camera
        d = np.subtract(camera.focal_point, camera.position)
        d = d / max(np.linalg.norm(d), 1e-12)
        self._pitch = float(np.degrees(np.arcsin(np.clip(d[1], -1, 1))))
        self._yaw = float(np.degrees(np.arctan2(d[0], d[2])))

    def _fly_dir(self):
        y, p = np.radians(self._yaw), np.radians(self._pitch)
        return np.array([np.cos(p) * np.sin(y), np.sin(p), np.cos(p) * np.cos(y)])

    def _fly_view(self):
        camera = self.plotter.camera
        pos = np.asarray(camera.position, float)
        camera.focal_point = tuple(pos + self._fly_dir() * max(self._fly_speed, 1e-4))
        camera.up = (0.0, 1.0, 0.0)
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.render()

    def _fly_move(self, delta):
        camera = self.plotter.camera
        camera.position = tuple(np.asarray(camera.position, float) + delta)
        self._fly_view()

    def _fly_event(self, event):
        """Keyboard/mouse while flying; True when handled (so VTK's own controls don't also react)."""
        kind = event.type()
        if kind == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.set_fly(False)
            elif not event.isAutoRepeat():
                self._keys.add(event.key())
                if event.key() in self.FLY_MOVES and not self._fly_timer.isActive():
                    self._fly_last = time.perf_counter()
                    self._fly_timer.start()
            return True  # also swallow VTK's single-key commands (w/s/e/q...)
        if kind == QEvent.KeyRelease:
            if not event.isAutoRepeat():
                self._keys.discard(event.key())
            return True
        if kind == QEvent.FocusOut:
            self._keys.clear()
            return False
        if kind == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            self._fly_jump(event.position())
            return True
        if kind == QEvent.MouseButtonPress:
            self._look = event.position()
            self.plotter.interactor.setFocus()
            return True
        if kind == QEvent.MouseMove and self._look is not None:
            pos = event.position()
            self._yaw -= (pos.x() - self._look.x()) * self.LOOK_DEGREES_PER_PIXEL
            self._pitch = min(89.0, max(-89.0, self._pitch - (pos.y() - self._look.y()) * self.LOOK_DEGREES_PER_PIXEL))
            self._look = pos
            self._fly_view()
            return True
        if kind == QEvent.MouseButtonRelease:
            self._look = None
            return True
        return False

    def _fly_tick(self):
        now = time.perf_counter()
        dt = min(now - self._fly_last, 0.5)  # big scenes can render slowly: keep the real speed
        self._fly_last = now
        moves = [self.FLY_MOVES[k] for k in self._keys if k in self.FLY_MOVES]
        if not moves or self.poly is None:
            self._fly_timer.stop()
            return
        forward = self._fly_dir()
        right = np.cross(forward, (0.0, 1.0, 0.0))
        right = right / max(np.linalg.norm(right), 1e-12)
        f, r, u = np.sum(moves, axis=0)
        step = self._fly_speed * dt * (4.0 if Qt.Key_Shift in self._keys else 1.0)
        delta = (forward * f + right * r + np.array([0.0, 1.0, 0.0]) * u) * step
        if np.any(delta):
            self._fly_move(delta)

    def _fly_jump(self, point):
        """Double-click: fly most of the way to the surface under the mouse, facing it."""
        from vtkmodules.vtkRenderingCore import vtkCellPicker
        widget = self.plotter.interactor
        ratio = widget.devicePixelRatioF()
        picker = vtkCellPicker()
        picker.SetTolerance(0.0005)
        if not picker.Pick(point.x() * ratio, (widget.height() - point.y()) * ratio, 0, self.plotter.renderer):
            return
        target = np.asarray(picker.GetPickPosition(), float)
        camera = self.plotter.camera
        pos = np.asarray(camera.position, float)
        d = target - pos
        dist = np.linalg.norm(d)
        if dist < 1e-9:
            return
        d /= dist
        self._pitch = float(np.degrees(np.arcsin(np.clip(d[1], -1, 1))))
        self._yaw = float(np.degrees(np.arctan2(d[0], d[2])))
        camera.position = tuple(pos + d * dist * 0.8)
        self._fly_view()

    def reset_camera(self):
        self._home_camera()
        self.plotter.render()

    def _home_camera(self):
        """Models are Y-up: look from the front-right, slightly above."""
        self.plotter.view_xy()
        self.plotter.camera.azimuth = 35
        self.plotter.camera.elevation = 20
        self.plotter.reset_camera()
        if self.fly.isChecked():
            self.plotter.camera.view_angle = self.FLY_FOV
            self._fly_home()
            self._fly_view()

    def uv_channels(self):
        if self.poly is None:
            return []
        return sorted(k for k in self.poly.point_data.keys() if re.fullmatch(r"UV\d", k))

    def set_uv_channel(self, name):
        if self.poly is None or name not in self.poly.point_data:
            return
        for mesh in [self.poly] + [part for part, *_ in self.parts]:
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
        for mesh in [self.poly] + [part for part, *_ in self.parts]:
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
        meshes = [(self.poly, pts)] + [(part, pts[used]) for (part, *_), used
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
        """parts: [(faces array (M, 3), texture image or None, color or None)] - each submesh with its own look."""
        self.anim_timer.stop()
        self.animator = None
        self.anim_bar.hide()
        self.poly = poly
        self.texture_img = texture_img
        self.override = None
        self._textures = {}
        self.parts = []
        looks = {(id(img) if img is not None else None, color) for _f, img, color in parts or []}
        if parts and (len(looks) > 1 or any(color is not None and img is None for _f, img, color in parts)):
            self._part_vertices = []
            for tris, img, color in parts:
                # Each part keeps only its own vertices (big maps have hundreds of parts).
                used, local = np.unique(tris, return_inverse=True)
                faces = np.hstack([np.full((len(tris), 1), 3, dtype=np.int64),
                                   local.reshape(-1, 3)]).ravel()
                part = pv.PolyData(poly.points[used], faces)
                for key in poly.point_data.keys():
                    part.point_data[key] = poly.point_data[key][used]
                if poly.active_texture_coordinates is not None:
                    part.active_texture_coordinates = np.asarray(poly.active_texture_coordinates)[used]
                self.parts.append((part, img, color))
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
            for part, img, color in self.parts:
                part_kwargs = dict(kwargs)
                if img is not None:
                    part_kwargs["texture"] = self._texture(img)
                elif color is not None:
                    part_kwargs["color"] = color[:3]
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
