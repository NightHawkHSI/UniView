"""The asset list: names on up to two lines, a group header that sticks to the top while you scroll
through a group (click it to close the group), one-click opening/closing of groups, and a compact
layout for a narrow panel (Info and Size shown under the name instead of in their own columns)."""

import logging
import os
import re

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QTreeWidget,
)

# Set UNIVIEW_PLAIN_TREE=1 to switch every colourful bit off and get the original look back.
COLORFUL = os.environ.get("UNIVIEW_PLAIN_TREE", "").strip().lower() not in ("1", "true", "yes")

log = logging.getLogger("viewer.asset_tree")
_warned = set()


def _warn_once(where, exc):
    """Styling must never take the app down: log the first failure per place and fall back to plain painting."""
    if where not in _warned:
        _warned.add(where)
        log.warning("Colourful painting failed in %s (%s: %s); using the plain look", where, type(exc).__name__, exc,
                    exc_info=True)


BREAK_AFTER = "_/\\.- "  # preferred places to wrap a name


# ---- colors: every group (Models, Textures ...) gets its own accent, shared by its rows and sticky header
ACCENTS = (  # (keywords in the group name, accent colour)
    (("model", "mesh", "prefab", "object"), "#4cc9f0"),
    (("texture", "page", "background", "image", "material", "shader"), "#b388ff"),
    (("sprite", "anim", "clip", "frame"), "#5be37d"),
    (("sound", "audio", "music", "voice"), "#ffb454"),
    (("script", "code", "text", "data", "file", "font"), "#8ab4ff"),
)
DEFAULT_ACCENT = "#6ea8fe"


def accent_for(group_text):
    name = (group_text or "").lower()
    for words, color in ACCENTS:
        if any(w in name for w in words):
            return QColor(color)
    return QColor(DEFAULT_ACCENT)


def split_count(text):
    """'Sprites  (18)' -> ('Sprites', '18'); text without a trailing (n) -> (text, '')."""
    m = re.match(r"^(.*?)\s*\(([\d,.]+)\)\s*$", text or "")
    return (m.group(1), m.group(2)) if m else (text or "", "")


def is_dark(palette):
    return palette.color(QPalette.Base).lightness() < 128


def with_alpha(color, alpha):
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def text_accent(color, dark):
    return color.lighter(135) if dark else color.darker(150)


class RowDelegate(QStyledItemDelegate):
    """Paints the row background for every column: a fading accent wash on group rows, a soft tint on hover,
    an accent tint plus a bar on the left edge when selected, and a thin accent stripe on every asset row."""

    def paint_row(self, painter, opt, index):
        tree = self.parent()
        is_group = not index.parent().isValid()
        accent = accent_for((index.siblingAtColumn(0) if is_group else index.parent()).data())
        dark = is_dark(opt.palette)
        r = opt.rect
        selected = bool(opt.state & QStyle.State_Selected)
        hovered = bool(opt.state & QStyle.State_MouseOver)
        painter.save()
        try:
            if is_group:
                grad = QLinearGradient(0, 0, max(1, tree.viewport().width()), 0)
                grad.setColorAt(0.0, with_alpha(accent, 70 if dark else 60))
                grad.setColorAt(1.0, with_alpha(accent, 8))
                painter.fillRect(r, QBrush(grad))
                painter.fillRect(QRect(r.left(), r.bottom(), r.width(), 1), with_alpha(accent, 90))
            elif selected:
                painter.fillRect(r, with_alpha(accent, 80 if dark else 60))
            elif hovered:
                painter.fillRect(r, QColor(255, 255, 255, 16) if dark else QColor(0, 0, 0, 14))
            if index.column() == 0:  # bar flush with the left edge of the list
                wide_bar = is_group or selected
                painter.fillRect(QRect(0, r.top(), 4 if wide_bar else 2, r.height()),
                                 accent if wide_bar else with_alpha(accent, 110))
        finally:
            painter.restore()
        return accent, is_group, dark

    def paint(self, painter, option, index):
        if not COLORFUL:
            return QStyledItemDelegate.paint(self, painter, option, index)
        try:
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            self.paint_row(painter, opt, index)
            opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
            (opt.widget.style() if opt.widget else QApplication.style()).drawControl(
                QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        except Exception as e:  # never let styling break the list
            _warn_once("RowDelegate.paint", e)
            QStyledItemDelegate.paint(self, painter, option, index)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if COLORFUL and not index.parent().isValid():
            size.setHeight(max(size.height(), 28))  # group rows a little taller
        return size


def wrap_name(text, fits, max_lines=2):
    """Split `text` into at most `max_lines` lines that each satisfy fits(line), breaking after
    _ / . - or a space when that keeps a line reasonably full, else anywhere. The last line is
    returned as-is (the caller elides it). Returns (lines, complete) - complete is False when the
    text didn't fit without eliding."""
    lines, rest = [], text
    while rest and len(lines) < max_lines - 1:
        if fits(rest):
            break
        lo, hi = 1, len(rest)  # longest prefix that fits
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(rest[:mid]):
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        nice = max((rest.rfind(c, 0, cut) for c in BREAK_AFTER), default=-1)
        if nice >= cut * 0.5:
            cut = nice + 1
        lines.append(rest[:cut])
        rest = rest[cut:]
    lines.append(rest)
    return lines, fits(lines[-1])


class NameDelegate(RowDelegate):
    """Name column: icon, then the name on up to two lines, shortened in the middle if still too long.
    In compact mode, asset rows show their Info and Size in grey under the name."""

    def _details(self, index):
        tree = self.parent()
        if not getattr(tree, "compact", False) or not index.parent().isValid():
            return ""
        info, size = (index.siblingAtColumn(c).data() or "" for c in (1, 2))
        return "  ·  ".join(t for t in (info, size) if t)

    def _text_rect(self, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else None
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget) if style else opt.rect
        return opt, rect.adjusted(2, 0, -2, 0)

    def _layout(self, opt, rect, reserve=0):
        fm = opt.fontMetrics
        max_lines = max(1, min(2, rect.height() // max(1, fm.lineSpacing()) - reserve))
        lines, complete = wrap_name(opt.text, lambda s: fm.horizontalAdvance(s) <= rect.width(), max_lines)
        if not complete:
            lines[-1] = fm.elidedText(lines[-1], Qt.ElideMiddle, rect.width())
        return lines

    def paint(self, painter, option, index):
        if not COLORFUL:
            return self._paint_plain(painter, option, index)
        painter.save()
        try:
            self._paint_fancy(painter, option, index)
        except Exception as e:  # never let styling break the list
            _warn_once("NameDelegate.paint", e)
            painter.restore()
            painter.save()
            QStyledItemDelegate.paint(self, painter, option, index)
        finally:
            painter.restore()

    def _paint_plain(self, painter, option, index):
        """The original look (UNIVIEW_PLAIN_TREE=1)."""
        opt, rect = self._text_rect(option, index)
        text = opt.text
        details = self._details(index)
        lines = self._layout(opt, rect, reserve=1 if details else 0)
        opt.text = ""
        style = opt.widget.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        selected = opt.state & QStyle.State_Selected
        painter.setPen(opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text))
        painter.setFont(opt.font)
        fm = opt.fontMetrics
        height = fm.lineSpacing() * (len(lines) + (1 if details else 0))
        y = rect.top() + (rect.height() - height) // 2
        for line in lines:
            painter.drawText(QRect(rect.left(), y, rect.width(), fm.lineSpacing()),
                             Qt.AlignLeft | Qt.AlignVCenter, line)
            y += fm.lineSpacing()
        if details:
            painter.setPen(opt.palette.color(QPalette.HighlightedText if selected else QPalette.PlaceholderText))
            painter.drawText(QRect(rect.left(), y, rect.width(), fm.lineSpacing()), Qt.AlignLeft | Qt.AlignVCenter,
                             fm.elidedText(details, Qt.ElideRight, rect.width()))
        painter.restore()
        opt.text = text

    def _paint_fancy(self, painter, option, index):
        opt, rect = self._text_rect(option, index)
        text = opt.text
        accent, is_group, dark = self.paint_row(painter, opt, index)
        selected = bool(opt.state & QStyle.State_Selected)
        details = self._details(index)
        lines = self._layout(opt, rect, reserve=1 if details else 0)
        opt.text = ""
        opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
        (opt.widget.style() if opt.widget else QApplication.style()).drawControl(
            QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.setRenderHint(QPainter.Antialiasing)
        if is_group:
            self._paint_group(painter, opt, rect, text, accent, dark)
            opt.text = text
            return
        painter.setPen(opt.palette.color(QPalette.Text))
        painter.setFont(opt.font)
        fm = opt.fontMetrics
        height = fm.lineSpacing() * (len(lines) + (1 if details else 0))
        y = rect.top() + (rect.height() - height) // 2
        for line in lines:
            painter.drawText(QRect(rect.left(), y, rect.width(), fm.lineSpacing()),
                             Qt.AlignLeft | Qt.AlignVCenter, line)
            y += fm.lineSpacing()
        if details:
            painter.setPen(text_accent(accent, dark) if selected else opt.palette.color(QPalette.PlaceholderText))
            painter.drawText(QRect(rect.left(), y, rect.width(), fm.lineSpacing()), Qt.AlignLeft | Qt.AlignVCenter,
                             fm.elidedText(details, Qt.ElideRight, rect.width()))
        opt.text = text

    def _paint_group(self, painter, opt, rect, text, accent, dark):
        """Group row: bold accent-coloured name followed by a rounded count pill."""
        name, count = split_count(text)
        font = QFont(opt.font)
        font.setBold(True)
        painter.setFont(font)
        fm = QFontMetrics(font)
        pill_w = fm.horizontalAdvance(count) + 14 if count else 0
        avail = max(10, rect.width() - pill_w - 8)
        shown = name if fm.horizontalAdvance(name) <= avail else fm.elidedText(name, Qt.ElideRight, avail)
        name_w = fm.horizontalAdvance(shown)
        painter.setPen(text_accent(accent, dark))
        painter.drawText(QRect(rect.left(), rect.top(), name_w + 6, rect.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         shown)
        if count and rect.width() > name_w + pill_w + 8:
            pill = QRect(rect.left() + name_w + 8, rect.center().y() - 9, pill_w, 18)
            painter.setPen(Qt.NoPen)
            painter.setBrush(with_alpha(accent, 90))
            painter.drawRoundedRect(pill, 9, 9)
            painter.setPen(QColor("#ffffff") if dark else text_accent(accent, False))
            painter.drawText(pill, Qt.AlignCenter, count)

    def helpEvent(self, event, view, option, index):
        # Full name as a tooltip when it doesn't fit on the row.
        opt, rect = self._text_rect(option, index)
        details = self._details(index)
        if opt.text and ("".join(self._layout(opt, rect, reserve=1 if details else 0)) != opt.text):
            QToolTip.showText(event.globalPos(), opt.text + ("\n" + details if details else ""), view)
            return True
        return super().helpEvent(event, view, option, index)


class AssetTree(QTreeWidget):
    """QTreeWidget with top-level group rows (Models, Textures ...) and asset rows under them."""

    STICKY_HEIGHT = 24
    COMPACT_BELOW = 480  # px: narrower than this, Info/Size go under the name
    SORT_NAMES = ("Name", "Info (triangles, pixels...)", "Size")

    def __init__(self, parent=None):
        super().__init__(parent)
        if COLORFUL:
            self.setItemDelegate(RowDelegate(self))  # background for the Info / Size columns
        self.setItemDelegateForColumn(0, NameDelegate(self))
        if COLORFUL:
            self.setMouseTracking(True)  # hover tint
            self.setAlternatingRowColors(False)
            pal = self.palette()  # selection is drawn by the delegates (accent tint); keep the view's own fill invisible
            pal.setColor(QPalette.Highlight, QColor(0, 0, 0, 0))
            self.setPalette(pal)
        self.setIndentation(12)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.sticky = QToolButton(self.viewport())
        self.sticky.setCursor(Qt.PointingHandCursor)
        self.sticky.setToolTip("Close this group and go back to its top")
        self.sticky.setObjectName("stickyHeader")
        self.sticky.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.sticky.hide()
        self.sticky.clicked.connect(self.close_sticky_group)
        self._sticky_group = None
        self._sticky_style = None
        self._press_pos = None
        self.compact = False
        self.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.header().customContextMenuRequested.connect(self._header_menu)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.update_sticky())
        self.itemExpanded.connect(lambda _: self.update_sticky())
        self.itemCollapsed.connect(lambda _: self.update_sticky())

    # ---- sticky group header
    def group_at_top(self):
        """The group whose rows fill the top of the list while its own header row is scrolled away."""
        item = self.itemAt(QPoint(4, 2))
        if item is None or item.parent() is None:
            return None
        group = item.parent()
        return group if self.visualItemRect(group).bottom() < 0 else None

    def update_sticky(self):
        group = self.group_at_top()
        self._sticky_group = group
        if group is None:
            self.sticky.hide()
            return
        try:
            if COLORFUL:
                self._style_sticky(group)
        except Exception as e:  # cosmetic only
            _warn_once("sticky header colours", e)
        self.sticky.setText(f"▾  {group.text(0).replace('&', '&&')}     (click to close)")
        self.sticky.setGeometry(0, 0, self.viewport().width(), self.STICKY_HEIGHT)
        self.sticky.show()
        self.sticky.raise_()

    def _style_sticky(self, group):
        """Colour the pinned header like its group row: a tint of the group's accent over the list background."""
        accent = accent_for(group.text(0))
        pal = self.palette()
        dark = is_dark(pal)
        base = pal.color(QPalette.Base)
        mix = lambda t: QColor(*(round(b + (a - b) * t) for a, b in zip(accent.getRgb()[:3], base.getRgb()[:3])))
        key = (accent.name(), dark, base.name())
        if key == self._sticky_style:
            return
        self._sticky_style = key
        self.sticky.setStyleSheet(
            f"QToolButton#stickyHeader {{ background: {mix(0.28).name()}; color: {text_accent(accent, dark).name()};"
            f" border: 0; border-bottom: 2px solid {accent.name()}; border-left: 4px solid {accent.name()};"
            f" text-align: left; padding-left: 8px; font-weight: 600; }}"
            f"QToolButton#stickyHeader:hover {{ background: {mix(0.40).name()}; }}")

    def close_sticky_group(self):
        group = self._sticky_group
        if group is None:
            return
        self.collapseItem(group)
        self.scrollToItem(group, QAbstractItemView.PositionAtTop)
        self.update_sticky()

    def collapse_all_groups(self):
        for i in range(self.topLevelItemCount()):
            self.collapseItem(self.topLevelItem(i))
        self.scrollToTop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_compact(self.width() < self.COMPACT_BELOW)
        self.update_sticky()

    # ---- compact layout for a narrow panel
    def set_compact(self, on):
        if on == self.compact:
            return
        self.compact = on
        for col in (1, 2):
            self.setColumnHidden(col, on)
        self.viewport().update()

    def _header_menu(self, pos):
        header = self.header()
        menu = QMenu(self)
        for col, name in enumerate(self.SORT_NAMES):
            for order, arrow in ((Qt.AscendingOrder, "↑"), (Qt.DescendingOrder, "↓")):
                act = menu.addAction(f"Sort by {name} {arrow}",
                                     lambda c=col, o=order: header.setSortIndicator(c, o))
                act.setCheckable(True)
                act.setChecked(header.sortIndicatorSection() == col and header.sortIndicatorOrder() == order)
        menu.addSeparator()
        menu.addAction("Collapse all groups", self.collapse_all_groups)
        menu.exec(header.mapToGlobal(pos))

    # ---- one click opens/closes a group
    def mousePressEvent(self, event):
        self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        on_row = (item is not None and item.childCount() and event.button() == Qt.LeftButton
                  and self._press_pos is not None and (pos - self._press_pos).manhattanLength() < 6
                  and pos.x() >= self.visualItemRect(item).left())  # not the arrow: it toggles by itself
        super().mouseReleaseEvent(event)
        if on_row:
            item.setExpanded(not item.isExpanded())