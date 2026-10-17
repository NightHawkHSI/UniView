"""Options > Plugin manager: the engine plugins, switch them on/off, install from a file, remove.

The file work is in uniview.plugin_manager; changes apply after a restart, which this offers.
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

import engines
from uniview import plugin_manager
from uniview.constants import APP_SHORT, PLUGINS_DIR, REPO_URL, log
from uniview.ui.dialogs import open_plugins_folder

INSTALL_WARNING = ("Plugins are Python code: they run inside {app} with the same access to your files as {app} "
                   "itself. Only install plugins from people you trust.")


class PluginManagerDialog(QDialog):
    """The plugin list with a switch per plugin, Install / Remove, and a restart prompt."""

    # What was installed / removed since UniView started (the dialog is opened anew each time).
    _installed: list[str] = []
    _removed: list[str] = []

    def __init__(self, settings, on_restart=None, parent=None, plugins_dir=PLUGINS_DIR):
        super().__init__(parent)
        self.settings = settings
        self.on_restart = on_restart
        self.plugins_dir = plugins_dir
        self.report = engines.load_report()
        self._filling = False
        self.setWindowTitle("Plugin manager")
        self.resize(780, 480)
        self.setAcceptDrops(True)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Engine", "Version", "From", "About"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.itemChanged.connect(self._toggled)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        header = self.tree.header()
        for col in (0, 1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)

        info = QLabel(
            "Each game is read by an engine plugin. Get published plugins from the <b>Plugins</b> tab on the start "
            "page. Here, untick one to switch it off, or install another from a "
            "<code>.py</code> / <code>.zip</code> file (you can also drop it on this window). To write your own, "
            "start from <code>plugins/_template.py</code> and see "
            f"<a href='{REPO_URL}/blob/main/PLUGINS.md'>PLUGINS.md</a>. A plugin with the same id as a built-in "
            "one replaces it. Right-click a game → <i>Read with engine</i> to pick one by hand.")
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)

        self.banner = QLabel("Restart UniView to apply the changes.")
        self.restart_button = QPushButton("Restart now")
        self.restart_button.clicked.connect(self._restart)
        self.restart_button.setEnabled(on_restart is not None)
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner, 1)
        banner_row.addWidget(self.restart_button)
        self.banner_widgets = (self.banner, self.restart_button)

        install = QPushButton("Install plugin...")
        install.clicked.connect(self._pick_file)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Moves the plugin file to plugins/_removed/")
        self.remove_button.clicked.connect(self._remove_current)
        folder = QPushButton("Open plugins folder")
        folder.clicked.connect(open_plugins_folder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        for b in (install, self.remove_button, folder):
            buttons.addButton(b, QDialogButtonBox.ActionRole)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tree, 1)
        lay.addWidget(info)
        lay.addLayout(banner_row)
        lay.addWidget(buttons)
        self._fill()

    # -- list --

    def _rows(self):
        return plugin_manager.rows(self.report, self.settings.disabled_plugins, self.plugins_dir,
                                   self._installed, self._removed)

    def _fill(self):
        self._filling = True
        current = self.tree.currentItem()
        current_key = current.data(0, Qt.UserRole).key if current else None
        self.tree.clear()
        red, dim = QBrush(QColor("#ef5350")), QBrush(QColor("#9e9e9e"))
        for row in self._rows():
            about = {"removed": "Removed - restart UniView to unload it"}.get(row.pending, row.about)
            item = QTreeWidgetItem([row.name, row.version, row.origin, about])
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(3, about)
            if row.pending != "removed":
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if row.enabled else Qt.Unchecked)
                item.setToolTip(0, "Untick to switch this plugin off (applies after a restart)")
            if row.failed:
                item.setForeground(3, red)
            elif row.pending == "removed" or not row.enabled:
                for col in range(4):
                    item.setForeground(col, dim)
            self.tree.addTopLevelItem(item)
            if row.key == current_key:
                self.tree.setCurrentItem(item)
        self._filling = False
        self._update_buttons()

    def _update_buttons(self):
        item = self.tree.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        self.remove_button.setEnabled(bool(row and row.removable and row.pending != "removed"))
        for w in self.banner_widgets:
            w.setVisible(self.restart_needed())

    def restart_needed(self):
        if self._installed or self._removed:
            return True
        keys = {e.key for e in self.report}
        at_start = {e.key for e in self.report if e.disabled}
        return at_start != set(self.settings.disabled_plugins) & keys

    def _toggled(self, item, column):
        if self._filling or column != 0:
            return
        row = item.data(0, Qt.UserRole)
        on = item.checkState(0) == Qt.Checked
        disabled = [k for k in self.settings.disabled_plugins if k != row.key]
        if not on:
            disabled.append(row.key)
        self.settings.disabled_plugins = disabled
        self.settings.save()
        log.info("Plugin manager: %s switched %s (after a restart)", row.key, "on" if on else "off")
        self._fill()

    # -- install / remove --

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Install plugin", os.path.expanduser("~"),
                                              "UniView plugins (*.py *.zip)")
        if path:
            self.install_file(path)

    def install_file(self, path):
        try:
            files = plugin_manager.plan(path)
        except (OSError, plugin_manager.PluginInstallError) as e:
            QMessageBox.warning(self, "Install plugin", f"Can't install {os.path.basename(path)}:\n\n{e}")
            return False
        names = plugin_manager.top_level(files)
        text = (f"Install {', '.join(names)} into the plugins folder?\n\n"
                + INSTALL_WARNING.format(app=APP_SHORT))
        replaced = plugin_manager.conflicts(files, self.plugins_dir)
        if replaced:
            text += f"\n\nThis replaces the existing {', '.join(replaced)}."
        if QMessageBox.question(self, "Install plugin", text) != QMessageBox.Yes:
            return False
        try:
            added = plugin_manager.install(files, self.plugins_dir)
        except OSError as e:
            QMessageBox.warning(self, "Install plugin", f"Couldn't write to the plugins folder:\n\n{e}")
            return False
        for fname in added:
            if fname in self._removed:
                self._removed.remove(fname)
            if fname not in self._installed:
                self._installed.append(fname)
        self._fill()
        return True

    def _remove_current(self):
        item = self.tree.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        if not row or not row.removable:
            return
        if QMessageBox.question(self, "Remove plugin",
                                f"Remove {row.origin}? It's moved to plugins/{plugin_manager.REMOVED_DIR}/, "
                                "so you can put it back later.") != QMessageBox.Yes:
            return
        try:
            plugin_manager.remove(row.origin, self.plugins_dir)
        except OSError as e:
            QMessageBox.warning(self, "Remove plugin", f"Couldn't remove {row.origin}:\n\n{e}")
            return
        if row.origin in self._installed:
            self._installed.remove(row.origin)  # never loaded: nothing to restart for
        else:
            self._removed.append(row.origin)
        self.settings.disabled_plugins = [k for k in self.settings.disabled_plugins if k != row.key]
        self.settings.save()
        self._fill()

    # -- drag and drop --

    def _dropped_paths(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        return [p for p in paths if p.lower().endswith(plugin_manager.INSTALLABLE)]

    def dragEnterEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for path in self._dropped_paths(event):
            self.install_file(path)
        event.acceptProposedAction()

    def _restart(self):
        self.accept()
        if self.on_restart:
            self.on_restart()
