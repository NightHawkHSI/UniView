"""Search inside files: a small window listing the assets whose contents contain some text."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from uniview.content_search import TEXT_KINDS, ContentSearch
from uniview.ui.theme import role

MAX_RESULTS = 5000


class ContentSearchDialog(QDialog):
    goto_requested = Signal(object)  # asset key

    def __init__(self, session_fn, parent=None):
        """session_fn() -> the game session to search (the one open when Search is pressed)."""
        super().__init__(parent)
        self.setWindowTitle("Search inside files")
        self.resize(900, 560)
        self.session_fn = session_fn
        self.search = ContentSearch()
        self.search.hit.connect(self._add_hit)
        self.search.progress.connect(self._progress)
        self.search.finished.connect(self._finished)
        self.results = 0

        self.query = QLineEdit(placeholderText="Text to find inside the files, e.g. an ID, a line of dialogue, a key...")
        self.query.returnPressed.connect(self.start)
        self.go = QPushButton("Search")
        self.go.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.search.stop)
        self.stop_btn.setEnabled(False)
        self.all_kinds = QCheckBox("Also models, textures and audio (slow)")
        self.all_kinds.setToolTip("Normally only text, data and other files are searched - pixels, sound and\n"
                                  "meshes rarely contain readable text.")
        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(self.go)
        top.addWidget(self.stop_btn)

        self.status = QLabel("Matches ignore upper/lower case and find UTF-8 and UTF-16 text.")
        role(self.status, "muted")
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Asset", "Kind", "Found"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        header = self.list.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.list.setColumnWidth(0, 340)
        self.list.setColumnWidth(1, 70)
        self.list.itemActivated.connect(self._open)
        self.list.itemDoubleClicked.connect(self._open)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.all_kinds)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)

    def start(self):
        text = self.query.text().strip()
        session = self.session_fn()
        if not text or session is None:
            self.status.setText("Open a game and type something to find." if session is None else "Type something to find.")
            return
        self.list.clear()
        self.results = 0
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Searching...")
        self.search.start(session, text, None if self.all_kinds.isChecked() else TEXT_KINDS)

    def _add_hit(self, asset, hits):
        self.results += 1
        if self.results > MAX_RESULTS:
            return
        found = "   |   ".join(s for _off, s in hits)
        item = QTreeWidgetItem([asset.name, asset.kind, found])
        item.setData(0, Qt.UserRole, asset.key)
        item.setToolTip(2, "\n\n".join(f"at byte {off:,}:\n{s}" for off, s in hits))
        self.list.addTopLevelItem(item)

    def _progress(self, done, total):
        self.status.setText(f"Searching... {done:,} of {total:,} assets, {self.results:,} with a match")

    def _finished(self, found, searched, cancelled):
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        more = f" (showing the first {MAX_RESULTS:,})" if found > MAX_RESULTS else ""
        self.status.setText(f"{'Stopped' if cancelled else 'Done'}: {found:,} asset(s) with a match{more}, "
                            f"{searched:,} searched. Double-click one to open it.")

    def _open(self, item, _column=0):
        key = item.data(0, Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)

    def closeEvent(self, event):
        self.search.stop()
        super().closeEvent(event)
