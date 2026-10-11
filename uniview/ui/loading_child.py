"""The loading screen's own process (`unity_viewer.py --loading-screen`).

While a game loads, the loader thread holds the main process's GIL for long stretches, and anything on the main
UI thread that needs Python waits for it. Running the QML in a second process gives it its own GIL and its own UI
thread, so it keeps animating however busy the loader is. The main window sends it events as JSON lines on stdin
(see loading_screen.LoadingScreen) and gives it a native child window to draw into ("embed").

Kept to PySide6 + the standard library so it starts quickly: nothing from engines/ or the rest of uniview/."""

import ctypes
import glob
import json
import os
import sys
import threading

QML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loading_screen.qml")
TICK_MS = 50  # how often the real reading progress is sampled


def quiet_ffmpeg():
    """Qt plays the mp3s through its bundled FFmpeg, which prints decoder chatter ("[mp3float @ ...] Could not update
    timestamps for skipped samples.") straight to stderr. Turn its log down to nothing - Qt reports real errors itself."""
    import PySide6
    for dll in glob.glob(os.path.join(os.path.dirname(PySide6.__file__), "*avutil*")):
        try:
            ctypes.CDLL(dll).av_log_set_level(-8)  # AV_LOG_QUIET; same module Qt loads, so it applies to Qt's decoding
            return
        except (OSError, AttributeError):
            pass


def _std_stream(name, std_handle, mode):
    """sys.stdin / sys.stderr, or the same pipe opened by hand: a windowed (--windowed) exe gets None for them even
    when the parent handed it pipes."""
    stream = getattr(sys, name)
    if stream is not None:
        return open(stream.fileno(), mode, encoding="utf-8", closefd=False)
    if sys.platform == "win32":
        import msvcrt
        get = ctypes.windll.kernel32.GetStdHandle
        get.restype = ctypes.c_void_p
        handle = get(std_handle)
        if handle not in (None, 0, ctypes.c_void_p(-1).value):
            return open(msvcrt.open_osfhandle(handle, os.O_RDONLY if "r" in mode else os.O_WRONLY), mode,
                        encoding="utf-8")
    return None


def io_counter(pid):
    """A function returning how many bytes process `pid` has read so far (the OS's own count), or None if this
    platform can't tell."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoCounters)]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        counters = IoCounters()

        def read_bytes():
            return counters.read_bytes if kernel32.GetProcessIoCounters(handle, ctypes.byref(counters)) else None
        return read_bytes if read_bytes() is not None else None
    path = f"/proc/{pid}/io"
    if os.path.isfile(path):
        def read_bytes():
            try:
                with open(path) as f:
                    for line in f:
                        if line.startswith("rchar:"):
                            return int(line.split()[1])
            except (OSError, ValueError):
                return None
            return None
        return read_bytes if read_bytes() is not None else None
    return None


class ReadWatch:
    """What the main process is really reading: bytes read (OS counter) since the current file started, against
    the file's size, plus the actual bytes of the file at that point - the 1s and 0s the screen shows."""

    SAMPLE = 8  # bytes per tick -> 64 bits

    def __init__(self, counter):
        self.counter = counter
        self.paths, self.sizes = [], []
        self.index, self.base, self.last = -1, 0, 0
        self.handle = None
        self.lock = threading.Lock()  # events come in on the stdin thread, ticks on the UI thread

    def queue(self, entries):
        with self.lock:
            self.paths = [str(e[2]) if len(e) > 2 else "" for e in entries]
            self.sizes = [int(e[1] or 0) for e in entries]
        self.at(-1)

    def at(self, index):
        with self.lock:
            self._at(index)

    def _at(self, index):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        self.index = int(index)
        if 0 <= self.index < len(self.paths):
            self.base = self.last = self.counter() or 0
            try:
                self.handle = open(self.paths[self.index], "rb") if self.paths[self.index] else None
            except OSError:
                self.handle = None

    def tick(self):
        """{"i", "frac", "bytes" (read since the last tick), "bits"} for the file being read, or None."""
        with self.lock:
            return self._tick()

    def _tick(self):
        if not 0 <= self.index < len(self.sizes):
            return None
        now = self.counter()
        if now is None:
            return None
        delta, self.last = max(0, now - self.last), now
        size = self.sizes[self.index]
        done = min(size, max(0, now - self.base)) if size else 0
        bits = ""
        if self.handle is not None:
            try:
                end = self.handle.seek(0, 2)
                self.handle.seek(max(0, min(done, end) - self.SAMPLE))
                bits = "".join(f"{b:08b}" for b in self.handle.read(self.SAMPLE))
            except (OSError, ValueError):
                bits = ""
        return {"i": self.index, "frac": done / size if size else 0.0, "bytes": delta, "bits": bits}


def host_client_size(hwnd):
    """(width, height) in physical pixels of a window's client area (Windows), or None."""
    if not hwnd or sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        return (w, h) if w > 0 and h > 0 else None
    except (OSError, AttributeError):
        return None


def make_bridge():
    from PySide6.QtCore import QObject, Signal

    class Bridge(QObject):
        """What the QML listens to."""
        event = Signal(str, "QVariant")   # config / start / stop / queue / at / found / sounds / preview
        stage = Signal(str)               # a loader message
        progress = Signal(int, int)       # done, total
        settled = Signal()                # from the QML: after "finish", everything on screen has played out

    return Bridge


class _Commands:
    """stdin lines -> Qt signals. Runs on a reader thread; the signals queue over to the UI thread."""

    def __init__(self, bridge, control):
        self.bridge, self.control = bridge, control

    def run(self, stream):
        for line in stream:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("k")
            if kind == "event":
                self.bridge.event.emit(msg["n"], msg.get("p"))
            elif kind == "stage":
                self.bridge.stage.emit(msg["t"])
            elif kind == "progress":
                self.bridge.progress.emit(int(msg["d"]), int(msg["t"]))
            elif kind in ("embed", "size", "bg", "snapshot"):
                self.control.emit(kind, msg.get("v"))
            elif kind == "quit":
                break
        self.control.emit("quit", None)  # stdin closed: the main window is gone


def child_main():
    from PySide6.QtCore import QObject, Qt, QUrl, Signal
    from PySide6.QtGui import QColor, QGuiApplication, QWindow
    from PySide6.QtQuick import QQuickView, QQuickWindow, QSGRendererInterface

    try:  # sound; the QML copes without it
        from PySide6 import QtMultimedia  # noqa: F401
    except ImportError:
        pass

    # A plain child HWND drawn with OpenGL embeds cleanly in another process's window; Direct3D flip-model
    # swap chains on child windows are less dependable.
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    app = QGuiApplication(sys.argv)
    quiet_ffmpeg()

    class Control(QObject):
        command = Signal(str, "QVariant")

    bridge = make_bridge()()
    control = Control()
    view = QQuickView()
    view.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowTransparentForInput
                  | Qt.WindowType.WindowDoesNotAcceptFocus)
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.setColor(QColor("#16171a"))
    view.rootContext().setContextProperty("bridge", bridge)
    view.setSource(QUrl.fromLocalFile(QML_FILE))
    errors = _std_stream("stderr", -12, "w")
    for error in view.errors():
        if errors is not None:
            print("QML:", error.toString(), file=errors, flush=True)
    state = {"host": None, "hwnd": 0, "px": None, "applied": None}

    def fit():
        """Size the view to the host window's pixels, in this window's own scale (which can differ from the main
        process's, and change when the app moves to another monitor)."""
        px = host_client_size(state["hwnd"]) or state["px"]
        if not px:
            return
        dpr = view.devicePixelRatio() or 1.0
        geometry = (max(1, round(px[0] / dpr)), max(1, round(px[1] / dpr)))
        if geometry != state["applied"]:
            state["applied"] = geometry
            view.setGeometry(0, 0, *geometry)

    def on_command(kind, value):
        if kind == "embed":
            host = QWindow.fromWinId(int(value))
            state["host"] = host  # keep the wrapper alive
            state["hwnd"] = int(value)
            view.setParent(host)
            view.setPosition(0, 0)
            view.show()
        elif kind == "size":  # host size in physical pixels
            state["px"] = (int(value[0]), int(value[1]))
            state["applied"] = None
            fit()
        elif kind == "bg":
            view.setColor(QColor(value))
        elif kind == "snapshot":  # the current frame to a file, for the main window's fade-out
            image = view.grabWindow()
            if not image.isNull():
                part = value + ".part"
                if image.save(part, "BMP"):
                    os.replace(part, value)
        elif kind == "quit":
            app.quit()

    control.command.connect(on_command)
    from PySide6.QtCore import QTimer
    fitter = QTimer(app)  # catches scale/size changes no message announced (dragging to another monitor)
    fitter.timeout.connect(fit)
    fitter.start(250)
    replies = _std_stream("stdout", -11, "w")
    if replies is not None:  # back to the main process (LoadingScreen._reply_loop)
        def settled():
            try:
                print('{"k":"settled"}', file=replies, flush=True)
            except (OSError, ValueError):
                pass
        bridge.settled.connect(settled)
    counter = io_counter(os.getppid())
    if counter is not None:  # real reading progress; without it the QML falls back to its own guesswork
        watch = ReadWatch(counter)

        def on_event(name, payload):
            if name == "queue":
                watch.queue(payload or [])
            elif name == "at":
                watch.at(payload)
            elif name == "stop":
                watch.at(-1)

        def tick():
            info = watch.tick()
            if info is not None:
                info["dt"] = TICK_MS / 1000
                bridge.event.emit("read", info)

        bridge.event.connect(on_event)
        timer = QTimer(app)
        timer.timeout.connect(tick)
        timer.start(TICK_MS)
    if os.environ.get("UNIVIEW_FRAME_LOG") and errors is not None:  # diagnostics: frame gaps to stderr after each load
        import time
        gaps, last = [], [0.0]

        def frame():
            t = time.perf_counter()
            if last[0]:
                gaps.append(t - last[0])
            last[0] = t

        def report():
            if gaps:
                busy = [g for g in gaps if g < 0.5]  # leave out idle stretches between loads
                print(f"frames {len(gaps)} avg {1 / (sum(busy) / max(1, len(busy))):.0f} fps "
                      f"max gap {max(busy) * 1000:.0f} ms, >50 ms: {sum(g > 0.05 for g in busy)}",
                      file=errors, flush=True)
            gaps.clear()

        view.frameSwapped.connect(frame)
        bridge.event.connect(lambda name, _payload: report() if name == "stop" else None)
    commands = _std_stream("stdin", -10, "r")
    if commands is None:
        return 1
    reader = threading.Thread(target=_Commands(bridge, control.command).run,
                              args=(commands,), daemon=True, name="loading-screen-commands")
    reader.start()
    return app.exec()
