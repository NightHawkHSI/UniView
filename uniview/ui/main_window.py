"""The main window: asset list, previews, menus, export."""

import os
import subprocess
import tempfile
import time
import traceback
import webbrowser

from PIL import Image
from PySide6.QtCore import QPoint, QSignalBlocker, QSize, Qt, QThread, QTimer
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import engines
from engines.sdk import IMAGE_KINDS, KIND_LABELS, KINDS, MODEL_KINDS, VIEW_OPTIONS
from uniview import __version__, code_links, game_data, modmaker, prefab_info
from uniview.constants import (
    APP_DIR,
    APP_SHORT,
    APP_TITLE,
    CRASH_FILE,
    GRID_LIMIT,
    GRID_THUMB,
    ICON_FILE,
    LOG_FILE,
    MAX_TEXT_CHARS,
    REPO_URL,
    SORT_ROLE,
    THUMB_CACHE_MAX,
    THUMB_SIZE,
    TYPE_ROLE,
    VIEW_OPTION_ITEMS,
    log,
)
from uniview.export import (
    display_texture,
    export_ext,
    plan_export,
    render_uv_layout,
    session_materials,
    write_animated_glb,
    write_asset,
)
from uniview.model_display import is_place, model_info_rows, texture_groups, texture_loader
from uniview.projects import ProjectStore, detect_project_engine, engine_info_text, project_options
from uniview.search import FILTER_HELP, AssetFilter, is_unreadable
from uniview.textdecode import inspect_bytes
from uniview.settings import BLENDER_IMPORT, Settings, find_blender
from uniview.tools import missing_recommended
from uniview.ui.asset_tree import AssetTree
from uniview.ui.console import ConsoleDock
from uniview.ui.dialogs import ExportDialog, PluginsDialog, edit_project_notes, open_plugins_folder
from uniview.ui.home import HomePage
from uniview.ui.media import AnimationView, AudioView, ImageView, ImageWindow, VideoView
from uniview.ui import canvas_render
from uniview.ui.mesh_view import MeshView
from uniview.ui.prefab_view import PrefabView
from uniview.ui.decompile_dialog import decompile_game_code
from uniview.ui.duplicates_dialog import DuplicatesDialog
from uniview.ui.modmaker_dialog import ModMakerDialog, replace_asset
from uniview.ui.search_dialog import ContentSearchDialog
from uniview.ui.versions_dialog import VersionsDialog
from uniview.ui.ui_prefab import has_thumbnail, ui_picture, uid_map
from uniview.ui.tools_dialog import ToolsDialog
from uniview.ui.unity_export import ask_project_folder, choose_editor_version, run_export
from uniview.unity_project import installed_editors, target_version, unity_version
from uniview.util import blank_icon, fmt_size, norm_path, open_path, pil_to_pixmap, safe_filename
from uniview.workers import Loader, StatsWorker, ThumbnailWorker, meshdata_to_polydata
from uniview.ui import theme
from uniview.ui.theme import role


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

class MainWindow(QMainWindow):

    def __init__(self, log_handler=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)
        self.settings = Settings.load()
        theme.apply(QApplication.instance(), self.settings.theme)
        self.session = None     # GameSession of the game being viewed
        self.current = None      # Asset shown on the right
        self.thread = None
        self.last_dir = self.settings.last_dir
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
        self._pending_unity_export = None  # (game path, project folder) waiting for the game to load
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
        back = theme.set_icon(QPushButton("All games"), "back")
        back.clicked.connect(self.show_home)
        self.notes_btn = QPushButton("Notes")
        self.notes_btn.setToolTip("Your notes for this game")
        self.notes_btn.clicked.connect(self.edit_current_notes)
        back.setFlat(True)
        back.setStyleSheet("text-align: left;")
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
        self.list_btn = theme.set_icon(QToolButton(text="List", checkable=True, checked=True), "list")
        self.grid_btn = theme.set_icon(QToolButton(text="Grid", checkable=True), "grid")
        for btn in (self.list_btn, self.grid_btn):
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
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

        self.tree = AssetTree()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Info", "Size"])
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        # Info / Size just wide enough for their usual values: the name gets the rest.
        fm = self.tree.fontMetrics()
        header.resizeSection(1, fm.horizontalAdvance("12,345 tris") + 14)
        header.resizeSection(2, fm.horizontalAdvance("999.9 KB") + 14)
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

        left = QWidget(objectName="sidePanel")
        left.setAttribute(Qt.WA_StyledBackground, True)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 8, 6, 8)
        ll.setSpacing(6)
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
        panel.show_code.connect(self.show_code)
        self.image_view = ImageView()
        self.image_view.save_requested.connect(self.save_shown_image)
        self.image_view.goto_requested.connect(self.goto_asset)
        self.text_view = QPlainTextEdit(readOnly=True)
        self._content_search = None  # Search inside files window (made on first use)
        self._duplicates = None      # Duplicate assets window (made on first use)
        self._versions = None        # Game versions window (made on first use)
        self._modmaker = None        # Mod Maker window (made on first use)
        self.hidden_copies = set()   # uids the duplicate finder hides from the list
        self.prefab_view = PrefabView()
        self.prefab_view.goto_requested.connect(self.goto_asset)
        self.prefab_view.save_requested.connect(lambda: self.save_shown_image(self.prefab_view.picture))
        self.anim_view = AnimationView()
        self.anim_view.play_requested.connect(self.play_animation)
        self.anim_view.export_requested.connect(self.export_animated)
        self.anim_view.flipbook_requested.connect(self.play_flipbook)
        self.flipbook_timer = QTimer(self, interval=15)
        self.flipbook_timer.timeout.connect(self._flipbook_tick)
        self._flipbook = None
        self.audio_view = AudioView(self.settings)
        self.video_view = VideoView(self.settings)
        self.video_view.save_requested.connect(lambda: self.current and self.export_item(self.current))
        self.audio_view.save_requested.connect(lambda: self.current and self.export_item(self.current))

        self.placeholder = role(QLabel("Loading...", alignment=Qt.AlignCenter), "muted")
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
                  self.anim_view, self.video_view, self.prefab_view):
            self.stack.addWidget(w)

        self.viewer = QSplitter()
        self.viewer.addWidget(left)
        self.viewer.addWidget(self.stack)
        self.viewer.setSizes([430, 1070])

        self.store = ProjectStore()
        self.home = HomePage(self.store, self.is_loaded, self.settings)
        self.home.open_requested.connect(self.open_project)
        self.home.unload_requested.connect(self.unload_game)
        self.home.export_unity_requested.connect(self.export_unity_project)
        self.home.decompile_requested.connect(self.decompile_code)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.home)
        self.pages.addWidget(self.viewer)
        self.setCentralWidget(self.pages)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.engine_label = QLabel()
        self.engine_label.setContentsMargins(6, 0, 6, 0)
        self.statusBar().addPermanentWidget(self.engine_label)

        self.console = None
        if log_handler is not None:
            self.console = ConsoleDock(log_handler, self)
            self.addDockWidget(Qt.BottomDockWidgetArea, self.console)
            self.resizeDocks([self.console], [170], Qt.Vertical)

        for key in VIEW_OPTIONS:
            VIEW_OPTIONS[key] = self.settings.option(key)
        self._build_menu()
        if self.settings.view == "grid":
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

    def decompile_code(self, path=None):
        """File -> Decompile game code... (the open game), or a Unity game's right-click menu on Projects."""
        path = path or (self.session.path if self.session is not None else None)
        if not path:
            QMessageBox.information(self, "Decompile game code", "Open a game first (or right-click one on the "
                                    "Projects page).")
            return
        project = self.store.get(path)
        name = project["name"] if project else os.path.basename(os.path.normpath(path))
        decompile_game_code(self, path if os.path.isdir(path) else os.path.dirname(path), name)

    def export_unity_project(self, path):
        """Projects page -> right-click a Unity game -> Export as Unity project..."""
        project = self.store.get(path)
        name = project["name"] if project else os.path.basename(path)
        root = ask_project_folder(self, name, self.last_dir)
        if root is None:
            return
        self.last_dir = os.path.dirname(root)
        if norm_path(path) in self.loaded:
            self._run_unity_export(path, root)
            return
        self._pending_unity_export = (path, root)  # runs when the game has loaded
        self.open_project(path)

    def _run_unity_export(self, path, root):
        entry = self.loaded.get(norm_path(path))
        if entry is None:
            return
        session = entry["session"]
        project = self.store.get(path) or {}
        game_version = unity_version(project.get("engine_version"), session.engine_version)
        editors = installed_editors()
        version = target_version(game_version, editors)
        if game_version and version != game_version:
            version = choose_editor_version(self, game_version, version)
            if not version:
                return
        run_export(self, session, root, version, editors.get(version), game_version)

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
        add(m, "Game versions (snapshots / compare)...", self.show_versions)
        add(m, "Decompile game code (C#)...", self.decompile_code)
        m.addSeparator()
        add(m, "Quit", self.close, "Ctrl+Q")

        a = self.menuBar().addMenu("&Asset")
        add(a, "Save selected...", self.export_selected, "Ctrl+S")
        add(a, "Toggle favorite", self.toggle_favorite_selected, "Ctrl+D")
        add(a, "Open model in Blender", self.open_in_blender, "Ctrl+B")
        add(a, "Show UV layout", self.show_uv_layout, "Ctrl+U")
        add(a, "Show raw bytes", lambda: self.show_raw(), "Ctrl+R")
        a.addSeparator()
        add(a, "Find (search box)", self.focus_search, "Ctrl+F")
        add(a, "Search inside files...", self.show_content_search, "Ctrl+Shift+F")
        add(a, "Find duplicate assets...", self.show_duplicates)
        add(a, "Set Blender location...", self.choose_blender)

        mod = self.menuBar().addMenu("&Mod")
        add(mod, "Mod Maker...", self.show_modmaker, "Ctrl+M")
        add(mod, "Replace selected asset with file...", self.replace_selected)

        view = self.menuBar().addMenu("&View")
        add(view, "List view", lambda: self.list_btn.setChecked(True), "Ctrl+1")
        add(view, "Grid view", lambda: self.grid_btn.setChecked(True), "Ctrl+2")
        add(view, "Collapse all groups", self.tree.collapse_all_groups, "Ctrl+Shift+C")
        if self.console is not None:
            toggle = self.console.toggleViewAction()
            toggle.setShortcut("Ctrl+`")
            view.addAction(toggle)
        view.addSeparator()
        looks = view.addMenu("Theme")
        group = QActionGroup(self)
        for mode, text in (("dark", "Dark"), ("light", "Light"), ("system", "Match Windows")):
            act = QAction(text, self, checkable=True)
            act.setChecked(self.settings.theme == mode)
            act.triggered.connect(lambda _=False, m=mode: self.set_theme(m))
            group.addAction(act)
            looks.addAction(act)

        options = self.menuBar().addMenu("&Options")
        for key, text, tip in VIEW_OPTION_ITEMS:
            act = QAction(text, self, checkable=True)
            act.setChecked(self.settings.option(key))
            act.setToolTip(tip)
            act.setStatusTip(tip)
            act.toggled.connect(lambda on, key=key: self._set_view_option(key, on))
            options.addAction(act)
        options.setToolTipsVisible(True)

        help_menu = self.menuBar().addMenu("&Help")
        online = QAction("Download community compatibility list", self, checkable=True)
        online.setChecked(self.settings.online_compat)
        online.setToolTip(f"Fetches compat.json from {REPO_URL} at startup")
        online.toggled.connect(self._set_online_compat)
        help_menu.addAction(online)
        help_menu.addAction("Project page on GitHub", lambda: webbrowser.open(REPO_URL))
        help_menu.addSeparator()
        help_menu.addAction("Engine plugins...", lambda: PluginsDialog(self).exec())
        help_menu.addAction("Optional tools (audio decoder, script decompiler...)...", self.show_tools)
        help_menu.addAction("Open plugins folder", open_plugins_folder)
        help_menu.addSeparator()
        help_menu.addAction("Open log file", lambda: open_path(LOG_FILE))
        help_menu.addAction("Open crash log", lambda: open_path(CRASH_FILE))
        help_menu.addAction("Open app folder", lambda: os.startfile(APP_DIR))

    def set_theme(self, mode):
        self.settings.theme = mode
        self.settings.save()
        theme.apply(QApplication.instance(), mode)
        log.info("Theme: %s", mode)

    def _set_view_option(self, key, on):
        self.settings.set_option(key, on)
        self.settings.save()
        if key == "hide_unreadable":
            self.apply_filter()
            return
        VIEW_OPTIONS[key] = on
        for entry in self.loaded.values():
            session = entry.get("session")
            if session is not None:
                with session.lock:
                    session.options_changed()
        if self.current is not None and self.current.kind in MODEL_KINDS:
            self.show_asset(self.current)  # rebuild the map / scene with the new setting

    def _set_online_compat(self, on):
        self.settings.online_compat = on
        self.settings.save()
        if on:
            self.home.fetch_compat()

    # ---- progress
    def set_progress(self, done, total):
        """Progress bars at done/total; total 0 means 'busy, no idea how long' (moving bar)."""
        for bar in (self.progress, self.load_bar):
            if total <= 0:
                bar.setRange(0, 0)  # busy animation
            else:
                bar.setRange(0, total)
                bar.setValue(done)
            bar.show()

    def show_busy(self):
        self.set_progress(0, 0)

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
        self.show_busy()
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
        if self.session is not session:
            self.hidden_copies = set()  # the duplicate finder's results belong to the previous game
            if self._duplicates is not None:
                with QSignalBlocker(self._duplicates.hide):
                    self._duplicates.hide.setChecked(False)
        self.session = session
        if self._modmaker is not None and self._modmaker.isVisible():
            self._modmaker.refresh()
        self.thumb_cache = entry["thumbs"]
        self.stats = entry["stats"]
        project = self.current_project()
        self.favorites = set(project.get("favorites", [])) if project else entry["favorites"]
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        kind_icons = {"audio": self.style().standardIcon(QStyle.SP_MediaVolume),
                      "video": self.style().standardIcon(QStyle.SP_MediaPlay),
                      "font": self.style().standardIcon(QStyle.SP_FileDialogDetailedView),
                      "data": self.style().standardIcon(QStyle.SP_FileDialogInfoView),
                      "animation": self.style().standardIcon(QStyle.SP_BrowserReload)}

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
                child.setIcon(0, self.thumb_cache.get(asset.key, self.blank) if has_thumbnail(asset) else
                              kind_icons.get(kind, file_icon))
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
        pending, self._pending_unity_export = self._pending_unity_export, None
        if pending is not None and norm_path(pending[0]) == key:
            QTimer.singleShot(0, lambda: self._run_unity_export(*pending))

    def on_load_failed(self, tb):
        self._pending_unity_export = None
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
        unreadable = self.settings.option("hide_unreadable") and any(is_unreadable(st) for _k, st in batch)
        if (unreadable or AssetFilter(self.search.text()).conditions) and not self.filter_timer.isActive():
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
        hide_unreadable = self.settings.option("hide_unreadable")
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
                        stats = self.stats.get(asset.key)
                        ok = filt.matches(asset.name, kind, stats)
                        if ok and hide_unreadable and is_unreadable(stats):
                            ok = False
                        if ok and want == "fav":
                            ok = asset.uid in self.favorites
                        if ok and asset.uid in self.hidden_copies:
                            ok = False
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
        self.settings.view = "grid" if grid else "list"
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
                icon = self.thumb_cache.get(key, self.blank_big) if has_thumbnail(asset) else file_icon
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
                if asset and has_thumbnail(asset) and asset.key not in self.thumb_cache:
                    jobs.append((asset.key, self.session, asset))
        else:
            height = self.tree.viewport().height()
            item = self.tree.itemAt(QPoint(4, 4))
            extra = 0
            while item is not None and extra < 20:
                if self.tree.visualItemRect(item).top() > height:
                    extra += 1
                asset = item.data(0, Qt.UserRole)
                if asset and has_thumbnail(asset) and asset.key not in self.thumb_cache:
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
            elif has_thumbnail(asset):
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
        session = self.session
        if session is None:  # a leftover selection while another game is opening
            return
        self.current = asset
        self.audio_view.stop()
        self.video_view.stop()
        self.flipbook_timer.stop()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with session.lock:
                if asset.kind == "scene" and hasattr(session, "is_ui") and session.is_ui(asset) and                         self.show_structure(asset, ui=True):
                    pass  # a UI prefab: drawn as a 2D picture
                elif asset.kind in MODEL_KINDS:
                    try:
                        self.show_mesh(asset)
                    except ValueError as e:
                        # Nothing to draw (a sound or logic-only prefab): show what it's made of instead.
                        if asset.kind != "scene" or not self.show_structure(asset, str(e)):
                            raise
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
                elif asset.kind == "font":
                    self.show_texture(asset, session.font(asset))
                elif asset.kind == "video":
                    self.stack.setCurrentWidget(self.video_view)
                    try:
                        data, ext = session.video(asset)
                        self.video_view.load(asset.name, data, ext)
                    except Exception as e:
                        if not isinstance(e, (ValueError, NotImplementedError)):
                            log.exception("Couldn't read the video '%s'", asset.name)
                        self.video_view.show_error(asset.name, str(e))
                elif asset.kind == "data":
                    self.text_view.setPlainText(session.text(asset)[:MAX_TEXT_CHARS])
                    self.stack.setCurrentWidget(self.text_view)
                elif asset.kind == "animation":
                    text = session.text(asset)
                    try:
                        targets = session.animation_targets(asset)
                    except Exception:
                        log.exception("Finding models for animation '%s' failed", asset.name)
                        targets = []
                    try:
                        frames, _length = session.sprite_frames(asset)
                    except Exception:
                        log.exception("Reading the sprite frames of '%s' failed", asset.name)
                        frames = []
                    self.anim_view.show_clip(text, targets, flipbook=len(frames))
                    self.stack.setCurrentWidget(self.anim_view)
                elif asset.kind == "text":
                    self.show_text_file(asset)
                else:
                    self.show_raw(asset)  # no dedicated preview: show what the bytes are
        except (ValueError, NotImplementedError) as e:
            # Expected cases (nothing visible, unsupported format...): a plain message is enough.
            log.info("No preview for %s '%s': %s", asset.kind, asset.name, e)
            self.stack.setCurrentWidget(self.text_view)
            self.text_view.setPlainText(f"{asset.name}\n\n{e}")
        except Exception as e:
            log.exception("Could not preview %s '%s'", asset.kind, asset.name)
            self.stack.setCurrentWidget(self.text_view)
            self.text_view.setPlainText(f"Could not preview {asset.name}:\n\n{e}\n\n{traceback.format_exc()}")
        finally:
            QApplication.restoreOverrideCursor()

    def show_tools(self, at_startup=False):
        ToolsDialog(self.settings, self, at_startup=at_startup).exec()

    def check_tools_at_startup(self):
        """Offer to install recommended helpers that are missing (unless turned off in the dialog)."""
        if not self.settings.check_tools:
            return
        try:
            missing = missing_recommended(self.settings.blender_path)
        except Exception:
            log.exception("Checking the optional tools failed")
            return
        if missing:
            log.info("Optional tools not installed: %s", ", ".join(t.name for t in missing))
            self.show_tools(at_startup=True)

    def play_flipbook(self):
        """Play the current clip's sprite frames in the image view."""
        clip, session = self.current, self.session
        if clip is None or session is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            with session.lock:
                frames, length = session.sprite_frames(clip)
                cache = {}
                images = []
                for t, sprite in frames:
                    if sprite.key not in cache:
                        cache[sprite.key] = session.image(sprite)
                    images.append((t, cache[sprite.key]))
        except Exception as e:
            log.exception("Playing the sprite animation '%s' failed", clip.name)
            QMessageBox.warning(self, "Sprite animation", str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        if not images:
            return
        # Frames have different sizes: pad them all to one canvas so the animation doesn't jump.
        w = max(img.width for _t, img in images)
        h = max(img.height for _t, img in images)
        padded = []
        for t, img in images:
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(img.convert("RGBA"), ((w - img.width) // 2, h - img.height))
            padded.append((t, canvas))
        step = length / len(padded) if length > 0 else 1 / 12
        self._flipbook = {"frames": padded, "length": max(length, padded[-1][0] + step), "start": time.time(),
                          "index": -1, "name": clip.name}
        self.stack.setCurrentWidget(self.image_view)
        self.image_view.set_links("Sprite animation", [], f"{len(padded)} frames, {self._flipbook['length']:.2f} s "
                                                        "(select another asset to stop)")
        self._flipbook_tick()
        self.image_view.fit()
        self.flipbook_timer.start()

    def _flipbook_tick(self):
        fb = self._flipbook
        if fb is None or self.stack.currentWidget() is not self.image_view:
            self.flipbook_timer.stop()
            return
        t = (time.time() - fb["start"]) % fb["length"]
        index = 0
        for i, (ft, _img) in enumerate(fb["frames"]):
            if ft <= t:
                index = i
        if index != fb["index"]:
            fb["index"] = index
            self.image_view.show_image(fb["frames"][index][1],
                                       f"{fb['name']}  frame {index + 1}/{len(fb['frames'])}", fit=False)

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
        return session_materials(self.session, asset)

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
        groups = texture_groups(md, materials)
        image_of = texture_loader(session, len(groups))
        tex = image_of(display_texture(md, materials))
        parts = [(tris, image_of(tex_asset), color, alpha) for tex_asset, color, tris, alpha in groups]
        self.stack.setCurrentWidget(self.mesh_view)
        flat = bool(getattr(md, "view_2d", False))
        self.mesh_view.show_mesh(poly, tex, parts, flat=flat, gizmos=getattr(md, "gizmos", None),
                                 gizmos_only=bool(getattr(md, "gizmos_only", False)),
                                 owners=getattr(md, "owners", None))
        self.mesh_view.set_fly(is_place(asset) and not flat)
        n_tex = sum(len(m.textures) for m in materials)
        log.info("Model '%s': %s verts, %s tris, %d material(s), %d texture(s)%s", asset.name,
                 f"{poly.n_points:,}", f"{poly.n_cells:,}", len(materials), n_tex,
                 "" if tex is not None else " - no texture found")
        if md.skipped:
            log.info("Skipped %d line/point part(s) of '%s'", md.skipped, asset.name)
        rows = model_info_rows(session, asset, md, poly.n_points, poly.n_cells, self.mesh_view.uv_channels(),
                               favorite=asset.uid in self.favorites)
        jobs = self.mesh_view.panel.set_info(asset.name, "<br>".join(rows), materials, self.blank_big)
        self._request_icons(jobs, self.mesh_view.panel.set_icon)
        nodes = self._structure_nodes(asset)
        self.mesh_view.panel.set_structure(nodes, uid_map(session), self._game_entries(asset, nodes))

    def _game_entries(self, asset, nodes):
        """Game data entries that name a prefab, each with its code links (see uniview.game_data)."""
        if not nodes or asset.ref.get("type") == "scene":
            return []
        try:
            entries = game_data.entries_for(self.session, asset.name, nodes)
            for entry in entries:
                entry["links"] = code_links.links(self.session.path, [r[:2] for r in entry["rows"]])
            return entries
        except Exception:
            log.exception("Looking up game data for '%s' failed", asset.name)
            return []

    def show_code(self, link):
        """Decompile the function a game data entry points at and show it in a window."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Decompiling with ILSpy...")
        try:
            type_name, method, text = code_links.method_source(link)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
            log.warning("Couldn't decompile %s: %s", link["title"], e)
            QMessageBox.warning(self, "C# code", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.statusBar().clearMessage()
        header = f"// {os.path.basename(link['dll'])}  ›  {type_name}\n// decompiled with ILSpy\n\n"
        view = QPlainTextEdit(header + text, readOnly=True)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setTabStopDistance(view.fontMetrics().horizontalAdvance(" ") * 4)
        view.setWindowTitle(f"{type_name.rsplit('.', 1)[-1]}.{method} - {APP_SHORT}")
        view.resize(760, 420)
        view.setAttribute(Qt.WA_DeleteOnClose)
        view.destroyed.connect(lambda *_: self._windows.remove(view) if view in self._windows else None)
        self._windows.append(view)
        view.show()
        log.info("Showing decompiled %s.%s", type_name, method)

    def _structure_nodes(self, asset):
        """hierarchy() nodes of a prefab or scene for the info panel's Objects & scripts tab, or None."""
        if asset.kind != "scene" or not hasattr(self.session, "hierarchy"):
            return None
        try:
            return self.session.hierarchy(asset)
        except Exception:
            log.exception("Reading the objects of '%s' failed", asset.name)
            return None

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

    def show_text_file(self, asset):
        """A text asset: decoded in its own encoding, or - when it's really binary - shown as an image, unpacked,
        or described with its readable strings and a hex dump."""
        session = self.session
        try:
            data = session.raw(asset)
        except Exception:
            data = None
        if not isinstance(data, (bytes, bytearray)):
            text = session.text(asset)
            data = text if isinstance(text, bytes) else None
            if data is None:
                self.text_view.setPlainText(text[:MAX_TEXT_CHARS])
                self.stack.setCurrentWidget(self.text_view)
                return
        self._show_bytes(asset, bytes(data))

    def _show_bytes(self, asset, data, header=""):
        """Bytes through the inspector: text in its encoding, an image, or format + strings + hex dump."""
        found = inspect_bytes(data)
        if found["image"] is not None:
            self.image_view.show_image(found["image"], asset.name)
            self.image_view.set_links("", [], f"{found['format']} inside {asset.name}")
            self.stack.setCurrentWidget(self.image_view)
            return
        self.text_view.setPlainText((header + found["text"])[:MAX_TEXT_CHARS])
        self.stack.setCurrentWidget(self.text_view)
        what = found["format"] or "text"
        if found["encoding"] and found["encoding"] != "utf-8":
            what += f" ({found['encoding']})"
        self.statusBar().showMessage(f"{asset.name}: {what}, {len(data):,} bytes")

    def show_content_search(self):
        if self._content_search is None:
            self._content_search = ContentSearchDialog(lambda: self.session, self)
            self._content_search.goto_requested.connect(self._goto_from_search)
        self._content_search.show()
        self._content_search.raise_()
        self._content_search.activateWindow()
        self._content_search.query.setFocus()

    def show_duplicates(self):
        if self._duplicates is None:
            self._duplicates = DuplicatesDialog(lambda: self.session, self)
            self._duplicates.goto_requested.connect(self._goto_from_search)
            self._duplicates.hide_changed.connect(self._set_hidden_copies)
        self._duplicates.show()
        self._duplicates.raise_()
        self._duplicates.activateWindow()

    # ---- Mod Maker
    def show_modmaker(self):
        if self._modmaker is None:
            self._modmaker = ModMakerDialog(self)
        self._modmaker.show()
        self._modmaker.refresh()
        self._modmaker.raise_()
        self._modmaker.activateWindow()

    def replace_selected(self):
        selected = self.selected_assets()
        if not selected:
            QMessageBox.information(self, "Mod Maker", "Select an asset first.")
            return
        self.replace_with_file(selected[0])

    def replace_with_file(self, data):
        folder = replace_asset(self, self.session, data, self.last_dir)
        if folder is None:
            return
        self.last_dir = folder
        count = len(modmaker.ModProject(self.session.path).edits)
        self.statusBar().showMessage(f"'{data.name}' added to the mod ({count} replacement(s)) - "
                                     "Mod → Mod Maker to install or export it", 8000)
        if self._modmaker is not None and self._modmaker.isVisible():
            self._modmaker.refresh()

    def remove_replacement(self, data):
        modmaker.ModProject(self.session.path).remove(data.uid)
        self.statusBar().showMessage(f"'{data.name}' removed from the mod", 5000)
        if self._modmaker is not None and self._modmaker.isVisible():
            self._modmaker.refresh()

    def unload_for_mod(self):
        """Let go of the open game's files before Mod Maker replaces them."""
        if self.loading_path:
            self.unload_game(self.loading_path)

    def reload_game(self):
        if self.loading_path:
            self.load(self.loading_path)

    def show_versions(self):
        if self._versions is None:
            self._versions = VersionsDialog(self._open_game, self)
            self._versions.goto_requested.connect(self._goto_from_search)
        self._versions.show()
        self._versions.raise_()
        self._versions.activateWindow()
        self._versions.refresh()

    def _open_game(self):
        """(session, game name, game folder) of the game being viewed, or None."""
        if self.session is None:
            return None
        project = self.current_project()
        folder = self.loading_path or self.session.path
        return self.session, (project or {}).get("name") or os.path.basename(folder.rstrip("\\/")), folder

    def _set_hidden_copies(self, uids):
        self.hidden_copies = set(uids)
        self.apply_filter()

    def _goto_from_search(self, key):
        self.pages.setCurrentWidget(self.viewer)
        self.goto_asset(key)

    def show_raw(self, asset=None):
        """Any asset, any engine: what its stored bytes are (Asset > Show raw bytes, and assets without a preview)."""
        asset = asset or self.current
        session = self.session
        if asset is None or session is None:
            return
        self.current = asset
        rows = []
        try:
            rows = [f"{label}: {value}" for label, value in session.describe(asset)]
        except Exception:
            pass
        header = f"{asset.name}\n" + "".join(f"{r}\n" for r in rows) + f"Kind: {asset.kind}\n\n"
        try:
            with session.lock:
                data = session.raw(asset)
        except Exception as e:
            self.text_view.setPlainText(header + f"No raw bytes for this asset: {e}")
            self.stack.setCurrentWidget(self.text_view)
            return
        self._show_bytes(asset, bytes(data), header)

    def show_structure(self, asset, reason="", ui=False):
        """Prefab/scene structure view (GameObjects, components, assets used; ui: also a 2D picture of a UI
        prefab). False if the engine can't."""
        session = self.session
        if not hasattr(session, "hierarchy"):
            return False
        try:
            nodes = session.hierarchy(asset)
        except Exception:
            log.exception("Reading the objects of '%s' failed", asset.name)
            return False
        if not nodes:
            return False
        by_uid = uid_map(session)

        def material_textures(uid):
            try:
                details = session.material_details(uid)
            except Exception:
                return []
            return [tex for _prop, tex, _scale, _offset in (details or {}).get("textures", []) if tex is not None]

        used = prefab_info.used_assets(nodes, by_uid,
                                       material_textures if hasattr(session, "material_details") else None)
        picture = None
        if ui:
            picture = ui_picture(session, nodes, by_uid)
            if picture is not None:
                picture = (canvas_render.to_pil(picture[0]),) + picture[1:]
        if ui and picture is None:
            return False  # nothing drawn: let the 3D view (or its fallback) have it
        if ui:
            header = ("UI prefab drawn in 2D with the game's sprites and fonts - close to the game, but without "
                      "scripts or effects (shadows, outlines, blur). ")
        else:
            header = f"{reason} Showing what it's made of: "
        log.info("Structure view for '%s': %d object(s)%s", asset.name, len(nodes), ", UI picture" if ui else "")
        self.prefab_view.show_prefab(asset.name, header, nodes, by_uid, used, self.blank_big, picture)
        self._request_icons(used, self.prefab_view.set_link_icon)
        self.stack.setCurrentWidget(self.prefab_view)
        return True

    def show_texture(self, asset, img):
        name = asset.name
        log.info("Texture '%s': %dx%d %s", name, img.width, img.height, img.mode)
        try:
            rects = self.session.sprite_rects(asset) if self.session is not None else []
        except Exception:
            log.exception("Finding the sprites of '%s' failed", name)
            rects = []
        self.image_view.show_image(img, ("\u2605 " if asset.uid in self.favorites else "") + name, rects)
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
        if data.kind in MODEL_KINDS:
            menu.addAction("Open in Blender", lambda: self.open_in_blender(data))
        fav = all(d.uid in self.favorites for d in selected)
        menu.addAction(("Remove from favorites" if fav else "Add to favorites") + "   Ctrl+D",
                       lambda: self.toggle_favorites(selected))
        menu.addAction("Show raw bytes   Ctrl+R", lambda: self.show_raw(data))
        if modmaker.can_replace(self.session, data):
            menu.addSeparator()
            menu.addAction("Replace with file... (Mod Maker)", lambda: self.replace_with_file(data))
            if modmaker.ModProject(self.session.path).edit_for(data.uid) is not None:
                menu.addAction("Remove replacement from mod", lambda: self.remove_replacement(data))
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
            log.exception("Applying the texture '%s' failed", getattr(data, "name", data))
            QMessageBox.warning(self, "Texture", str(e))

    # ---- Blender
    def choose_blender(self):
        path, _ = QFileDialog.getOpenFileName(self, "Where is blender.exe?", self.settings.blender_path,
                                              "Blender (blender.exe);;Programs (*.exe)")
        if path:
            self.settings.blender_path = path
            self.settings.save()
            log.info("Blender location set to %s", path)
        return path or None

    def open_in_blender(self, data=None):
        data = data or self.current
        if not data or data.kind not in MODEL_KINDS:
            QMessageBox.information(self, "Open in Blender", "Select a model first.")
            return
        blender = find_blender(self.settings.blender_path)
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
            write_asset(self.session, data, path)
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
            self.settings.last_dir = self.last_dir
            self.settings.save()
        return path, chosen

    def export_selected(self):
        datas = self.selected_assets()
        if len(datas) > 1:
            self.export_many(datas)
        elif datas:
            self.export_item(datas[0])

    def export_selected_mesh(self):
        if self.current and self.current.kind in MODEL_KINDS:
            self.export_item(self.current)

    def save_shown_image(self, view=None):
        img = (view or self.image_view).image
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

    def export_item(self, asset):
        if asset.kind in MODEL_KINDS:
            filters = "Wavefront OBJ + textures (*.obj);;glTF binary, textures inside (*.glb)"
        elif asset.kind in IMAGE_KINDS:
            filters = "PNG image (*.png)"
        else:
            filters = "All files (*)"
        ext = export_ext(asset, self.settings.model_format)
        if asset.kind in ("font", "video") and ext:
            label = {"ttf": "TrueType font", "otf": "OpenType font"}.get(ext, f"{ext.upper()} video")
            filters = f"{label} (*.{ext});;All files (*)"
        base = safe_filename(os.path.splitext(os.path.basename(asset.name))[0]
                             if asset.kind in ("text", "file", "audio") else os.path.basename(asset.name))
        path, chosen = self.ask_save_path(f"{base}.{ext}", filters)
        if not path:
            return
        if asset.kind in MODEL_KINDS and not path.lower().endswith((".obj", ".glb")):
            path += ".glb" if "glb" in chosen else ".obj"
        try:
            written = write_asset(self.session, asset, path)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(w) for w in written))
            for w in written:
                log.info("Saved %s", w)
        except Exception as e:
            log.exception("Saving %s failed", asset.name)
            QMessageBox.warning(self, "Save failed", str(e))

    def export_animated(self, model):
        """Save `model` as a GLB with its skeleton and the current animation clip."""
        clip, session = self.current, self.session
        if clip is None or clip.kind != "animation" or session is None:
            return
        base = safe_filename(f"{os.path.basename(model.name)}_{os.path.basename(clip.name)}")
        path, _chosen = self.ask_save_path(f"{base}.glb", "glTF binary with skeleton and animation (*.glb)")
        if not path:
            return
        if not path.lower().endswith(".glb"):
            path += ".glb"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            written, rig = write_animated_glb(session, model, clip, path)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(w) for w in written))
            log.info("Saved %s ('%s' on '%s', %d joints)", path, clip.name, model.name, len(rig["joints"]))
        except Exception as e:
            if not isinstance(e, (ValueError, NotImplementedError)):
                log.exception("Saving the animated model '%s' failed", model.name)
            QMessageBox.warning(self, "Save animated GLB", str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def write_asset(self, asset, path):
        """Save one asset of the loaded game to `path`; returns the list of files written."""
        return write_asset(self.session, asset, path)

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
        dlg = ExportDialog(len(items), any(a.kind in MODEL_KINDS for a in items), self.settings, self)
        if dlg.exec():
            folder, model_format, keep = dlg.values()
            self._export_to_folder(items, folder, model_format, keep)

    def _export_to_folder(self, items, folder, model_format="obj", keep_structure=False):
        log.info("Exporting %d item(s) to %s (models as %s%s)", len(items), folder, model_format.upper(),
                 ", keeping the game's folders" if keep_structure else "")
        ok = fail = 0
        self.set_progress(0, len(items))
        for n, (asset, path) in enumerate(plan_export(items, folder, model_format, keep_structure), 1):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write_asset(self.session, asset, path)
                ok += 1
            except Exception as e:
                fail += 1
                log.warning("Could not export %s: %s", asset.name, e)
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
        self.settings.last_dir = self.last_dir
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
