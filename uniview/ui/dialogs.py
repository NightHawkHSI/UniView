"""Small dialogs: export, Steam game picker, tags, engine options, notes, plugins."""

import os

from PySide6.QtCore import QFileInfo, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

import engines
from uniview.constants import APP_SHORT, PLUGINS_DIR, REPO_URL, log
from uniview.projects import game_exe, project_options


class ExportDialog(QDialog):
    """Options for bulk export: folder, model format, keep the game's folder structure."""

    def __init__(self, count, has_models, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Export")
        self.folder = QLineEdit(settings.export_dir or settings.last_dir)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        self.format = QComboBox()
        self.format.addItem("OBJ + MTL + PNG textures", "obj")
        self.format.addItem("GLB (one file, textures inside)", "glb")
        self.format.setCurrentIndex(max(0, self.format.findData(settings.model_format)))
        self.format.setEnabled(has_models)
        self.keep = QCheckBox("Keep the game's folder structure")
        self.keep.setToolTip("Puts each asset in folders that mirror where it lives in the game\n"
                             "(e.g. assets/prefabs/weapons/...), so big dumps stay easy to browse.")
        self.keep.setChecked(settings.keep_structure)
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
        self.settings.export_dir = self.folder.text().strip()
        self.settings.model_format = self.format.currentData()
        self.settings.keep_structure = self.keep.isChecked()
        self.settings.save()
        self.accept()

    def values(self):
        return self.folder.text().strip(), self.format.currentData(), self.keep.isChecked()

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
