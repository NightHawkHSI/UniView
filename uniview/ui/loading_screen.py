"""Game loading screen in the style of the old Garry's Mod one: dots orbit the app icon, the game's real files
float around it (one icon per file, sized by how big it is), the one being read sheds bits into the centre, and
each finished file gets swallowed. While the plugin indexes, the assets it finds fly out to a ring of counters.

The drawing and animation live in loading_screen.qml and run in Qt's own JavaScript engine, rendered by Qt Quick.
The loader thread holds Python's GIL for long stretches, so nothing on the UI thread may wait for Python while a
game loads: attach() connects the Loader's signals straight to the QML side (signal to signal, no Python in
between). A Python paintEvent used to stutter with 250-1000 ms hitches.

Plugins announce their files with progress.files() / progress.file() and their finds with progress.found()
(engines/sdk.py). Plugins that only report done/total get that many anonymous icons instead.

Sounds (Sounds/ folder, settings in Options > Loading screen sounds): a pop when a file starts being read, a drop
when it's swallowed, and a looping 1s-and-0s hum while its bits stream into the centre. QML plays them itself."""

import ctypes
import glob
import os

import PySide6
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget

try:  # only used from QML, but importing it here makes PyInstaller bundle the multimedia plugins (ffmpeg backend)
    from PySide6 import QtMultimedia  # noqa: F401
except ImportError:  # no sound then; the QML copes
    pass

from engines.sdk import KIND_LABELS, KINDS
from uniview.constants import ICON_FILE, SOUNDS_DIR, log
from uniview.ui import theme

QML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loading_screen.qml")
KIND_STYLE = {  # kind -> (icon label, colour)
    "model": ("MESH", "#3fb950"), "scene": ("SCENE", "#2fbf71"), "texture": ("TEX", "#4c8dff"),
    "sprite": ("SPR", "#26c6da"), "animation": ("ANIM", "#ff7b54"), "controller": ("CTRL", "#f778ba"),
    "font": ("FONT", "#b388ff"), "video": ("VID", "#f85149"), "data": ("DATA", "#8b9099"),
    "text": ("TXT", "#a0a7b4"), "audio": ("WAV", "#d29922"), "file": ("FILE", "#8b9099"),
}
THEME_TOKENS = ("BG", "TEXT", "MUTED", "FAINT", "ACCENT", "RAISED", "BORDER_STRONG")
SOUNDS = (  # QML name, file in Sounds/, settings option key, menu text
    ("pop", "pop.mp3", "sound_pop", "File starts loading (pop)"),
    ("drop", "Drop.mp3", "sound_drop", "File pulled into the centre (drop)"),
    ("bits", "1And0s.mp3", "sound_bits", "Reading 1s and 0s (loop)"),
)


def sound_config(settings):
    """{"volume": 0..1, "files": {name: file URL or ""}} for the QML side; "" = muted or missing."""
    files = {}
    for name, filename, key, _text in SOUNDS:
        path = os.path.join(SOUNDS_DIR, filename)
        on = settings.loading_sounds and settings.option(key) and os.path.isfile(path)
        files[name] = QUrl.fromLocalFile(path).toString() if on else ""
    return {"volume": settings.sound_volume, "files": files}


def _quiet_ffmpeg():
    """Qt plays the mp3s through its bundled FFmpeg, which prints decoder chatter ("[mp3float @ ...] Could not update
    timestamps for skipped samples.") straight to stderr. Turn its log down to nothing - Qt reports real errors itself."""
    for dll in glob.glob(os.path.join(os.path.dirname(PySide6.__file__), "*avutil*")):
        try:
            ctypes.CDLL(dll).av_log_set_level(-8)  # AV_LOG_QUIET; same module Qt loads, so it applies to Qt's decoding
            return
        except (OSError, AttributeError) as e:
            log.debug("Couldn't quieten FFmpeg (%s): %s", dll, e)


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
        _quiet_ffmpeg()
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

    def set_sounds(self, config):
        """Apply sound_config(settings); safe to call mid-load."""
        self.bridge.event.emit("sounds", config)

    def preview_sound(self):
        """Play the pop once at the current volume (after the volume slider moves)."""
        self.bridge.event.emit("preview", None)

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
