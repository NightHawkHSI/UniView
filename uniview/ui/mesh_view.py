"""The 3D model view and its info panel."""

import re
import time

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pyvistaqt import QtInteractor

from uniview import prefab_info
from uniview.constants import log
from uniview.util import fmt_distance, wheel_steps
from uniview.ui import theme
from uniview.ui.theme import role


class MeshInfoPanel(QWidget):
    """Right-hand panel for a model: stats, source file, materials and their textures."""

    apply_texture = Signal(object)  # texture Asset
    open_texture = Signal(object)   # jump to an asset (texture, or anything a component references) in the list
    save_texture = Signal(object)
    save_model = Signal()
    open_blender = Signal()
    show_code = Signal(object)      # a uniview.code_links link to decompile and show

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel()
        self.title.setWordWrap(True)
        role(self.title, "heading")
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
        role(hint, "muted")

        save_tex = QPushButton("Save selected texture...")
        save_tex.clicked.connect(self._save_selected)
        save_model = role(QPushButton("Save model..."), "primary")
        save_model.setToolTip("OBJ + MTL + PNG textures, or a single GLB with the textures inside.\n"
                              "Both open textured in Blender.")
        save_model.clicked.connect(self.save_model)
        blender = QPushButton("Open in Blender")
        blender.setToolTip("Exports the model as GLB and opens it in Blender (Ctrl+B).")
        blender.clicked.connect(self.open_blender)

        # Objects & scripts (prefabs / scenes): GameObject tree with each object's components and their values.
        self.objects_summary = role(QLabel(), "muted")
        self.objects_filter = QLineEdit(placeholderText="Filter objects and components...")
        self.objects_filter.setClearButtonEnabled(True)
        self.objects_filter.textChanged.connect(self._filter_objects)
        self.objects = QTreeWidget()
        self.objects.setHeaderLabels(["Name", "Value"])
        self.objects.setColumnWidth(0, 200)
        self.objects.setUniformRowHeights(True)
        self.objects.setToolTip("Every object in the prefab with its components (scripts in blue).\n"
                                "Open a component to see its values; double-click a referenced asset to jump to it.")
        self.objects.itemExpanded.connect(self._fill_component)
        self.objects.itemDoubleClicked.connect(self._object_double_clicked)
        self.objects.setContextMenuPolicy(Qt.CustomContextMenu)
        self.objects.customContextMenuRequested.connect(self._objects_menu)
        self._structure = None  # (nodes, by_uid) shown in the objects tree

        materials_page = QWidget()
        ml = QVBoxLayout(materials_page)
        ml.setContentsMargins(0, 6, 0, 0)
        ml.addWidget(self.textures, 1)
        ml.addWidget(hint)
        objects_page = QWidget()
        ol = QVBoxLayout(objects_page)
        ol.setContentsMargins(0, 6, 0, 0)
        ol.addWidget(self.objects_summary)
        ol.addWidget(self.objects_filter)
        ol.addWidget(self.objects, 1)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(materials_page, "Materials")
        self.tabs.addTab(objects_page, "Objects && scripts")
        self.materials_label = role(QLabel("MATERIALS & TEXTURES"), "section")
        self._prefer_objects = True  # open prefabs on the objects tab until the user picks Materials
        self.tabs.tabBarClicked.connect(lambda i: setattr(self, "_prefer_objects", i == 1))
        self.tabs.currentChanged.connect(lambda i: save_tex.setVisible(i == 0))

        # Stats on top, tabs below, with a handle between them (a prefab's stats run long).
        details_scroll = QScrollArea()
        details_scroll.setWidgetResizable(True)
        details_scroll.setFrameShape(QScrollArea.NoFrame)
        details_scroll.setWidget(self.details)
        details_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.materials_label)
        bl.addWidget(self.tabs, 1)
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(details_scroll)
        self.split.addWidget(bottom)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([170, 500])

        self.setObjectName("sidePanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 12, 12)
        lay.setSpacing(6)
        lay.addWidget(self.title)
        lay.addWidget(self.split, 1)
        lay.addWidget(save_tex)
        lay.addWidget(save_model)
        lay.addWidget(blender)
        self._items = {}  # thumb key -> [QListWidgetItem]
        self.set_structure(None)

    # ---- objects & scripts
    def set_structure(self, nodes, by_uid=None, game_entries=None):
        """Show a prefab/scene's GameObjects and components (hierarchy() nodes), or hide the tab (None).
        game_entries: game data that names the prefab ([{"title", "rows": [(field, value)], "links"}],
        see uniview.game_data / code_links), shown as a "Game data" group on top."""
        has = bool(nodes)
        self.tabs.setTabVisible(1, has)
        self.tabs.tabBar().setVisible(has)
        self.materials_label.setVisible(not has)
        self.objects.clear()
        self.objects_filter.clear()
        self._structure = (nodes, by_uid or {}) if has else None
        if not has:
            self.tabs.setCurrentIndex(0)
            return
        visible = prefab_info.visible_flags(nodes)
        scripts = prefab_info.scripts(nodes)
        data = f" · {len(game_entries)} game data entr{'y' if len(game_entries) == 1 else 'ies'}" \
            if game_entries else ""
        self.objects_summary.setText(f"{len(nodes):,} object(s) · {len(scripts)} script(s){data}"
                                     + ("  (hover for the list)" if scripts else ""))
        self.objects_summary.setToolTip("Scripts used:\n" + "\n".join(scripts) if scripts else "")
        bold = self.objects.font()
        bold.setBold(True)
        muted, accent = QColor(theme.MUTED), QColor(theme.ACCENT_HOVER)
        items = []
        for i, n in enumerate(nodes):
            names = prefab_info.component_names(n)
            item = QTreeWidgetItem([n["name"], ", ".join(names)])
            item.setFont(0, bold)
            item.setForeground(1, muted)
            state = prefab_info.node_state(nodes, i, visible)
            p, s = n.get("pos") or (0, 0, 0), n.get("scale") or (1, 1, 1)
            item.setToolTip(0, "\n".join([n["name"]] + state + [f"position ({p[0]:g}, {p[1]:g}, {p[2]:g})",
                                                              f"scale ({s[0]:g}, {s[1]:g}, {s[2]:g})"]))
            if not visible[i]:
                item.setForeground(0, muted)
            for comp in prefab_info.components(n, nodes, by_uid or {}):
                child = QTreeWidgetItem([comp["title"].rsplit(".", 1)[-1], "script" if comp["script"] else ""])
                child.setToolTip(0, comp["title"])
                child.setForeground(1, muted)
                if comp["script"]:
                    child.setForeground(0, accent)
                child.setData(0, Qt.UserRole + 1, comp["rows"])  # filled in when opened
                if comp["rows"]:
                    child.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                item.addChild(child)
            parent = n.get("parent", -1)
            if 0 <= parent < len(items):
                items[parent].addChild(item)
            else:
                self.objects.addTopLevelItem(item)
            items.append(item)
        for item in items[:1]:
            item.setExpanded(True)
        if game_entries:
            self._add_game_data(game_entries, bold, muted, accent)
        if self._prefer_objects:
            self.tabs.setCurrentIndex(1)

    def _add_game_data(self, entries, bold, muted, accent):
        group = QTreeWidgetItem(["Game data", f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} name this"])
        group.setFont(0, bold)
        group.setForeground(1, muted)
        group.setToolTip(0, "Entries in the game's data tables (JSON text assets) that name this prefab.\n"
                            "Data-driven games keep an item's gameplay values here instead of on the prefab.")
        for entry in entries:
            item = QTreeWidgetItem([entry["title"], f"{len(entry['rows'])} field(s)"])
            item.setToolTip(0, entry["title"])
            item.setForeground(1, muted)
            for link in entry.get("links") or []:
                code = QTreeWidgetItem(["C# code", f"{link['title']}  (double-click)"])
                code.setForeground(0, accent)
                code.setForeground(1, accent)
                code.setData(0, Qt.UserRole + 2, link)
                code.setToolTip(0, "Decompile the game function that runs this:\n" + "\n".join(
                    f"{t.rsplit('.', 1)[-1]}.{m}" for t in link["types"] for m in link["methods"]))
                item.addChild(code)
            item.setData(0, Qt.UserRole + 1, [(field, value, None) for field, value in entry["rows"]])
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            group.addChild(item)
        self.objects.insertTopLevelItem(0, group)
        group.setExpanded(True)
        # Open the entries that link to code (else the first one) so the main facts show straight away.
        linked = [i for i, e in enumerate(entries) if e.get("links")] or [0]
        for i in linked:
            group.child(i).setExpanded(True)

    def _fill_component(self, item):
        rows = item.data(0, Qt.UserRole + 1)
        if rows is None:
            return
        item.setData(0, Qt.UserRole + 1, None)
        link = QColor(theme.ACCENT_HOVER)
        for field, value, asset in rows:
            row = QTreeWidgetItem([field, value])
            row.setToolTip(0, field)
            row.setToolTip(1, value)
            if asset is not None:
                row.setData(0, Qt.UserRole, asset)
                row.setForeground(1, link)
            item.addChild(row)

    def _filter_objects(self, text):
        terms = text.lower().split()

        def walk(item):
            own = all(t in (item.text(0) + " " + item.toolTip(0)).lower() for t in terms)
            shown = False
            for i in range(item.childCount()):
                shown |= walk(item.child(i))
            visible = not terms or own or shown
            item.setHidden(not visible)
            if terms and shown:
                item.setExpanded(True)
            return visible

        for i in range(self.objects.topLevelItemCount()):
            walk(self.objects.topLevelItem(i))

    def _object_double_clicked(self, item, _column):
        if item.data(0, Qt.UserRole + 2):
            self.show_code.emit(item.data(0, Qt.UserRole + 2))
        elif item.data(0, Qt.UserRole):
            self.open_texture.emit(item.data(0, Qt.UserRole))

    def _objects_menu(self, pos):
        item = self.objects.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        asset = item.data(0, Qt.UserRole)
        if asset:
            menu.addAction("Go to asset in list", lambda: self.open_texture.emit(asset))
        menu.addAction("Copy", lambda: QApplication.clipboard().setText(
            f"{item.text(0)} = {item.text(1)}" if item.text(1) else item.text(0)))
        menu.addAction("Expand all", self.objects.expandAll)
        menu.addAction("Collapse all", self.objects.collapseAll)
        menu.exec(self.objects.viewport().mapToGlobal(pos))

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

def tool_button(icon_name, tip, checkable=True, checked=False):
    """Icon-only toolbar button (theme line icon, accent-coloured while checked)."""
    btn = QToolButton(checkable=checkable, checked=checked, toolTip=tip)
    role(btn, "icon")
    btn.setIconSize(QSize(18, 18))
    btn.setCursor(Qt.PointingHandCursor)
    return theme.set_icon(btn, icon_name)


class MeshView(QWidget):
    uv_layout_requested = Signal()

    def _set_background(self):
        self.plotter.set_background(theme.VIEWPORT_BOTTOM, top=theme.VIEWPORT_TOP)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plotter = QtInteractor(self)
        self._set_background()
        theme.on_change(self._set_background, self)
        # Handle the mouse wheel ourselves so zoom works regardless of focus/touchpad.
        self.plotter.interactor.installEventFilter(self)
        self.poly = None
        self.texture_img = None
        self.override = None     # texture the user put on the whole model
        self.parts = []          # [(PolyData of one submesh, its texture or None)]
        self._textures = {}      # (id(img), alpha, flip) -> pv.Texture
        self.flat = False        # 2D content: straight-on view without perspective
        self.gizmos = {}         # kind -> (points, segments) line overlays

        self.wire = tool_button("wireframe", "Wireframe")
        self.edges = tool_button("solid", "Edges: draw the triangle edges over the model")
        self.use_tex = tool_button("texture", "Texture", checked=True)
        self.flip_v = tool_button("flip", "Flip texture V (for upside-down textures)")
        self.tex_alpha = tool_button("alpha", "Texture alpha: use the texture's alpha channel as transparency.\n"
                                     "Off by default: many games store other data there.")
        self.use_colors = tool_button("colors", "Vertex colors", checked=True)
        self.show_gizmos = tool_button("gizmo", "Gizmos: draw colliders (green, triggers yellow), light ranges "
                                       "(orange),\ncamera views (white) and sound sources (cyan) as wire shapes.")
        self.show_gizmos.setEnabled(False)
        for cb in (self.wire, self.edges, self.use_tex, self.flip_v, self.tex_alpha, self.use_colors,
                   self.show_gizmos):
            cb.toggled.connect(self.redraw)
        zoom_in = tool_button("zoom_in", "Zoom in (or use the mouse wheel)", checkable=False)
        zoom_out = tool_button("zoom_out", "Zoom out (or use the mouse wheel)", checkable=False)
        for btn, factor in ((zoom_in, 1.25), (zoom_out, 0.8)):
            btn.clicked.connect(lambda _=False, f=factor: self.zoom(f))
        reset = tool_button("reset", "Reset camera", checkable=False)
        reset.clicked.connect(self.reset_camera)
        self.fly = tool_button("fly", "Fly (F): walk/fly through the scene like a game camera.\n"
                               "WASD move, Q/E down/up, drag to look around, Shift faster,\n"
                               "mouse wheel changes speed, double-click a spot to jump there.\n"
                               "F toggles it, Esc leaves it.")
        self.fly.toggled.connect(self.set_fly)
        self.uv_combo = QComboBox()
        self.uv_combo.setToolTip("Which UV set maps the texture.\n"
                                 "UV0 is the normal one; UV1 is often the baked-lighting (lightmap) layout.")
        self.uv_combo.currentTextChanged.connect(self.set_uv_channel)
        uv_layout = tool_button("uv", "UV layout: show the UV triangles drawn over the texture (Ctrl+U).",
                                checkable=False)
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
        role(self.fly_hint, "accent")
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
        role(self.info, "muted")
        toolbar = QWidget(objectName="viewerBar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(8, 6, 8, 6)
        bar.setSpacing(4)
        groups = ((self.use_tex, self.flip_v, self.tex_alpha, self.use_colors),
                  (self.wire, self.edges, self.show_gizmos),
                  (zoom_in, zoom_out, reset, self.fly),
                  (role(QLabel("UV"), "muted"), self.uv_combo, uv_layout))
        for i, group in enumerate(groups):
            if i:
                bar.addSpacing(8)
                sep = QWidget(objectName="vsep")
                sep.setAttribute(Qt.WA_StyledBackground, True)
                sep.setFixedSize(1, 18)
                bar.addWidget(sep)
                bar.addSpacing(8)
            for w in group:
                bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.info)

        self.panel = MeshInfoPanel()
        split = QSplitter()
        split.addWidget(self.plotter.interactor)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setSizes([760, 360])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(toolbar)
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
            self.plotter.disable_parallel_projection()
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
        """Models are Y-up: look from the front-right, slightly above. 2D content: straight on, from where a
        Unity 2D camera looks (-Z; x is mirrored in UniView's space, so this keeps the game's left/right)."""
        if self.flat and not self.fly.isChecked():
            self.plotter.view_xy(negative=True)
            self.plotter.enable_parallel_projection()
            self.plotter.reset_camera()
            if self.poly is not None and self.poly.n_points > 8:
                # Frame the busy part in x/y only: depth (sprite layers can be hundreds of units apart) and
                # far-off sprites parked off screen would otherwise shrink everything to a dot.
                pts = np.asarray(self.poly.points)
                lo, hi = np.percentile(pts, [2, 98], axis=0)
                camera = self.plotter.camera
                center = (lo + hi) / 2
                camera.focal_point = (center[0], center[1], pts[:, 2].mean())
                camera.position = (center[0], center[1], pts[:, 2].min() - max(hi[0] - lo[0], hi[1] - lo[1], 1.0))
                aspect = max(self.plotter.interactor.width(), 1) / max(self.plotter.interactor.height(), 1)
                camera.parallel_scale = max((hi[1] - lo[1]) / 2, (hi[0] - lo[0]) / 2 / aspect, 1e-3) * 1.05
                self.plotter.renderer.ResetCameraClippingRange()
            return
        self.plotter.disable_parallel_projection()
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

    def show_mesh(self, poly, texture_img, parts=None, flat=False, gizmos=None, gizmos_only=False):
        """parts: [(faces array (M, 3), texture image or None, color or None)] - each submesh with its own look.
        flat: 2D content (sprites facing -Z) - looked at straight on, without perspective."""
        self.flat = flat
        self.gizmos = gizmos or {}
        self.show_gizmos.setEnabled(bool(self.gizmos))
        if gizmos_only:
            with QSignalBlocker(self.show_gizmos):
                self.show_gizmos.setChecked(True)  # nothing else to see
        if flat and self.fly.isChecked():
            self.set_fly(False)
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

    GIZMO_COLORS = {"collider": "#3ddc84", "trigger": "#e8d44d", "light": "#ff9f43", "camera": "#ffffff",
                    "audio": "#4dd0e1"}

    def _draw_gizmos(self):
        if not (self.show_gizmos.isChecked() and self.gizmos):
            return
        for kind, (pts, segs) in self.gizmos.items():
            if not len(segs):
                continue
            lines = np.hstack([np.full((len(segs), 1), 2, np.int64), segs]).ravel()
            self.plotter.add_mesh(pv.PolyData(np.asarray(pts, np.float32), lines=lines),
                                  color=self.GIZMO_COLORS.get(kind, "#ff00ff"), line_width=2, lighting=False)

    def redraw(self, *_, reset_camera=False):
        self.plotter.clear_actors()  # clear() would also drop the lights
        if not self.plotter.renderer.lights:
            self.plotter.enable_lightkit()
        if self.poly is None:
            return
        self._draw_gizmos()
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
