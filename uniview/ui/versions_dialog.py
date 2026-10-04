"""Game versions window: save snapshots of a game's assets and compare versions (added / removed / changed)."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uniview import versions
from uniview.duplicates import DuplicateScan
from uniview.util import fmt_size

MAX_ROWS = 20000


class VersionsDialog(QDialog):
    goto_requested = Signal(object)  # asset key

    def __init__(self, game_fn, parent=None):
        """game_fn() -> (session, game name, game folder) of the open game, or None."""
        super().__init__(parent)
        self.setWindowTitle("Game versions")
        self.resize(1000, 640)
        self.game_fn = game_fn
        self.scan = DuplicateScan()
        self.scan.progress.connect(lambda d, t: self.status.setText(f"Fingerprinting assets... {d:,} of {t:,}"))
        self.scan.finished.connect(self._scanned)
        self._after_scan = None
        self.by_key = {}

        self.game_label = QLabel()
        self.label = QLineEdit(placeholderText="Label for this version, e.g. 'patch 1.2' (optional)")
        self.save_btn = QPushButton("Save snapshot of this version")
        self.save_btn.setToolTip("Fingerprints every asset of the open game (the first time takes a while)\n"
                                 "and saves the list, so a later version can be compared with it.")
        self.save_btn.clicked.connect(self.save)
        top = QHBoxLayout()
        top.addWidget(self.label, 1)
        top.addWidget(self.save_btn)

        self.snapshots = QListWidget()
        self.snapshots.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.compare_open = QPushButton("Compare with the open game")
        self.compare_open.clicked.connect(self.compare_with_open)
        self.compare_two = QPushButton("Compare the two selected")
        self.compare_two.clicked.connect(self.compare_selected)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.delete)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Saved snapshots (newest first):"))
        ll.addWidget(self.snapshots, 1)
        for b in (self.compare_open, self.compare_two, self.delete_btn):
            ll.addWidget(b)

        self.result = QTreeWidget()
        self.result.setHeaderLabels(["Asset", "Kind", "Size"])
        self.result.setColumnWidth(0, 430)
        self.result.setColumnWidth(1, 80)
        self.result.itemDoubleClicked.connect(self._open)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.result)
        split.setSizes([300, 700])

        self.status = QLabel("Save a snapshot now; after the game updates, compare the new version with it.")
        self.status.setStyleSheet("color: gray;")
        self.status.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(self.game_label)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.status)

    # ---- state
    def refresh(self):
        game = self.game_fn()
        self.snapshots.clear()
        if game is None:
            self.game_label.setText("<b>Open a game first.</b>")
            return
        session, name, folder = game
        build = versions.steam_build_id(folder)
        self.game_label.setText(f"<b>{name}</b>" + (f"  ·  Steam build {build}" if build else ""))
        if not self.label.text():
            self.label.setText(f"build {build}" if build else "")
        for path, meta in versions.list_snapshots(name):
            text = f"{meta['created'].replace('T', ' ')}  {meta['label']}\n{meta['count']:,} assets"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, path)
            self.snapshots.addItem(item)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def _busy(self, on):
        for w in (self.save_btn, self.compare_open, self.compare_two, self.delete_btn):
            w.setEnabled(not on)

    def _fingerprint(self, then):
        game = self.game_fn()
        if game is None:
            self.status.setText("Open a game first.")
            return
        self._after_scan = then
        self._busy(True)
        self.status.setText("Fingerprinting assets...")
        self.scan.start(game[0])

    def _scanned(self, _groups, hashed, cancelled):
        self._busy(False)
        then, self._after_scan = self._after_scan, None
        if cancelled or then is None:
            self.status.setText("Stopped.")
            return
        then(versions.entries(self.scan.hashes))

    # ---- actions
    def save(self):
        def done(rows):
            _session, name, _folder = self.game_fn()
            path = versions.save_snapshot(name, rows, self.label.text().strip(),
                                          getattr(self.game_fn()[0], "engine_version", ""))
            self.refresh()
            self.status.setText(f"Saved a snapshot of {len(rows):,} assets ({os.path.basename(path)}).")
        self._fingerprint(done)

    def _selected(self):
        return [i.data(Qt.UserRole) for i in self.snapshots.selectedItems()]

    def compare_with_open(self):
        chosen = self._selected()
        if len(chosen) != 1:
            self.status.setText("Select one snapshot to compare with the open game.")
            return
        old = versions.load_snapshot(chosen[0])
        session = self.game_fn()[0]
        self.by_key = {versions.asset_key(a): a for a in session.assets}
        self._fingerprint(lambda rows: self._show(old, {"label": "open game", "assets": rows}, linked=True))

    def compare_selected(self):
        chosen = self._selected()
        if len(chosen) != 2:
            self.status.setText("Select exactly two snapshots (Ctrl+click).")
            return
        a, b = (versions.load_snapshot(p) for p in chosen)
        old, new = sorted((a, b), key=lambda d: d.get("created", ""))
        self.by_key = {}
        self._show(old, new, linked=False)

    def delete(self):
        chosen = self._selected()
        if not chosen or QMessageBox.question(self, "Delete snapshots", f"Delete {len(chosen)} snapshot(s)?") \
                != QMessageBox.Yes:
            return
        for p in chosen:
            try:
                os.remove(p)
            except OSError:
                pass
        self.refresh()

    def _show(self, old, new, linked):
        d = versions.diff(old["assets"], new["assets"])
        self.result.clear()
        names = {"added": "Added", "removed": "Removed", "changed": "Changed"}
        for k in ("changed", "added", "removed"):
            rows = d[k]
            top = QTreeWidgetItem([f"{names[k]} ({len(rows):,})", "", ""])
            for kind, key, old_size, new_size in rows[:MAX_ROWS]:
                if k == "changed":
                    size = f"{fmt_size(old_size)} → {fmt_size(new_size)}"
                else:
                    size = fmt_size(new_size if k == "added" else old_size)
                child = QTreeWidgetItem([versions.key_name(key), kind, size])
                if "@" in key:
                    child.setToolTip(0, "Stored under " + key.split("@", 1)[1])
                asset = self.by_key.get(key) if linked and k != "removed" else None
                if asset is not None:
                    child.setData(0, Qt.UserRole, asset.key)
                top.addChild(child)
            self.result.addTopLevelItem(top)
            top.setExpanded(len(rows) <= 200)
        def label(snap):
            return snap.get("label") or snap.get("created", "").replace("T", " ")
        self.status.setText(f"{label(old)} → {label(new)}: {len(d['changed']):,} changed, {len(d['added']):,} added, "
                            f"{len(d['removed']):,} removed, {d['same']:,} the same."
                            + (" Double-click one to open it." if linked else ""))

    def _open(self, item, _column=0):
        key = item.data(0, Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)

    def closeEvent(self, event):
        self.scan.stop()
        super().closeEvent(event)
