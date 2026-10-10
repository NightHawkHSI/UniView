"""The log console dock."""

import logging
import logging.handlers

from PySide6.QtGui import QColor, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QCheckBox, QDockWidget, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from uniview.constants import CRASH_FILE, LOG_FILE
from uniview.ui import theme
from uniview.util import open_path


class ConsoleDock(QDockWidget):
    """Bottom panel showing the log as it happens."""

    COLORS = {logging.DEBUG: "FAINT", logging.WARNING: "WARNING", logging.ERROR: "DANGER"}  # theme tokens

    def __init__(self, handler, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("console")
        self.handler = handler
        self.text = QPlainTextEdit(readOnly=True, objectName="consoleText")
        self.text.setMaximumBlockCount(5000)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)

        debug = QCheckBox("Show debug")
        debug.setToolTip("Also show low-level messages (e.g. assets that have no thumbnail).")
        debug.toggled.connect(lambda on: handler.setLevel(logging.DEBUG if on else logging.INFO))
        clear = QPushButton("Clear")
        clear.clicked.connect(self.text.clear)
        open_log = QPushButton("Open log file")
        open_log.clicked.connect(lambda: open_path(LOG_FILE))
        open_crash = QPushButton("Open crash log")
        open_crash.clicked.connect(lambda: open_path(CRASH_FILE))

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        for btn in (clear, open_log, open_crash):
            btn.setFlat(True)
        for w in (debug, clear):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(open_log)
        bar.addWidget(open_crash)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(8, 0, 8, 6)
        lay.addLayout(bar)
        lay.addWidget(self.text)
        self.setWidget(body)
        handler.bridge.html.connect(self.text.appendHtml)  # straight to C++: no Python on the UI thread per line

    def append(self, text, level):
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        fmt = QTextCharFormat()
        # Plain lines get no colour of their own, so they follow the palette after a theme switch.
        if level >= logging.CRITICAL:
            fmt.setForeground(QColor("#ff4081"))
        elif level in self.COLORS:
            fmt.setForeground(QColor(getattr(theme, self.COLORS[level])))
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text, fmt)
        if at_bottom:
            bar.setValue(bar.maximum())
