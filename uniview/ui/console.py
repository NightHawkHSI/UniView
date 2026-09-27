"""The log console dock."""

import logging
import logging.handlers

from PySide6.QtGui import QColor, QFontDatabase, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QCheckBox, QDockWidget, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from uniview.constants import CRASH_FILE, LOG_FILE
from uniview.util import open_path


class ConsoleDock(QDockWidget):
    """Bottom panel showing the log as it happens."""

    COLORS = {logging.DEBUG: "#8a8a8a", logging.WARNING: "#e0a030",
              logging.ERROR: "#ef5350", logging.CRITICAL: "#ff4081"}

    def __init__(self, handler, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("console")
        self.handler = handler
        self.text = QPlainTextEdit(readOnly=True)
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
        for w in (debug, clear):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(open_log)
        bar.addWidget(open_crash)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 0, 4, 4)
        lay.addLayout(bar)
        lay.addWidget(self.text)
        self.setWidget(body)
        handler.bridge.message.connect(self.append)

    def append(self, text, level):
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.COLORS.get(level, self.palette().color(QPalette.Text))))
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(text, fmt)
        if at_bottom:
            bar.setValue(bar.maximum())
