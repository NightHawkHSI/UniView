"""The Plugins tab: engine plugins from the UniView-Plugins repo - installed and available, with their versions -
and Install / Update for each. The work is in uniview.plugin_index; plugins load after a restart.

Plugins are third-party code that runs locally, so every install shows a notice first.
"""

import os
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import engines
from engines.sdk import API_VERSION
from uniview import plugin_index, plugin_manager
from uniview.constants import APP_SHORT, PLUGINS_DIR, PLUGINS_INDEX_URL, PLUGINS_REPO_URL, log
from uniview.ui.dialogs import open_plugins_folder
from uniview.ui.theme import role

NOTICE = ("<b>Plugins are third-party code.</b> A plugin is a Python file that runs on your computer, inside "
          "{app}, with the same access to your files as {app} itself. The plugins listed here come from the "
          "<a href='{repo}'>UniView-Plugins</a> repository: they're open source, only read game files, and are "
          "<b>not affiliated with any game developer or publisher</b>. Only install plugins you trust, and check "
          "that the game's license allows extracting its files.")

STATE_TEXT = {"not installed": "Not installed", "installed": "Installed", "update": "Update available",
              "local": "Installed by hand", "failed": "Failed to load", "switched off": "Switched off",
              "needs newer UniView": "Needs a newer UniView"}


class PluginsPage(QWidget):
    """Installed vs. available plugins, Install / Update, and a restart banner."""

    restart_requested = Signal()
    manager_requested = Signal()   # open Options -> Plugin manager (switch off / remove / install from a file)
    _fetched = Signal(object, str)  # plugins.json dict (or None), error text

    def __init__(self, settings, parent=None, plugins_dir=PLUGINS_DIR, index_url=PLUGINS_INDEX_URL):
        super().__init__(parent)
        self.settings = settings
        self.plugins_dir = plugins_dir
        self.index_url = index_url
        self.index = plugin_index.cached_index()
        self.available = self._parse(self.index)
        self.installed_now = {}   # file -> version installed this session
        self._fetching = False
        self._fetched.connect(self._on_fetched)

        title = role(QLabel("Plugins"), "title")
        hint = role(QLabel("UniView reads Unity games and loose files by itself. Every other engine or game "
                           "(Source, Unreal, Terraria, ...) needs a plugin. Install one here, or drop a plugin "
                           "<code>.py</code> into the plugins folder by hand."), "muted")
        hint.setWordWrap(True)
        notice = QLabel(NOTICE.format(app=APP_SHORT, repo=PLUGINS_REPO_URL))
        notice.setWordWrap(True)
        notice.setOpenExternalLinks(True)
        notice.setTextFormat(Qt.RichText)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Plugin", "Games", "Installed", "Available", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.itemDoubleClicked.connect(lambda *_: self._install_current())
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.status = role(QLabel(), "muted")
        self.status.setWordWrap(True)
        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self._install_current)
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self.fetch)
        manager = QPushButton("Plugin manager...")
        manager.setToolTip("Switch plugins off, remove them, or install one from a .py / .zip file")
        manager.clicked.connect(self.manager_requested.emit)
        folder = QPushButton("Open plugins folder")
        folder.clicked.connect(open_plugins_folder)
        buttons = QHBoxLayout()
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(manager)
        buttons.addWidget(folder)

        self.banner = QLabel("Restart UniView to load the new plugins.")
        self.restart_button = QPushButton("Restart now")
        self.restart_button.clicked.connect(self.restart_requested.emit)
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner, 1)
        banner_row.addWidget(self.restart_button)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 12)
        lay.setSpacing(6)
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addSpacing(4)
        lay.addWidget(notice)
        lay.addSpacing(6)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.status)
        lay.addLayout(banner_row)
        lay.addLayout(buttons)
        self._fill()

    # ---- the list

    @staticmethod
    def _parse(index):
        if not index:
            return []
        try:
            return plugin_index.parse_index(index)
        except plugin_index.PluginIndexError as e:
            log.warning("Ignoring the plugin list: %s", e)
            return []

    def fetch(self):
        """Download plugins.json in the background."""
        if self._fetching:
            return
        self._fetching = True
        self.refresh_button.setEnabled(False)
        self.status.setText("Getting the plugin list ...")

        def work():
            try:
                self._fetched.emit(plugin_index.fetch_index(self.index_url), "")
            except Exception as e:
                self._fetched.emit(None, f"{type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True, name="plugin-index").start()

    def _on_fetched(self, index, error):
        self._fetching = False
        self.refresh_button.setEnabled(True)
        if index is not None:
            self.index, self.available = index, self._parse(index)
            log.info("Plugin list: %d plugins available", len(self.available))
        else:
            log.info("Couldn't get the plugin list (%s)%s", error, " - showing the saved one" if self.index else "")
        self._fill(error)

    def rows(self):
        return plugin_index.rows(self.available, engines.load_report(), self.installed_now, API_VERSION)

    def _fill(self, error=""):
        current = self.tree.currentItem()
        current_file = current.data(0, Qt.UserRole).file if current else None
        self.tree.clear()
        colors = {"update": QColor("#ffb74d"), "failed": QColor("#ef5350"), "installed": QColor("#66bb6a"),
                  "needs newer UniView": QColor("#9e9e9e")}
        for row in self.rows():
            state = STATE_TEXT.get(row.state, row.state)
            if row.pending:
                state = "Installed - restart to load"
            item = QTreeWidgetItem([row.name, ", ".join(row.games), row.installed or "-", row.available or "-",
                                    state])
            item.setData(0, Qt.UserRole, row)
            tip = row.description + (f"\n\nFile: {row.file}" if row.file else "")
            for col in range(5):
                item.setToolTip(col, tip.strip())
            if row.state in colors:
                item.setForeground(4, QBrush(colors[row.state]))
            self.tree.addTopLevelItem(item)
            if row.file == current_file:
                self.tree.setCurrentItem(item)
        if error and not self.available:
            self.status.setText(f"Couldn't get the plugin list from GitHub ({error}). Check your internet "
                                f"connection and click Refresh list, or download plugins from {PLUGINS_REPO_URL}.")
        elif error:
            self.status.setText(f"Couldn't refresh the plugin list ({error}); showing the one from last time.")
        elif not self.available:
            self.status.setText("Click Refresh list to see the plugins you can install.")
        else:
            rows = self.rows()
            updates = sum(r.state == "update" for r in rows)
            failed = sum(r.state == "failed" and r.entry is not None for r in rows)
            self.status.setText(f"{len(self.available)} plugins available"
                                + (f", {updates} update{'s' if updates != 1 else ''}" if updates else "") + "."
                                + (" Plugins that fail to load were probably made for an older UniView: select "
                                   "them and click Reinstall." if failed else ""))
        self._update_buttons()

    def _update_buttons(self):
        item = self.tree.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        can = bool(row and row.entry and row.state not in ("needs newer UniView",))
        self.install_button.setEnabled(can)
        self.install_button.setText({"update": "Update", "installed": "Reinstall", "failed": "Reinstall",
                                     "switched off": "Reinstall"}.get(row.state if row else "", "Install"))
        restart = bool(self.installed_now)
        self.banner.setVisible(restart)
        self.restart_button.setVisible(restart)

    def show_plugin(self, plugin_id):
        """Select the row of a plugin (when a game needs it) and make sure the list is fresh."""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            row = item.data(0, Qt.UserRole)
            if row.entry and row.entry.id == plugin_id:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                break
        if not self.available:
            self.fetch()

    def available_entry(self, plugin_id):
        return plugin_index.entry_for(self.available, plugin_id)

    # ---- install

    def _install_current(self):
        item = self.tree.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        if row and row.entry and row.state != "needs newer UniView":
            self.install(row.entry)

    def install(self, entry):
        text = (f"Install {entry.name} {entry.version} ({entry.file})?\n\n"
                f"It will be downloaded from {PLUGINS_REPO_URL} into the plugins folder.\n\n"
                "Plugins are third-party code: this one will run on your computer, inside "
                f"{APP_SHORT}, with the same access to your files as {APP_SHORT}. It is not made by or "
                "affiliated with the game's developer or publisher. Only install plugins you trust.")
        if QMessageBox.question(self, "Install plugin", text) != QMessageBox.Yes:
            return False
        self.status.setText(f"Downloading {entry.file} ...")
        self.repaint()
        try:
            files = plugin_index.download(entry, plugin_index.base_url(self.index, self.index_url))
        except Exception as e:
            log.warning("Downloading plugin %s failed: %s", entry.file, e)
            QMessageBox.warning(self, "Install plugin", f"Couldn't download {entry.file}:\n\n{e}")
            self._fill()
            return False
        try:
            plugin_manager.install(files, self.plugins_dir)
        except OSError as e:
            QMessageBox.warning(self, "Install plugin", f"Couldn't write to the plugins folder:\n\n{e}")
            self._fill()
            return False
        self.installed_now[entry.file] = entry.version
        self._note_installed(entry.file)
        # A plugin that was switched off and is installed again should load.
        if entry.file in self.settings.disabled_plugins:
            self.settings.disabled_plugins = [k for k in self.settings.disabled_plugins if k != entry.file]
            self.settings.save()
        log.info("Installed plugin %s %s from the plugin list (%s)", entry.file, entry.version,
                 os.path.join(self.plugins_dir, entry.file))
        self._fill()
        return True

    @staticmethod
    def _note_installed(fname):
        """Tell the plugin manager window too, so it shows the restart banner."""
        from uniview.ui.plugin_dialog import PluginManagerDialog
        if fname in PluginManagerDialog._removed:
            PluginManagerDialog._removed.remove(fname)
        if fname not in PluginManagerDialog._installed:
            PluginManagerDialog._installed.append(fname)
