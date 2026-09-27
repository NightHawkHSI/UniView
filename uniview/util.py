"""Small helpers shared by the UI and workers."""

import json
import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QMessageBox

from uniview.constants import log


def norm_path(path):
    return os.path.normcase(os.path.abspath(path))

def pil_to_qimage(img):
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    return QImage(data, img.width, img.height, QImage.Format_RGBA8888).copy()

def pil_to_pixmap(img):
    return QPixmap.fromImage(pil_to_qimage(img))

def safe_filename(name):
    cleaned = "".join(c if c.isalnum() or c in " ._-#" else "_" for c in name)
    return cleaned.strip(" ._") or "unnamed"

def fmt_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def read_json(path, what):
    """Parsed JSON file, or None if it's missing. A corrupt file is logged and copied to <path>.bad
    so the next save doesn't silently replace what the user had."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("Could not read the %s file %s: %s", what, path, e)
        try:
            shutil.copyfile(path, path + ".bad")
            log.error("A copy of it was kept as %s", path + ".bad")
        except OSError:
            pass
        return None

def write_json(path, data):
    """Write JSON atomically: serialize first, then replace the file, so a failure never leaves it truncated."""
    text = json.dumps(data, indent=2)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

def fmt_distance(value):
    """Short number for a speed/distance in scene units (1.2k, 35, 0.04)."""
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.3g}"

def wheel_steps(event):
    """Scroll amount in 'notches' (mouse wheel = 1 per notch, touchpads give fractions)."""
    delta = event.angleDelta().y() or event.pixelDelta().y() * 4
    return delta / 120

def checker_brush(cell=10):
    pix = QPixmap(cell * 2, cell * 2)
    pix.fill(QColor("#555"))
    painter = QPainter(pix)
    painter.fillRect(0, 0, cell, cell, QColor("#444"))
    painter.fillRect(cell, cell, cell, cell, QColor("#444"))
    painter.end()
    return QBrush(pix)

def open_path(path):
    if not os.path.exists(path):
        QMessageBox.information(None, "Nothing yet", f"{os.path.basename(path)} does not exist yet "
                                "(nothing has been written to it).")
        return
    os.startfile(path)

def blank_icon(size):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    return QIcon(pix)
