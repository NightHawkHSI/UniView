"""Logging to viewer.log and the console dock, plus crash handlers (crash.log)."""

import faulthandler
import logging
import logging.handlers
import sys
import threading
import traceback
from datetime import datetime

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from uniview.constants import CRASH_FILE, LOG_FILE, log


class LogBridge(QObject):
    """Carries log lines from any thread to the console widget (queued to the UI thread)."""

    message = Signal(str, int)  # formatted text, level number

class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.bridge = LogBridge()

    def emit(self, record):
        try:
            self.bridge.message.emit(self.format(record), record.levelno)
        except RuntimeError:
            pass  # console already destroyed (app closing)

def setup_logging():
    """Log to viewer.log (rotating) and to the in-app console. Returns the console handler."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    console_handler = QtLogHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console_handler)
    for noisy in ("PIL", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return console_handler

_crash_stream = None  # kept open for faulthandler

def write_crash(kind, exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(CRASH_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {kind} =====\n{text}")
    except OSError:
        pass
    log.critical("%s - saved to crash.log\n%s", kind, text.rstrip())

def install_crash_handlers():
    """Python errors -> crash.log + dialog; hard crashes (segfaults) -> crash.log via faulthandler."""
    global _crash_stream

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        write_crash("Unhandled error", exc_type, exc, tb)
        app = QApplication.instance()
        if app is not None:
            try:
                QMessageBox.critical(app.activeWindow(), "Something went wrong",
                                     f"{exc_type.__name__}: {exc}\n\nDetails were saved to:\n{CRASH_FILE}")
            except Exception:
                pass

    def thread_excepthook(args):
        write_crash(f"Unhandled error in thread '{args.thread.name if args.thread else '?'}'",
                    args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:
        _crash_stream = open(CRASH_FILE, "a", encoding="utf-8")
        faulthandler.enable(_crash_stream, all_threads=True)
    except OSError:
        pass
