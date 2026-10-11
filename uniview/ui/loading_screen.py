"""Game loading screen in the style of the old Garry's Mod one: dots orbit the app icon, the game's real files
float around it (one icon per file, sized by how big it is), the one being read sheds bits into the centre, and
each finished file gets swallowed. While the plugin indexes, the assets it finds fly out to a ring of counters.

The drawing and animation live in loading_screen.qml and run in Qt's own JavaScript engine, rendered by Qt Quick.
The loader thread holds Python's GIL for long stretches, so the QML runs in a process of its own
(loading_child.py, `unity_viewer.py --loading-screen`) with its own GIL and UI thread, drawing into a native
window inside the main one. The loader's events reach it through a queue, a writer thread and a pipe, so neither
side waits for the other. If that process can't start, the QML runs in-process instead (_InProcessView).

Plugins announce their files with progress.files() / progress.file() and their finds with progress.found()
(engines/sdk.py). Plugins that only report done/total get that many anonymous icons instead.

Sounds (Sounds/ folder, settings in Options > Loading screen sounds): a pop when a file starts being read, a drop
when it's swallowed, and a looping 1s-and-0s hum while its bits stream into the centre. QML plays them itself."""

import atexit
import json
import os
import queue
import subprocess
import sys
import threading

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

try:  # only used from QML, but importing it here makes PyInstaller bundle the multimedia plugins (ffmpeg backend)
    from PySide6 import QtMultimedia  # noqa: F401
except ImportError:  # no sound then; the QML copes
    pass

from engines.sdk import KIND_LABELS, KINDS
from uniview.constants import APP_DIR, ICON_FILE, SOUNDS_DIR, log
from uniview.ui import theme
from uniview.ui.loading_child import QML_FILE, make_bridge, quiet_ffmpeg

MAIN_SCRIPT = os.path.join(APP_DIR, "unity_viewer.py")
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


class _InProcessView(QQuickWidget):
    """The QML in this process: the fallback when the loading screen's own process can't run. Stutters while the
    loader holds the GIL, but works everywhere."""

    def __init__(self, parent=None):
        # The 3D view and other widgets are OpenGL; Qt Quick would pick Direct3D on Windows, and one window can't
        # composite both. Must be set before the first Qt Quick window exists.
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
        super().__init__(parent)
        quiet_ffmpeg()
        self.bridge = make_bridge()(self)
        self.rootContext().setContextProperty("bridge", self.bridge)
        self.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.setClearColor(QColor(theme.BG or "#16171a"))
        self.setSource(QUrl.fromLocalFile(QML_FILE))
        for error in self.errors():
            log.warning("Loading screen QML: %s", error.toString())

    def send(self, msg):
        kind = msg["k"]
        if kind == "event":
            self.bridge.event.emit(msg["n"], msg.get("p"))
        elif kind == "stage":
            self.bridge.stage.emit(msg["t"])
        elif kind == "progress":
            self.bridge.progress.emit(msg["d"], msg["t"])
        elif kind == "bg":
            self.setClearColor(QColor(msg["v"]))


class LoadingScreen(QWidget):
    """A native window the loading screen process draws into. Messages queue up in _outbox and a writer thread
    pipes them over, so the loader never waits on the pipe and a busy child just catches up."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = False
        self._fallback = None
        self._proc = None
        self._dead = threading.Event()
        self._outbox = queue.SimpleQueue()
        self._embedded = 0
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._paint_bg()
        # Started now so it's warm by the first load. UNIVIEW_INPROCESS_LOADING=1 skips it (debugging).
        if os.environ.get("UNIVIEW_INPROCESS_LOADING") or not self._spawn():
            self._use_fallback()

    # ---- the loading screen process
    def _spawn(self):
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--loading-screen"]
        elif os.path.isfile(MAIN_SCRIPT):
            cmd = [sys.executable, MAIN_SCRIPT, "--loading-screen"]
        else:
            return False
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=APP_DIR,
                env=dict(os.environ, PYINSTALLER_SUPPRESS_SPLASH_SCREEN="1"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            log.warning("Loading screen process didn't start (%s) - drawing it in-process", e)
            return False
        threading.Thread(target=self._write_loop, daemon=True, name="loading-screen-writer").start()
        threading.Thread(target=self._error_loop, daemon=True, name="loading-screen-stderr").start()
        atexit.register(self._shutdown)
        return True

    def _write_loop(self):
        pipe = self._proc.stdin
        try:
            while (line := self._outbox.get()) is not None:
                pipe.write(line)
                if self._outbox.empty():
                    pipe.flush()
            pipe.close()
        except (OSError, ValueError):  # the child is gone
            pass
        self._dead.set()

    def _error_loop(self):
        for raw in self._proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                log.warning("Loading screen process: %s", text)
        self._dead.set()

    def _shutdown(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._outbox.put(b'{"k":"quit"}\n')
            self._outbox.put(None)
            try:
                proc.wait(1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _alive(self):
        return self._proc is not None and not self._dead.is_set() and self._proc.poll() is None

    def _use_fallback(self):
        if self._fallback is not None:
            return
        if self._proc is not None:
            log.warning("Loading screen process stopped - drawing it in-process")
            self._shutdown()
        self._fallback = _InProcessView(self)
        self._layout.addWidget(self._fallback)

    def send(self, msg):
        """Queue one message for the screen. Safe from any thread and never blocks."""
        if self._fallback is not None:
            self._fallback.send(msg)
        else:
            self._outbox.put(json.dumps(msg, separators=(",", ":"), default=str).encode("utf-8") + b"\n")

    def _paint_bg(self):
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(theme.BG or "#16171a"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def _send_size(self):
        self.send({"k": "size", "v": [self.width(), self.height()]})

    def showEvent(self, event):
        super().showEvent(event)
        if self._fallback is None:
            hwnd = int(self.winId())
            if hwnd != self._embedded:  # first show, or Qt recreated the native window
                self._embedded = hwnd
                self.send({"k": "embed", "v": hwnd})
            self._send_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._embedded and self._fallback is None:
            self._send_size()

    def snapshot(self, timeout=0.4):
        """The screen's current frame as a QPixmap, rendered by its process (so windows on top don't matter), or
        None. Waits up to `timeout` seconds for it."""
        import tempfile
        import time

        from PySide6.QtGui import QPixmap
        if self._fallback is not None:
            return self._fallback.grab()
        if not self._alive():
            return None
        path = os.path.join(tempfile.gettempdir(), f"uniview-loading-{os.getpid()}.bmp")
        try:
            os.remove(path)
        except OSError:
            pass
        self.send({"k": "snapshot", "v": path})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if os.path.exists(path):
                pixmap = QPixmap(path)
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None if pixmap.isNull() else pixmap
            time.sleep(0.01)
        return None

    def attach(self, loader):
        """Feed this screen from a workers.Loader. Direct connections: the forwarding runs on the loader's own
        thread (a queue put), so nothing waits for this UI thread, which may itself be waiting for the GIL."""
        direct = Qt.ConnectionType.DirectConnection
        loader.progress.connect(self.add_stage, direct)
        loader.progress_value.connect(self.set_progress, direct)
        loader.files_event.connect(self.files_event, direct)

    # ---- driven by the main window
    def is_active(self):
        return self._active

    def start(self, title):
        if self._fallback is None and not self._alive():
            self._use_fallback()
        self._paint_bg()
        self.send({"k": "bg", "v": theme.BG or "#16171a"})
        self.send({"k": "event", "n": "config", "p": {
            "theme": {k: v for k in THEME_TOKENS if (v := getattr(theme, k, ""))},
            "kindStyle": {k: list(v) for k, v in KIND_STYLE.items()},
            "kindLabels": dict(KIND_LABELS),
            "kindOrder": list(KINDS),
            "icon": QUrl.fromLocalFile(ICON_FILE).toString() if os.path.isfile(ICON_FILE) else "",
        }})
        self._active = True
        self.send({"k": "event", "n": "start", "p": title})

    def set_sounds(self, config):
        """Apply sound_config(settings); safe to call mid-load."""
        self.send({"k": "event", "n": "sounds", "p": config})

    def preview_sound(self):
        """Play the pop once at the current volume (after the volume slider moves)."""
        self.send({"k": "event", "n": "preview", "p": None})

    def stop(self):
        self._active = False
        self.send({"k": "event", "n": "stop", "p": None})

    # The loader's events (attach() connects them; tests and callers without a Loader can call them too).
    def add_stage(self, text):
        self.send({"k": "stage", "t": str(text)})

    def set_progress(self, done, total):
        self.send({"k": "progress", "d": int(done), "t": int(total)})

    def files_event(self, event, payload):
        if event == "queue":
            payload = [[str(e[0]), int(e[1]), str(e[2]) if len(e) > 2 else ""] for e in payload]
        self.send({"k": "event", "n": event, "p": payload})


class FadeOverlay(QWidget):
    """The end of a load: a snapshot of the loading screen in a borderless window over it, faded out so the app
    appears underneath instead of popping in. A window of its own because the loading screen and the 3D view are
    native child windows that an ordinary widget can't draw over."""

    def __init__(self, target, ms=450):
        super().__init__(target.window(), Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.bg = QColor(theme.BG or "#16171a")
        top_left = target.mapToGlobal(target.rect().topLeft())
        self.setGeometry(QRect(top_left, target.size()))
        self.snapshot = target.snapshot() if hasattr(target, "snapshot") else None
        screen = target.screen()
        if self.snapshot is None and screen is not None:  # else what's on screen right now
            origin = screen.geometry().topLeft()
            shot = screen.grabWindow(0, top_left.x() - origin.x(), top_left.y() - origin.y(),
                                     target.width(), target.height())
            if not shot.isNull():
                self.snapshot = shot
        self.anim = QPropertyAnimation(self, b"windowOpacity", self)
        self.anim.setDuration(ms)
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.anim.finished.connect(self.close)
        self.setWindowOpacity(1.0)
        self.show()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.snapshot is not None:
            painter.drawPixmap(self.rect(), self.snapshot)
        else:
            painter.fillRect(self.rect(), self.bg)

    def start(self, delay=40):
        """Begin fading once the app underneath has had a moment to paint."""
        QTimer.singleShot(delay, self.anim.start)

