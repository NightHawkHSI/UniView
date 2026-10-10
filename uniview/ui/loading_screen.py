"""Game loading screen in the style of the old Garry's Mod one: dots orbit the app icon, the game's real files
float around it (one icon per file, sized by how big it is), the one being read sheds bits into the centre, and
each finished file gets swallowed. While the plugin indexes, the assets it finds fly out to a ring of counters.

The drawing and animation live in loading_screen.qml and run in Qt's own JavaScript engine, rendered by Qt Quick.
The loader thread holds Python's GIL for long stretches, so nothing on the UI thread may wait for Python while a
game loads: attach() connects the Loader's signals straight to the QML side (signal to signal, no Python in
between). A Python paintEvent used to stutter with 250-1000 ms hitches.

Plugins announce their files with progress.files() / progress.file() and their finds with progress.found()
(engines/sdk.py). Plugins that only report done/total get that many anonymous icons instead."""

import os

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget

from engines.sdk import KIND_LABELS, KINDS
from uniview.constants import ICON_FILE, log
from uniview.ui import theme

QML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loading_screen.qml")
KIND_STYLE = {  # kind -> (icon label, colour)
    "model": ("MESH", "#3fb950"), "scene": ("SCENE", "#2fbf71"), "texture": ("TEX", "#4c8dff"),
    "sprite": ("SPR", "#26c6da"), "animation": ("ANIM", "#ff7b54"), "controller": ("CTRL", "#f778ba"),
    "font": ("FONT", "#b388ff"), "video": ("VID", "#f85149"), "data": ("DATA", "#8b9099"),
    "text": ("TXT", "#a0a7b4"), "audio": ("WAV", "#d29922"), "file": ("FILE", "#8b9099"),
}
THEME_TOKENS = ("BG", "TEXT", "MUTED", "FAINT", "ACCENT", "RAISED", "BORDER_STRONG")


class _Bridge(QObject):
    """What the QML listens to. Loader signals connect to these directly (see LoadingScreen.attach)."""
    event = Signal(str, "QVariant")   # config / start / stop / queue / at / found
    stage = Signal(str)               # a loader message
    progress = Signal(int, int)       # done, total


class LoadingScreen(QQuickWidget):
    def __init__(self, parent=None):
        # The 3D view and other widgets are OpenGL; Qt Quick would pick Direct3D on Windows, and one window can't
        # composite both. Must be set before the first Qt Quick window exists.
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
        super().__init__(parent)
        self._active = False
        self.bridge = _Bridge(self)
        self.rootContext().setContextProperty("bridge", self.bridge)
        self.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.setClearColor(QColor(theme.BG or "#16171a"))
        self.setSource(QUrl.fromLocalFile(QML_FILE))
        for error in self.errors():
            log.warning("Loading screen QML: %s", error.toString())

    def attach(self, loader):
        """Feed this screen from a workers.Loader without any Python on the UI thread."""
        loader.progress.connect(self.bridge.stage)
        loader.progress_value.connect(self.bridge.progress)
        loader.files_event.connect(self.bridge.event)

    # ---- driven by the main window
    def is_active(self):
        return self._active

    def start(self, title):
        self.setClearColor(QColor(theme.BG or "#16171a"))
        self.bridge.event.emit("config", {
            "theme": {k: v for k in THEME_TOKENS if (v := getattr(theme, k, ""))},
            "kindStyle": {k: list(v) for k, v in KIND_STYLE.items()},
            "kindLabels": dict(KIND_LABELS),
            "kindOrder": list(KINDS),
            "icon": QUrl.fromLocalFile(ICON_FILE).toString() if os.path.isfile(ICON_FILE) else "",
        })
        self._active = True
        self.bridge.event.emit("start", title)

    def stop(self):
        self._active = False
        self.bridge.event.emit("stop", None)

    # The same events by hand (tests, or callers without a Loader).
    def add_stage(self, text):
        self.bridge.stage.emit(text)

    def set_progress(self, done, total):
        self.bridge.progress.emit(int(done), int(total))

    def files_event(self, event, payload):
        if event == "queue":
            payload = [[str(name), int(size)] for name, size in payload]
        self.bridge.event.emit(event, payload)
