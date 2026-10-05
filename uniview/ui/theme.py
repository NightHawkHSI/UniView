"""The app-wide look: Fusion style, a dark or light palette, and a stylesheet built from the colour
tokens below. Widgets opt into variants with a dynamic property instead of inline colours:

    role(label, "muted")     grey secondary text      role(label, "title")    big bold heading
    role(label, "heading")   panel heading            role(label, "section")  small caps-style caption
    role(label, "warn")      orange status text       role(button, "primary") accent-filled button

Line icons (toolbar, list/grid...) are drawn here too, in the current text colours: icon(name).
Anything built from the tokens once (a 3D background, an icon) re-runs on a theme switch through
on_change(callback) / set_icon(button, name).
"""

import os
import tempfile

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

MODES = ("system", "dark", "light")

PALETTES = {
    "dark": {
        "BG": "#16171a",          # window background
        "SURFACE": "#1d1e22",     # panels, lists
        "RAISED": "#25272c",      # inputs, buttons
        "HOVER": "#2e3036",
        "PRESSED": "#363940",
        "BORDER": "#31343a",
        "BORDER_STRONG": "#41444c",
        "TEXT": "#e4e6ea",
        "MUTED": "#8b9099",
        "FAINT": "#5c6068",
        "ACCENT": "#4c8dff",
        "ACCENT_HOVER": "#6aa0ff",
        "ACCENT_SOFT": "rgba(76, 141, 255, 0.16)",
        "SELECTION": "#24395c",   # solid selected-row colour (accent over SURFACE)
        "SUCCESS": "#3fb950",
        "WARNING": "#d29922",
        "DANGER": "#f85149",
        "VIEWPORT_TOP": "#2a2c32",    # 3D view background gradient
        "VIEWPORT_BOTTOM": "#17181b",
    },
    "light": {
        "BG": "#eceef2",
        "SURFACE": "#f8f9fb",
        "RAISED": "#ffffff",
        "HOVER": "#e4e8ee",
        "PRESSED": "#d8dde5",
        "BORDER": "#d3d8e0",
        "BORDER_STRONG": "#b9c0cb",
        "TEXT": "#1c2028",
        "MUTED": "#5d6470",
        "FAINT": "#a1a7b1",
        "ACCENT": "#2f6fe4",
        "ACCENT_HOVER": "#1f5ccc",
        "ACCENT_SOFT": "rgba(47, 111, 228, 0.12)",
        "SELECTION": "#d5e2fa",
        "SUCCESS": "#1a7f37",
        "WARNING": "#9a6700",
        "DANGER": "#cf222e",
        "VIEWPORT_TOP": "#eef0f4",
        "VIEWPORT_BOTTOM": "#c8cdd6",
    },
}

# The current tokens, as module attributes (theme.SURFACE ...). apply() swaps them.
BG = SURFACE = RAISED = HOVER = PRESSED = BORDER = BORDER_STRONG = TEXT = MUTED = FAINT = ""
ACCENT = ACCENT_HOVER = ACCENT_SOFT = SELECTION = SUCCESS = WARNING = DANGER = ""
VIEWPORT_TOP = VIEWPORT_BOTTOM = ""
globals().update(PALETTES["dark"])
GOLD = "#f4c542"
RADIUS = 6
DARK = True     # whether the dark palette is the one in use
_mode = "dark"  # what the user picked (may be "system")
_callbacks = []


def _stylesheet(glyphs):
    sheet = f"""
* {{ outline: none; }}
QWidget {{ color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 6px 8px;
            border-radius: {RADIUS}px; }}

/* text roles */
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="title"] {{ font-size: 22px; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="section"] {{ color: {MUTED}; font-size: 11px; font-weight: 600; padding-top: 6px; }}
QLabel[role="warn"] {{ color: {WARNING}; }}
QLabel[role="accent"] {{ color: {ACCENT_HOVER}; }}

/* buttons */
QPushButton, QToolButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: {RADIUS}px;
                            padding: 5px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: {HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {PRESSED}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {FAINT}; background: {SURFACE}; }}
QToolButton:checked {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; color: {TEXT}; }}
QToolButton[role="icon"] {{ padding: 4px; min-width: 22px; min-height: 22px; }}
QToolButton[role="icon"]:disabled {{ background: transparent; border-color: transparent; }}
QPushButton[role="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[role="primary"]:disabled {{ background: {RAISED}; border-color: {BORDER}; color: {FAINT}; }}
QPushButton:flat {{ background: transparent; border-color: transparent; }}
QPushButton:flat:hover {{ background: {HOVER}; }}

/* inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: {RAISED}; border: 1px solid {BORDER}; border-radius: {RADIUS}px;
    selection-background-color: {ACCENT}; selection-color: white; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ padding: 5px 8px; min-height: 18px; }}
QLineEdit:hover, QComboBox:hover {{ border-color: {BORDER_STRONG}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {ACCENT}; }}
QPlainTextEdit, QTextEdit, QTextBrowser {{ background: {SURFACE}; padding: 4px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url(@DOWN@); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; padding: 4px;
                               selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT}; }}

/* checkboxes */
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {BORDER_STRONG}; border-radius: 4px;
                        background: {RAISED}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(@CHECK@); }}

/* lists and trees */
QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px;
                     alternate-background-color: {RAISED}; }}
QAbstractItemView::item {{ border-radius: 4px; }}
QAbstractItemView::item:hover {{ background: {HOVER}; }}
QAbstractItemView::item:selected {{ background: {SELECTION}; color: {TEXT}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: {SELECTION}; }}
QTreeView::branch:has-children:closed {{ image: url(@RIGHT@); }}
QTreeView::branch:has-children:open {{ image: url(@DOWN@); }}
QHeaderView {{ background: {SURFACE}; border: none; }}
QHeaderView::section {{ background: {SURFACE}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
                        padding: 5px 8px; font-weight: 600; }}
QHeaderView::section:hover {{ color: {TEXT}; }}
QToolButton#stickyHeader {{ text-align: left; padding: 2px 10px; border: none; border-radius: 0; font-weight: 600;
                            background: {RAISED}; border-bottom: 1px solid {BORDER}; }}
QToolButton#stickyHeader:hover {{ background: {HOVER}; color: {ACCENT_HOVER}; }}

/* scrollbars: thin, no arrows */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 3px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* sliders and progress */
QSlider::groove:horizontal {{ height: 4px; background: {BORDER_STRONG}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}
QProgressBar {{ background: {RAISED}; border: none; border-radius: 4px; text-align: center; color: {TEXT};
                min-height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

/* menus */
QMenuBar {{ background: {BG}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 4px; background: transparent; }}
QMenuBar::item:selected {{ background: {HOVER}; }}
QMenu {{ background: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS}px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 24px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::indicator {{ width: 12px; height: 12px; left: 6px; }}
QMenu::indicator:checked {{ image: url(@TICK@); }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* splitters, docks, status bar, tabs */
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QSplitter::handle:vertical {{ height: 5px; }}
QDockWidget::title {{ background: {BG}; color: {MUTED}; padding: 6px 10px; font-weight: 600;
                      border-top: 1px solid {BORDER}; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: {RADIUS}px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 7px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: {RADIUS}px; margin-top: 14px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}

/* named areas */
QWidget#viewerBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QWidget#sidePanel {{ background: {SURFACE}; }}
QWidget#vsep {{ background: {BORDER}; }}
QPlainTextEdit#consoleText {{ background: {BG}; border: 1px solid {BORDER}; }}
"""
    for name, path in glyphs.items():
        sheet = sheet.replace(f"@{name.upper()}@", path)
    return sheet


def role(widget, name):
    """Give a widget one of the stylesheet's variants (see the module docstring)."""
    widget.setProperty("role", name)
    if widget.style() is not None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    return widget


def palette():
    pal = QPalette()
    colors = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: SURFACE, QPalette.AlternateBase: RAISED,
        QPalette.ToolTipBase: RAISED, QPalette.ToolTipText: TEXT, QPalette.PlaceholderText: MUTED,
        QPalette.Text: TEXT, QPalette.Button: RAISED, QPalette.ButtonText: TEXT, QPalette.BrightText: "#ffffff",
        QPalette.Light: BORDER_STRONG, QPalette.Midlight: HOVER, QPalette.Mid: BORDER, QPalette.Dark: BG,
        QPalette.Shadow: "#000000", QPalette.Highlight: SELECTION, QPalette.HighlightedText: TEXT,
        QPalette.Link: ACCENT_HOVER, QPalette.LinkVisited: ACCENT,
    }
    for group_role, color in colors.items():
        pal.setColor(group_role, QColor(color))
    for group_role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, group_role, QColor(FAINT))
    return pal


# ---- line icons, drawn on a 64x64 canvas with a ~4px pen

def _pen(color, width=4.0):
    return QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


def _draw(name, p, color):
    p.setPen(_pen(color))
    p.setBrush(Qt.NoBrush)
    soft = QColor(color)
    soft.setAlpha(70)
    if name == "texture":
        p.drawRoundedRect(QRectF(8, 12, 48, 40), 6, 6)
        p.drawPolyline([QPointF(12, 46), QPointF(26, 30), QPointF(36, 40), QPointF(42, 34), QPointF(52, 44)])
        p.drawEllipse(QPointF(42, 22), 4, 4)
    elif name == "flip":
        p.drawLine(QPointF(8, 32), QPointF(56, 32))
        p.drawPolygon([QPointF(32, 8), QPointF(44, 24), QPointF(20, 24)])
        p.setBrush(QColor(color))
        p.drawPolygon([QPointF(32, 56), QPointF(44, 40), QPointF(20, 40)])
    elif name == "alpha":
        p.drawRoundedRect(QRectF(10, 10, 44, 44), 6, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for x, y in ((12, 12), (32, 32), (32, 12), (12, 32)):
            if (x + y) % 40 == 24:
                p.drawRect(QRectF(x, y, 20, 20))
    elif name == "colors":
        p.setPen(Qt.NoPen)
        for cx, cy, rgb in ((32, 22, "#ef5350"), (22, 40, "#43a047"), (42, 40, "#4c8dff")):
            c = QColor(rgb)
            c.setAlpha(190)
            p.setBrush(c)
            p.drawEllipse(QPointF(cx, cy), 14, 14)
    elif name in ("wireframe", "solid"):
        front = [QPointF(10, 22), QPointF(40, 22), QPointF(40, 52), QPointF(10, 52)]
        if name == "solid":
            p.setBrush(soft)
        p.drawPolygon(front)
        p.drawPolygon([QPointF(10, 22), QPointF(24, 10), QPointF(54, 10), QPointF(40, 22)])
        p.drawPolygon([QPointF(40, 22), QPointF(54, 10), QPointF(54, 40), QPointF(40, 52)])
        if name == "wireframe":
            p.setPen(_pen(color, 2.5))
            p.drawLine(QPointF(10, 52), QPointF(40, 22))
            p.drawLine(QPointF(40, 52), QPointF(54, 10))
    elif name == "gizmo":
        p.drawEllipse(QPointF(32, 32), 16, 16)
        for a, b in (((32, 6), (32, 18)), ((32, 46), (32, 58)), ((6, 32), (18, 32)), ((46, 32), (58, 32))):
            p.drawLine(QPointF(*a), QPointF(*b))
    elif name in ("zoom_in", "zoom_out"):
        p.drawEllipse(QPointF(27, 27), 16, 16)
        p.setPen(_pen(color, 6))
        p.drawLine(QPointF(39, 39), QPointF(54, 54))
        p.setPen(_pen(color))
        p.drawLine(QPointF(20, 27), QPointF(34, 27))
        if name == "zoom_in":
            p.drawLine(QPointF(27, 20), QPointF(27, 34))
    elif name == "reset":
        path = QPainterPath()
        path.arcMoveTo(QRectF(12, 12, 40, 40), 120)
        path.arcTo(QRectF(12, 12, 40, 40), 120, 300)
        p.drawPath(path)
        p.drawPolyline([QPointF(10, 10), QPointF(20, 18.5), QPointF(8, 24)])
    elif name == "fly":
        p.drawPolygon([QPointF(8, 30), QPointF(56, 10), QPointF(40, 54), QPointF(30, 36)])
        p.drawLine(QPointF(30, 36), QPointF(56, 10))
    elif name == "uv":
        p.drawRect(QRectF(10, 10, 44, 44))
        p.setPen(_pen(color, 2.5))
        for t in (24.7, 39.3):
            p.drawLine(QPointF(t, 10), QPointF(t, 54))
            p.drawLine(QPointF(10, t), QPointF(54, t))
        p.drawLine(QPointF(10, 54), QPointF(54, 10))
    elif name == "list":
        for y in (16, 32, 48):
            p.drawEllipse(QPointF(12, y), 2, 2)
            p.drawLine(QPointF(22, y), QPointF(54, y))
    elif name == "grid":
        for x in (10, 36):
            for y in (10, 36):
                p.drawRoundedRect(QRectF(x, y, 18, 18), 3, 3)
    elif name == "back":
        p.drawLine(QPointF(52, 32), QPointF(12, 32))
        p.drawPolyline([QPointF(28, 16), QPointF(12, 32), QPointF(28, 48)])
    elif name == "sun":
        p.drawEllipse(QPointF(32, 32), 10, 10)
        for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)):
            p.drawLine(QPointF(32 + dx * 17, 32 + dy * 17), QPointF(32 + dx * 24, 32 + dy * 24))
    else:
        raise KeyError(f"No icon called {name!r}")


def _pixmap(name, color, size=64):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64, size / 64)
    _draw(name, p, color)
    p.end()
    return pix


def icon(name):
    """A line icon in the current theme: text colour normally, accent when its button is checked,
    faint when disabled."""
    result = QIcon()
    for color, mode, state in ((TEXT, QIcon.Normal, QIcon.Off), (ACCENT_HOVER, QIcon.Normal, QIcon.On),
                               (FAINT, QIcon.Disabled, QIcon.Off), (FAINT, QIcon.Disabled, QIcon.On)):
        result.addPixmap(_pixmap(name, color), mode, state)
    return result


def on_change(callback, owner=None):
    """Call callback() after every theme switch (while `owner`, a QObject, still exists)."""
    _callbacks.append((callback, owner))


def set_icon(button, name):
    """Give a button a theme icon that follows theme switches."""
    button.setIcon(icon(name))
    on_change(lambda: button.setIcon(icon(name)), button)
    return button


# ---- stylesheet glyphs (stylesheets can only load images from files)

GLYPHS = {  # name: (token for the colour, polyline points on a 32x32 canvas)
    "check": ("#ffffff", ((7, 17), (13, 23), (25, 9))),
    "tick": ("TEXT", ((7, 17), (13, 23), (25, 9))),
    "down": ("MUTED", ((9, 12), (16, 20), (23, 12))),
    "right": ("MUTED", ((12, 9), (20, 16), (12, 23))),
}


def _glyphs():
    """Write the glyph PNGs for the current palette; returns {name: path}."""
    folder = os.path.join(tempfile.gettempdir(), "uniview_theme")
    os.makedirs(folder, exist_ok=True)
    paths = {}
    for name, (color, points) in GLYPHS.items():
        color = globals().get(color, color)
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(_pen(color, 3.5))
        painter.drawPolyline([QPointF(x, y) for x, y in points])
        painter.end()
        path = os.path.join(folder, f"{name}_{'dark' if DARK else 'light'}.png")
        pix.save(path, "PNG")
        paths[name] = path.replace("\\", "/")
    return paths


# ---- switching

def system_is_dark():
    try:
        return QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
    except AttributeError:  # Qt < 6.5
        return True


def mode():
    return _mode


def apply(app: QApplication, new_mode="dark"):
    """Fusion style + the palette + the stylesheet for the whole app. new_mode: dark, light or system
    (follows Windows' app mode, also when it changes later)."""
    global _mode, DARK
    first = _mode is None or not app.property("uniview_themed")
    _mode = new_mode if new_mode in MODES else "dark"
    DARK = system_is_dark() if _mode == "system" else _mode == "dark"
    globals().update(PALETTES["dark" if DARK else "light"])
    if first:
        app.setProperty("uniview_themed", True)
        app.setStyle(QStyleFactory.create("Fusion"))
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(
                lambda _scheme: _mode == "system" and apply(app, "system"))
        except AttributeError:
            pass
    app.setPalette(palette())
    try:
        glyphs = _glyphs()
    except OSError:
        glyphs = {}
    app.setStyleSheet(_stylesheet(glyphs))
    alive = []
    for callback, owner in _callbacks:
        if owner is not None and not shiboken6.isValid(owner):
            continue
        alive.append((callback, owner))
        if not first:
            callback()
    _callbacks[:] = alive
