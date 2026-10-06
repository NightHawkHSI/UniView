"""Animation state machine view: the layer's states as a graph (like Unity's Animator window) next to the
details (parameters, each state's clips and transitions with their conditions)."""

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uniview.controller_graph import edges, layout, motion_clips, transition_text
from uniview.ui import theme
from uniview.ui.theme import role
from uniview.util import wheel_steps

NODE_W, NODE_H, GAP_X, GAP_Y = 170, 38, 90, 26
DEFAULT_COLOR = "#e8833a"   # Unity marks the default state orange
ANY_COLOR = "#2fa3a3"       # and Any State teal
ENTRY_COLOR = "#3a9b4a"


class _GraphView(QGraphicsView):
    node_double_clicked = Signal(int)  # state index

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, event):
        factor = 1.2 ** wheel_steps(event)
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        while item is not None and not isinstance(item.data(0), int):
            item = item.parentItem()
        if item is not None:
            self.node_double_clicked.emit(item.data(0))
        super().mouseDoubleClickEvent(event)


class ControllerView(QWidget):
    clip_requested = Signal(object)  # an animation clip Asset (double-click a state)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.controller = None
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.layer_combo = QComboBox()
        self.layer_combo.currentIndexChanged.connect(self._show_layer)
        hint = role(QLabel("Double-click a state to open its clip · wheel zooms · drag pans"), "muted")
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        bar.addWidget(self.title)
        bar.addSpacing(12)
        bar.addWidget(role(QLabel("Layer"), "muted"))
        bar.addWidget(self.layer_combo)
        bar.addStretch()
        bar.addWidget(hint)

        self.scene = QGraphicsScene(self)
        self.view = _GraphView(self.scene)
        self.view.node_double_clicked.connect(self._node_double_clicked)
        self.scene.selectionChanged.connect(self._scene_selected)
        self.details = QTreeWidget()
        self.details.setHeaderLabels(["Name", "Value"])
        self.details.setColumnWidth(0, 210)
        self.details.itemDoubleClicked.connect(self._detail_double_clicked)
        self.details.currentItemChanged.connect(self._detail_selected)
        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(self.details)
        split.setStretchFactor(0, 1)
        split.setSizes([760, 380])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self._nodes = {}       # state index -> rect item
        self._state_items = {}  # state index -> details tree item

    # ---- data
    def show_controller(self, name, controller):
        self.controller = controller
        self.title.setText(f"{name}   ·   {len(controller['parameters'])} parameters")
        layers = controller.get("layers") or []
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        self.layer_combo.addItems([f"{layer['name']}  ({len(layer['states'])} states)" for layer in layers]
                                  or ["(no layers)"])
        self.layer_combo.blockSignals(False)
        self._show_layer(0)

    def _show_layer(self, index):
        self.scene.clear()
        self.details.clear()
        self._nodes, self._state_items = {}, {}
        layers = (self.controller or {}).get("layers") or []
        layer = layers[index] if 0 <= index < len(layers) else None
        self._fill_details(layer)
        if layer is None or not layer["states"]:
            self.scene.addText("This layer has no states.").setDefaultTextColor(QColor(theme.MUTED))
            return
        self._draw(layer)
        self.view.resetTransform()
        self.view.fitInView(self.scene.itemsBoundingRect().adjusted(-30, -30, 30, 30), Qt.KeepAspectRatio)
        scale = self.view.transform().m11()
        if scale > 1.0 or scale < 0.6:  # big machines: stay readable, start at the default state
            self.view.resetTransform()
            if scale < 0.6:
                self.view.scale(0.75, 0.75)
                start = self._nodes.get(layer.get("default_state", 0))
                if start is not None:
                    self.view.centerOn(start)

    # ---- graph
    def _node(self, rect, text, fill, data=None, bold=False):
        item = QGraphicsRectItem(rect)
        item.setBrush(QBrush(QColor(fill)))
        item.setPen(QPen(QColor(theme.BORDER_STRONG), 1.2))
        item.setFlag(QGraphicsItem.ItemIsSelectable, True)
        item.setData(0, data)
        label = QGraphicsSimpleTextItem(text, item)
        font = QFont()
        font.setBold(bold)
        label.setFont(font)
        label.setBrush(QBrush(QColor("#ffffff" if fill in (DEFAULT_COLOR, ANY_COLOR, ENTRY_COLOR) else theme.TEXT)))
        metrics = label.boundingRect()
        if metrics.width() > rect.width() - 12:  # long names: shorten with an ellipsis
            while text and label.boundingRect().width() > rect.width() - 16:
                text = text[:-1]
                label.setText(text + "…")
            metrics = label.boundingRect()
        label.setPos(rect.center().x() - metrics.width() / 2, rect.center().y() - metrics.height() / 2)
        self.scene.addItem(item)
        return item

    def _arrow(self, a, b, color, offset=0.0, label=""):
        """An arrow between two rects' borders (offset sideways so A->B and B->A don't overlap)."""
        p, q = a.center(), b.center()
        dx, dy = q.x() - p.x(), q.y() - p.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length * offset, dx / length * offset
        p, q = QPointF(p.x() + nx, p.y() + ny), QPointF(q.x() + nx, q.y() + ny)
        p, q = _border(a, p, q), _border(b, q, p)
        path = QPainterPath(p)
        path.lineTo(q)
        angle = math.atan2(q.y() - p.y(), q.x() - p.x())
        head = QPolygonF([q, QPointF(q.x() - 10 * math.cos(angle - 0.4), q.y() - 10 * math.sin(angle - 0.4)),
                          QPointF(q.x() - 10 * math.cos(angle + 0.4), q.y() - 10 * math.sin(angle + 0.4)), q])
        path.addPolygon(head)
        item = QGraphicsPathItem(path)
        item.setPen(QPen(QColor(color), 1.4))
        item.setBrush(QBrush(QColor(color)))
        item.setZValue(-1)
        if label:
            item.setToolTip(label)
        self.scene.addItem(item)

    def _draw(self, layer):
        states = layer["states"]
        places = layout(layer)
        default = layer.get("default_state", 0)

        def rect_at(col, row):
            return QRectF((col + 1) * (NODE_W + GAP_X), row * (NODE_H + GAP_Y), NODE_W, NODE_H)

        for i, s in enumerate(states):
            col, row = places[i]
            clips = motion_clips(s.get("motion"))
            fill = DEFAULT_COLOR if i == default else theme.RAISED
            node = self._node(rect_at(col, row), s["name"], fill, data=i, bold=i == default)
            tip = [s["name"]] + ([f"clip: {clips[0].name}"] if len(clips) == 1 else
                                 [f"blend tree: {len(clips)} clips"] if clips else ["no motion"])
            node.setToolTip("\n".join(tip))
            self._nodes[i] = node
        entry = self._node(QRectF(0, -(NODE_H + GAP_Y), NODE_W * 0.7, NODE_H), "Entry", ENTRY_COLOR)
        self._arrow(entry.rect(), self._nodes[default].rect(), ENTRY_COLOR)
        any_rect = None
        if layer.get("any_transitions"):
            any_rect = self._node(QRectF(0, NODE_H + GAP_Y, NODE_W * 0.7, NODE_H), "Any State", ANY_COLOR).rect()
        pairs = {(a, b) for a, b, _t in edges(layer)}
        for a, b, t in edges(layer):
            src = any_rect if a == -1 else self._nodes[a].rect()
            if src is None:
                continue
            offset = 5.0 if (b, a) in pairs else 0.0
            color = ANY_COLOR if a == -1 else theme.MUTED
            self._arrow(src, self._nodes[b].rect(), color, offset, transition_text(t, states))

    # ---- details
    def _fill_details(self, layer):
        bold = QFont()
        bold.setBold(True)
        params = QTreeWidgetItem(self.details, [f"Parameters ({len(self.controller['parameters'])})", ""])
        params.setFont(0, bold)
        for p in self.controller["parameters"]:
            default = p["default"]
            value = ("true" if default else "false") if p["type"] in ("Bool", "Trigger") else f"{default:g}"
            QTreeWidgetItem(params, [p["name"], f"{p['type']} = {value}"])
        params.setExpanded(len(self.controller["parameters"]) <= 20)
        if layer is None:
            return
        states = layer["states"]
        top = QTreeWidgetItem(self.details, [f"States ({len(states)})", f"weight {layer.get('weight', 1):g}"])
        top.setFont(0, bold)
        for i, s in enumerate(states):
            clips = motion_clips(s.get("motion"))
            what = (clips[0].name if len(clips) == 1 else f"blend tree, {len(clips)} clips" if clips else "no motion")
            item = QTreeWidgetItem(top, [s["name"] + ("  (default)" if i == layer.get("default_state") else ""),
                                         what])
            item.setData(0, Qt.UserRole, clips[0] if clips else None)
            self._state_items[i] = item
            for clip in clips if len(clips) > 1 else []:
                c = QTreeWidgetItem(item, ["clip", clip.name])
                c.setData(0, Qt.UserRole, clip)
            if s.get("speed", 1) != 1 or s.get("speed_param"):
                QTreeWidgetItem(item, ["speed", f"{s['speed']:g}" + (f" x {s['speed_param']}" if s.get("speed_param")
                                                                     else "")])
            if s.get("tag"):
                QTreeWidgetItem(item, ["tag", s["tag"]])
            for t in s.get("transitions") or []:
                QTreeWidgetItem(item, ["→", transition_text(t, states)])
        top.setExpanded(True)
        if layer.get("any_transitions"):
            anyitem = QTreeWidgetItem(self.details, [f"Any State ({len(layer['any_transitions'])})", ""])
            anyitem.setFont(0, bold)
            for t in layer["any_transitions"]:
                QTreeWidgetItem(anyitem, ["→", transition_text(t, states)])

    def _detail_double_clicked(self, item, _column):
        clip = item.data(0, Qt.UserRole)
        if clip is not None:
            self.clip_requested.emit(clip)

    def _detail_selected(self, item, _prev):
        for i, node in self._nodes.items():
            node.setSelected(self._state_items.get(i) is item)
        index = next((i for i, it in self._state_items.items() if it is item), None)
        if index is not None and index in self._nodes:
            self.view.centerOn(self._nodes[index])

    def _node_double_clicked(self, index):
        item = self._state_items.get(index)
        clip = item.data(0, Qt.UserRole) if item is not None else None
        if clip is not None:
            self.clip_requested.emit(clip)

    def _scene_selected(self):
        picked = [i.data(0) for i in self.scene.selectedItems() if isinstance(i.data(0), int)]
        if picked and picked[0] in self._state_items:
            self.details.blockSignals(True)
            self.details.setCurrentItem(self._state_items[picked[0]])
            self.details.blockSignals(False)


def _border(rect, inside, toward):
    """Where the line inside -> toward leaves rect."""
    dx, dy = toward.x() - inside.x(), toward.y() - inside.y()
    if not dx and not dy:
        return inside
    tx = (rect.width() / 2) / abs(dx) if dx else math.inf
    ty = (rect.height() / 2) / abs(dy) if dy else math.inf
    t = min(tx, ty, 1.0)
    return QPointF(inside.x() + dx * t, inside.y() + dy * t)
