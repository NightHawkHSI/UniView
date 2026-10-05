"""Duplicate finder window: groups of identical assets, and a switch to hide the extra copies in the list."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from uniview.duplicates import DuplicateScan, extra_copies, wasted
from uniview.util import fmt_size
from uniview.ui.theme import role

MAX_GROUPS = 5000


class DuplicatesDialog(QDialog):
    goto_requested = Signal(object)    # asset key
    hide_changed = Signal(object)      # set of uids to hide in the asset list (empty: hide none)

    def __init__(self, session_fn, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Duplicate assets")
        self.resize(900, 600)
        self.session_fn = session_fn
        self.session = None
        self.groups = {}
        self.scan = DuplicateScan()
        self.scan.progress.connect(self._progress)
        self.scan.finished.connect(self._finished)

        self.go = QPushButton("Find duplicates")
        self.go.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.scan.stop)
        self.stop_btn.setEnabled(False)
        self.hide = QCheckBox("Hide the extra copies in the asset list")
        self.hide.setToolTip("Keeps the first copy of each group in the list and hides the rest.")
        self.hide.setEnabled(False)
        self.hide.toggled.connect(self._hide_toggled)
        top = QHBoxLayout()
        top.addWidget(self.go)
        top.addWidget(self.stop_btn)
        top.addStretch()
        top.addWidget(self.hide)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Asset", "Kind", "Size", "File"])
        self.list.setColumnWidth(0, 380)
        self.list.setColumnWidth(1, 70)
        self.list.setColumnWidth(2, 90)
        self.list.itemDoubleClicked.connect(self._open)
        self.list.itemActivated.connect(self._open)
        self.status = QLabel("Compares the content of every asset (names and where they're stored don't matter).")
        role(self.status, "muted")

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)

    def start(self):
        session = self.session_fn()
        if session is None:
            self.status.setText("Open a game first.")
            return
        if self.hide.isChecked():
            self.hide.setChecked(False)
        self.session = session
        self.list.clear()
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.hide.setEnabled(False)
        self.status.setText("Fingerprinting assets...")
        self.scan.start(session)

    def _progress(self, done, total):
        self.status.setText(f"Fingerprinting assets... {done:,} of {total:,}")

    def _finished(self, groups, hashed, cancelled):
        self.groups = groups
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.hide.setEnabled(bool(groups))
        self.list.setUpdatesEnabled(False)
        try:
            for copies in list(groups.values())[:MAX_GROUPS]:
                first = copies[0]
                top = QTreeWidgetItem([f"{len(copies)} copies: {first.name}", first.kind,
                                       fmt_size(first.size), f"{fmt_size(wasted(copies))} extra"])
                top.setData(0, Qt.UserRole, first.key)
                for a in copies:
                    child = QTreeWidgetItem([a.name, a.kind, fmt_size(a.size), a.source or ""])
                    child.setData(0, Qt.UserRole, a.key)
                    top.addChild(child)
                self.list.addTopLevelItem(top)
        finally:
            self.list.setUpdatesEnabled(True)
        extra = sum(len(c) - 1 for c in groups.values())
        total = sum(wasted(c) for c in groups.values())
        self.status.setText(f"{'Stopped early: ' if cancelled else ''}{len(groups):,} group(s) of identical assets, "
                            f"{extra:,} extra copies taking {fmt_size(total)} ({hashed:,} assets compared). "
                            "Double-click one to open it.")

    def _hide_toggled(self, on):
        self.hide_changed.emit(extra_copies(self.groups) if on else set())

    def _open(self, item, _column=0):
        key = item.data(0, Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)

    def closeEvent(self, event):
        self.scan.stop()
        super().closeEvent(event)
