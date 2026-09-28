"""Help > Optional tools...: which helper programs are installed, with install buttons. Also shown at
startup when a recommended one (game audio decoder, script decompiler) is missing."""

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from uniview.constants import log
from uniview.tools import all_tools


class _Relay(QObject):
    progress = Signal(str, str)        # tool id, text
    finished = Signal(str, str, bool)  # tool id, text, ok


class ToolsDialog(QDialog):
    def __init__(self, settings, parent=None, at_startup=False):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Optional tools")
        self.setMinimumWidth(640)
        self.relay = _Relay()
        self.relay.progress.connect(self._on_progress)
        self.relay.finished.connect(self._on_finished)
        self.rows = {}
        intro = QLabel(("Some helper programs UniView uses aren't installed yet. " if at_startup else "")
                       + "They're optional: UniView works without them, but these features need them. "
                       "Downloads go into UniView's tools folder.")
        intro.setWordWrap(True)
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for row, tool in enumerate(all_tools(settings.blender_path)):
            text = QLabel(f"<b>{tool.name}</b>{'' if tool.recommended else '  <i>(optional)</i>'}<br>{tool.purpose}")
            text.setWordWrap(True)
            status = QLabel()
            status.setWordWrap(True)
            status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            button = QPushButton(tool.install_label)
            button.clicked.connect(lambda _=False, t=tool: self._install(t))
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(text)
            lay.addWidget(status)
            grid.addWidget(box, row, 0)
            grid.addWidget(button, row, 1, Qt.AlignTop)
            self.rows[tool.id] = (tool, status, button)
            self._refresh(tool.id)
        self.check = QCheckBox("Check at startup")
        self.check.setChecked(settings.check_tools)
        self.check.toggled.connect(self._set_check)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addSpacing(6)
        lay.addLayout(grid)
        lay.addSpacing(6)
        lay.addWidget(self.check)
        lay.addWidget(buttons)

    def _refresh(self, tool_id):
        tool, status, button = self.rows[tool_id]
        try:
            found = tool.status()
        except Exception as e:
            log.debug("Checking %s: %s", tool_id, e)
            found = None
        if found:
            status.setText(f"<span style='color:#5cb85c'>✔ Installed</span>  <small>{found}</small>")
            if tool.install_label == "Install":
                button.setText("Reinstall")
        else:
            status.setText("<span style='color:#e0a030'>✖ Not installed</span>"
                           + (f"  <small>(goes to {tool.where})</small>" if tool.where else ""))

    def _install(self, tool):
        _t, status, button = self.rows[tool.id]
        button.setEnabled(False)
        status.setText("Starting...")

        def work():
            try:
                result = tool.install(lambda text: self.relay.progress.emit(tool.id, text))
                self.relay.finished.emit(tool.id, f"Installed: {result}" if result else "Opened the download page", True)
            except Exception as e:
                log.exception("Installing %s failed", tool.name)
                self.relay.finished.emit(tool.id, f"Install failed: {e}", False)

        threading.Thread(target=work, daemon=True, name=f"install-{tool.id}").start()

    def _on_progress(self, tool_id, text):
        self.rows[tool_id][1].setText(text)

    def _on_finished(self, tool_id, text, ok):
        tool, status, button = self.rows[tool_id]
        button.setEnabled(True)
        if ok and tool.status():
            self._refresh(tool_id)
            log.info("%s: %s", tool.name, text)
        else:
            status.setText(text)

    def _set_check(self, on):
        self.settings.check_tools = on
        self.settings.save()
