"""The asset list: names on up to two lines, a group header that sticks to the top while you scroll
through a group (click it to close the group), one-click opening/closing of groups, and a compact
layout for a narrow panel (Info and Size shown under the name instead of in their own columns)."""

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QAbstractItemView, QMenu, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QToolButton, QToolTip, QTreeWidget)

BREAK_AFTER = "_/\\.- "  # preferred places to wrap a name


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


class NameDelegate(QStyledItemDelegate):
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
        self.setItemDelegateForColumn(0, NameDelegate(self))
        self.setIndentation(12)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.sticky = QToolButton(self.viewport())
        self.sticky.setCursor(Qt.PointingHandCursor)
        self.sticky.setToolTip("Close this group and go back to its top")
        self.sticky.setStyleSheet(
            "QToolButton { text-align: left; padding: 2px 8px; border: none; font-weight: bold;"
            " background: palette(button); border-bottom: 1px solid palette(mid); }"
            "QToolButton:hover { background: palette(midlight); }")
        self.sticky.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.sticky.hide()
        self.sticky.clicked.connect(self.close_sticky_group)
        self._sticky_group = None
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
        self.sticky.setText(f"▾  {group.text(0)}     (click to close)")
        self.sticky.setGeometry(0, 0, self.viewport().width(), self.STICKY_HEIGHT)
        self.sticky.show()
        self.sticky.raise_()

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
