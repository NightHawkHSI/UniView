"""The log console dock: the live log, drawn with colour (dim time, level badge, highlighted values,
tinted error rows), coloured level chips with counts, a text filter, copy / save and a wrap toggle.

Everything the handler sends is kept (up to MAX_LINES) as plain text, and lines are drawn from that, so the
filters can change after the fact without losing anything and a theme switch just redraws in the new colours.
"""

import html as _html
import logging
import logging.handlers  # noqa: F401  (kept from the original imports)
import re
from collections import deque

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QBrush, QColor, QFontDatabase, QGuiApplication, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from uniview.constants import CRASH_FILE, LOG_FILE
from uniview.ui import theme
from uniview.util import open_path

MAX_LINES = 5000
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.I)
# "23:59:09  WARNING Skipped ..." -> time, level word, message
MSG_RE = re.compile(r"^\s*(\d{1,2}:\d\d:\d\d(?:[.,]\d+)?)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)$", re.S)
# values worth picking out of a message: 'quoted' / "quoted" text, and numbers with an optional unit
VALUE_RE = re.compile(r"""('[^'\n]{1,80}'|"[^"\n]{1,80}"|\b\d[\d,.]*(?:\s?(?:ms|s|KB|MB|GB|%))?\b)""")
SPACES_RE = re.compile(r"^ | {2,}")
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
          "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}
LABELS = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN",
          logging.ERROR: "ERROR", logging.CRITICAL: "CRIT"}


def _tok(name, default):
    return getattr(theme, name, default)


def _mix(a, b, t):
    """Colour a blended toward colour b by t (0..1)."""
    a, b = QColor(a), QColor(b)
    return QColor(*(round(x + (y - x) * t) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


def _nbsp(text):
    return SPACES_RE.sub(lambda m: "&nbsp;" * len(m.group()), text)


class ConsoleDock(QDockWidget):
    """Bottom panel showing the log as it happens."""

    CHIPS = (("All", 0, "chipAll"), ("Info", logging.INFO, "chipInfo"),
             ("Warnings", logging.WARNING, "chipWarn"), ("Errors", logging.ERROR, "chipErr"))

    def __init__(self, handler, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("console")
        self.handler = handler
        self._entries = deque(maxlen=MAX_LINES)  # (plain, plain_lowercase, level)
        self._last_level = logging.INFO          # continuation lines (tracebacks) inherit the previous level
        self._min_level = 0
        self._needle = ""
        self._counts = {logging.WARNING: 0, logging.ERROR: 0}
        self._restyling = False
        self._colors = {}
        self._text_colors = {}

        self.text = QPlainTextEdit(readOnly=True, objectName="consoleText")
        self.text.setMaximumBlockCount(MAX_LINES)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)

        debug = QCheckBox("Show debug")
        debug.setToolTip("Also show low-level messages (e.g. assets that have no thumbnail).")
        debug.toggled.connect(lambda on: handler.setLevel(logging.DEBUG if on else logging.INFO))

        # level chips: show this level and everything above it
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.chips = {}
        chip_tips = {0: "Show everything.", logging.INFO: "Info, warnings and errors.",
                     logging.WARNING: "Warnings and errors.", logging.ERROR: "Errors only."}
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.addWidget(debug)
        for label, level, name in self.CHIPS:
            chip = QPushButton(label, objectName=name)
            chip.setCheckable(True)
            chip.setProperty("chip", True)
            chip.setToolTip(chip_tips[level])
            self.chip_group.addButton(chip, level)
            self.chips[level] = chip
            top.addWidget(chip)
        self.chips[0].setChecked(True)
        self.chip_group.idClicked.connect(self._level_changed)

        self.search = QLineEdit(objectName="consoleFilter")
        self.search.setPlaceholderText("Filter text...")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Only show lines containing this text (not case sensitive).")
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        top.addWidget(self.search, 1)

        wrap = QCheckBox("Wrap")
        wrap.setToolTip("Wrap long lines instead of scrolling sideways.")
        wrap.toggled.connect(lambda on: self.text.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap))
        top.addWidget(wrap)

        copy = QPushButton("Copy")
        copy.setToolTip("Copy the lines currently shown (or just the selection).")
        copy.clicked.connect(self.copy_visible)
        save = QPushButton("Save...")
        save.setToolTip("Save the lines currently shown to a text file.")
        save.clicked.connect(self.save_visible)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        open_log = QPushButton("Open log file")
        open_log.clicked.connect(lambda: open_path(LOG_FILE))
        open_crash = QPushButton("Open crash log")
        open_crash.clicked.connect(lambda: open_path(CRASH_FILE))
        for btn in (copy, save, clear, open_log, open_crash):
            btn.setFlat(True)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        for w in (copy, save, clear):
            bottom.addWidget(w)
        bottom.addStretch()
        bottom.addWidget(open_log)
        bottom.addWidget(open_crash)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(6)
        lay.addLayout(top)
        lay.addWidget(self.text)
        lay.addLayout(bottom)
        self.setWidget(body)
        self._restyle()
        self._update_counts()
        handler.bridge.html.connect(self._on_html)

    # ---- colours
    def _restyle(self):
        """(Re)build the colours and the stylesheet from the current theme / palette."""
        self._restyling = True
        try:
            pal = self.palette()
            base, fg = pal.color(QPalette.Base), pal.color(QPalette.Text)
            self._colors = {
                logging.DEBUG: _tok("FAINT", "#7a828e"), logging.INFO: _tok("ACCENT", "#6ea8fe"),
                logging.WARNING: _tok("WARNING", "#ffb454"), logging.ERROR: _tok("DANGER", "#ff6b6b"),
                logging.CRITICAL: "#ff4081",
            }
            self._base, self._fg = base, fg
            dark = base.lightness() < 128
            # text in level colours needs to be darker on a light background to stay readable (badges stay solid)
            self._text_colors = {lv: (col if dark else QColor(col).darker(165).name()) for lv, col in self._colors.items()}
            border = _mix(base, fg, 0.22).name()
            css = [
                f'QPushButton[chip="true"] {{ border: 1px solid {border}; border-radius: 11px; padding: 2px 12px;'
                f' background: transparent; }}',
                f'QPushButton[chip="true"]:hover {{ border-color: {_mix(base, fg, 0.5).name()}; }}',
                f'QPushButton#chipAll:checked {{ background: {_mix(base, fg, 0.18).name()};'
                f' border-color: {_mix(base, fg, 0.4).name()}; }}',
            ]
            for name, level in (("chipInfo", logging.INFO), ("chipWarn", logging.WARNING), ("chipErr", logging.ERROR)):
                c = QColor(self._colors[level])
                text_col = c.lighter(125) if dark else c.darker(140)
                css.append(f'QPushButton#{name}:checked {{ background: {_mix(base, c, 0.30).name()};'
                           f' border-color: {c.name()}; color: {text_col.name()}; }}')
                css.append(f'QPushButton#{name} {{ color: {text_col.name()}; }}')
            css.append(f'QLineEdit#consoleFilter {{ border: 1px solid {border}; border-radius: 11px;'
                       f' padding: 2px 10px; background: {_mix(base, fg, 0.05).name()}; }}')
            css.append(f'QLineEdit#consoleFilter:focus {{ border-color: {_mix(base, QColor(self._colors[logging.INFO]), 0.7).name()}; }}')
            css.append(f'QPlainTextEdit#consoleText {{ border: 1px solid {border}; border-radius: 6px;'
                       f' background: {_mix(base, QColor("black") if dark else QColor("white"), 0.25).name()}; padding: 2px; }}')
            self.widget().setStyleSheet("\n".join(css))
        finally:
            self._restyling = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange and not self._restyling:
            QTimer.singleShot(0, self._theme_changed)

    def _theme_changed(self):
        self._restyle()
        self._refilter()

    # ---- drawing one line
    def _line_html(self, plain, level):
        c, tc = self._colors, self._text_colors
        dim, lc = c[logging.DEBUG], c[level]
        m = MSG_RE.match(plain)
        if m:
            clock, word, msg = m.groups()
            level = LEVELS[word]
            lc = c[level]
            label = LABELS[level].ljust(5).replace(" ", "&nbsp;")
            if level >= logging.WARNING:  # solid badge
                badge_fg = "#1b1d21" if QColor(lc).lightness() > 110 else "#ffffff"
                badge = f'<span style="background-color:{lc};color:{badge_fg};font-weight:600">&nbsp;{label}&nbsp;</span>'
            else:
                badge = f'<span style="color:{lc};font-weight:600">&nbsp;{label}&nbsp;</span>'
            body = _html.escape(msg, quote=False)
            body = VALUE_RE.sub(lambda v: f'<span style="color:{tc[level]};'
                                          f'font-weight:600">{v.group(1)}</span>', _nbsp(body))
            if level == logging.DEBUG:
                body = f'<span style="color:{dim}">{body}</span>'
            elif level >= logging.WARNING:
                body = f'<span style="color:{tc[level]}">{body}</span>'
            inner = f'<span style="color:{dim}">{_html.escape(clock)}</span>&nbsp;{badge}&nbsp;{body}'
        else:  # continuation line (traceback ...): same colour family as its parent, a little quieter
            col = tc[level] if level >= logging.WARNING else dim
            inner = f'<span style="color:{col}">{_nbsp(_html.escape(plain, quote=False))}</span>'
        return f'<p style="margin:0">{inner}</p>'

    def _tint(self, plain, level):
        """Background for the block just added: errors and their traceback lines get a soft red wash."""
        strength = {logging.ERROR: 0.16, logging.CRITICAL: 0.22}.get(level)
        if not strength:
            return
        if not MSG_RE.match(plain):
            strength *= 0.55  # continuation lines a little lighter
        fmt = QTextCursor(self.text.document().lastBlock())
        block_fmt = fmt.blockFormat()
        block_fmt.setBackground(QBrush(_mix(self._base, QColor(self._colors[level]), strength)))
        fmt.setBlockFormat(block_fmt)

    def _append_line(self, plain, level):
        self.text.appendHtml(self._line_html(plain, level))
        self._tint(plain, level)

    def _show(self, plain, level):
        bar = self.text.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self._append_line(plain, level)
        if at_bottom:
            bar.setValue(bar.maximum())

    # ---- incoming lines
    def _on_html(self, html_line):
        """A line (or several) from the log handler."""
        text = _html.unescape(TAG_RE.sub("", BR_RE.sub("\n", html_line)))
        for plain in text.split("\n"):
            m = MSG_RE.match(plain)
            if m:
                self._last_level = LEVELS[m.group(2)]
            self._add(plain, self._last_level, counted=bool(m))

    def append(self, text, level):
        """Add one plain line at the given level (kept for callers that don't go through the handler)."""
        self._last_level = level
        self._add(text, level, counted=True)

    def _add(self, plain, level, counted):
        self._entries.append((plain, plain.lower(), level))
        key = logging.ERROR if level >= logging.ERROR else level
        if counted and key in self._counts:
            self._counts[key] += 1
            self._update_counts()
        if self._passes(level, plain.lower()):
            self._show(plain, level)

    # ---- filters
    def _passes(self, level, plain_lower):
        return level >= self._min_level and (not self._needle or self._needle in plain_lower)

    def _level_changed(self, level):
        self._min_level = level
        self._refilter()

    def _apply_search(self):
        self._needle = self.search.text().strip().lower()
        self._refilter()

    def _refilter(self):
        """Redraw the view from the stored lines using the current filters."""
        self.text.setUpdatesEnabled(False)
        try:
            self.text.clear()
            for plain, lower, level in self._entries:
                if self._passes(level, lower):
                    self._append_line(plain, level)
        finally:
            self.text.setUpdatesEnabled(True)
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _update_counts(self):
        warnings, errors = self._counts[logging.WARNING], self._counts[logging.ERROR]
        self.chips[logging.WARNING].setText(f"Warnings  {warnings:,}" if warnings else "Warnings")
        self.chips[logging.ERROR].setText(f"Errors  {errors:,}" if errors else "Errors")
        self.setWindowTitle("Console" + (f"  ({errors:,} error{'s' if errors != 1 else ''})" if errors else ""))

    # ---- actions
    def visible_text(self):
        return "\n".join(plain for plain, lower, level in self._entries if self._passes(level, lower))

    def copy_visible(self):
        selected = self.text.textCursor().selectedText().replace("\u2029", "\n")
        QGuiApplication.clipboard().setText(selected or self.visible_text())

    def save_visible(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save console", "uniview-console.txt",
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.visible_text() + "\n")
        except OSError as e:
            self.append(f"Could not save the console to {path}: {e}", logging.ERROR)

    def clear(self):
        self._entries.clear()
        self._counts = {logging.WARNING: 0, logging.ERROR: 0}
        self.text.clear()
        self._update_counts()