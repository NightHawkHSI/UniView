"""The projects (home) page: one card per saved game."""

import ctypes
import os
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import QEvent, QFileInfo, QRect, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFileIconProvider,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

import engines
from uniview.catalog import UNKNOWN_ENGINE, UNTAGGED, compat_entry, group_projects, project_shown
from uniview.constants import APP_TITLE, log
from uniview.projects import detect_project_engine, engine_info_text, find_steam_games, game_exe
from uniview.settings import COMPAT_STATUS, compat_report_url, fetch_compat, load_compat
from uniview.ui.dialogs import EngineOptionsDialog, SteamPickDialog, TagsDialog, edit_project_notes


def exe_icon_image(exe, size=96):
    """The exe's icon as a QImage (safe off the GUI thread, unlike QFileIconProvider), or None."""
    if sys.platform != "win32":
        return None
    try:
        shell32, user32 = ctypes.windll.shell32, ctypes.windll.user32
        hicon = ctypes.c_void_p()
        # SHDefExtractIconW(file, index, flags, large, small, MAKELONG(large size, small size))
        if shell32.SHDefExtractIconW(exe, 0, 0, ctypes.byref(hicon), None, size | (16 << 16)) != 0 or not hicon:
            return None
        try:
            image = QImage.fromHICON(hicon.value)
        finally:
            user32.DestroyIcon(hicon)
        return None if image.isNull() else image
    except Exception:
        log.debug("Couldn't extract the icon of %s", exe, exc_info=True)
        return None


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

class HomePage(QWidget):
    """Start page: a box per saved game plus 'Add game' / 'Find games' boxes."""

    open_requested = Signal(str)  # project path
    unload_requested = Signal(str)
    export_unity_requested = Signal(str)  # project path
    _counted = Signal(str, int)
    _scanned = Signal(str, bool, object, object)  # path, folder exists, exe, icon QImage
    _detected = Signal(str, object)
    _compat_fetched = Signal(int)
    _games_found = Signal(object)
    _find_progress = Signal(int, int, str)  # done, total, folder name
    ADD, FIND = "__add__", "__find__"
    UNTAGGED = UNTAGGED
    UNKNOWN_ENGINE = UNKNOWN_ENGINE
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
        # Folder existence and exe icons, looked up off the GUI thread: on a cold disk every
        # isdir/listdir/icon read can take tens of ms, and refresh() runs again after each burst of
        # background results, so doing it inline froze the page for seconds at startup.
        self._scans = {}  # path -> (exists, QIcon or None)
        self._pending_scans = set()
        self._std_icons = {}
        self._scan_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="game-scan")
        self._scanned.connect(self._on_scanned)
        self.compat = load_compat()
        self._counting = set()
        self._detecting = set()
        self._headers = []
        self._counted.connect(self._on_counted)
        self._detected.connect(self._on_detected)
        self._compat_fetched.connect(self._on_compat_fetched)
        self._games_found.connect(self._on_games_found)
        self._find_progress.connect(self._on_find_progress)
        self._finding = False
        self._find_dialog = None
        self._find_cancel = False
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
        self._engine_filter = settings.home_engine
        self.group_combo = QComboBox()
        for label, value in self.GROUPS:
            self.group_combo.addItem(label, value)
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(settings.home_group)))
        self.group_combo.currentIndexChanged.connect(lambda _: self._catalog_changed("home_group",
                                                                                     self.group_combo))
        self.sort_combo = QComboBox()
        for label, value in self.SORTS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(settings.home_sort)))
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
        if self.settings.online_compat:
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
        return compat_entry(self.compat, path)

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
        item = QListWidgetItem(self._std_icon(icon), text)
        item.setData(Qt.UserRole, key)
        item.setData(CardDelegate.STATE_ROLE, "action")
        return item

    def _catalog_changed(self, key, combo):
        setattr(self.settings, key, combo.currentData() or "")
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

    def _std_icon(self, which):
        """Cached QStyle icon - standardIcon() hits the Windows shell (~20 ms) on every call."""
        icon = self._std_icons.get(which)
        if icon is None:
            icon = self._std_icons[which] = self.style().standardIcon(which)
        return icon

    def _scan(self, path):
        """(folder exists, icon or None) for a game, from cache; queues a background look-up if unknown."""
        scan = self._scans.get(path)
        if scan is None:
            self._scans[path] = scan = (True, None)  # optimistic until the scan reports back

            def work():
                exists = os.path.isdir(path)
                exe = game_exe(path) if exists else None
                self._scanned.emit(path, exists, exe, exe_icon_image(exe) if exe else None)

            self._scan_pool.submit(work)
            self._pending_scans.add(path)
        return scan

    def _on_scanned(self, path, exists, exe, image):
        self._pending_scans.discard(path)
        if image is not None:
            icon = QIcon(QPixmap.fromImage(image))
        elif exe:
            icon = self.icons.icon(QFileInfo(exe))  # warm by now - the scan already read the exe
        else:
            icon = None
        self._scans[path] = (exists, icon)
        self.refresh_timer.start()

    def rescan(self, path=None):
        """Forget cached folder/icon look-ups (one game, or all) so the next refresh redoes them."""
        if path is None:
            self._scans.clear()
        else:
            self._scans.pop(path, None)

    def _card_item(self, project):
        path = project["path"]
        exists, icon = self._scan(path)
        item = QListWidgetItem(icon or self._std_icon(QStyle.SP_DirIcon), project["name"])
        item.setData(Qt.UserRole, path)
        compat = self.compat_entry(path)
        engine = engine_info_text(project)
        tip = [path]
        if not exists:
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
            exists, _icon = self._scan(path)
            if exists and path not in self._pending_scans:
                if "engine" not in project or "engine_version" not in project:
                    self._detect_engine(path, project.get("engine"))
                elif project.get("file_count") is None:
                    self._count_files(path, project.get("engine"))
            if project_shown(project, tag, engine_filter, terms, self.compat):
                shown.append(project)

        if group_by:
            for title, projects in group_projects(shown, group_by, self.compat, self.is_loaded):
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
                self.rescan(project["path"])
                self.refresh()
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
        self._find_cancel = False
        log.info("Searching Steam libraries for games the engine plugins can read...")
        dlg = QProgressDialog("Looking for your Steam libraries...", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Find games in Steam")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(460)
        dlg.setAutoReset(False)
        dlg.setAutoClose(False)
        dlg.canceled.connect(self._cancel_find)
        dlg.show()
        self._find_dialog = dlg

        def work():
            try:
                games = find_steam_games(self._find_progress.emit, lambda: self._find_cancel)
            except Exception:
                log.exception("Searching the Steam libraries failed")
                games = []
            try:
                self._games_found.emit(games)
            except RuntimeError:
                pass  # window closed

        threading.Thread(target=work, daemon=True, name="find-games").start()

    def _cancel_find(self):
        self._find_cancel = True
        if self._find_dialog is not None:
            self._find_dialog.setLabelText("Stopping...")

    def _on_find_progress(self, done, total, name):
        dlg = self._find_dialog
        if dlg is None or self._find_cancel:
            return
        dlg.setMaximum(max(total, 1))
        dlg.setValue(done)
        dlg.setLabelText(f"Checking game folder {done + 1:,} of {total:,}:\n{name}")

    def _on_games_found(self, games):
        self._finding = False
        if self._find_dialog is not None:
            self._find_dialog.canceled.disconnect(self._cancel_find)
            self._find_dialog.close()
            self._find_dialog.deleteLater()
            self._find_dialog = None
        self.refresh()
        if self._find_cancel and not games:
            return
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
        if project.get("engine") == "unity":
            menu.addAction("Export as Unity project...", lambda: self.export_unity_requested.emit(path))
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
