"""Prefab/scene structure view: UI prefabs drawn as a 2D picture, and for anything with nothing to draw in 3D
(UI, sound or logic-only prefabs) the objects, components and assets it uses."""

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFontDatabase, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsPolygonItem,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from uniview import prefab_info
from uniview.ui.media import ImageView


class PrefabView(QWidget):
    """GameObject tree (left), the selected object's components (middle), assets it uses (right)."""

    goto_requested = Signal(object)  # asset key
    save_requested = Signal()        # save the picture

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes, self.by_uid, self.visible = [], {}, []
        self.boxes, self.to_image = {}, None  # UI picture: node -> canvas corners, canvas -> image transform
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.header.setTextFormat(Qt.RichText)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Object", "Components"])
        self.tree.setColumnWidth(0, 220)
        self.tree.currentItemChanged.connect(self._node_selected)
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.details.setLineWrapMode(QPlainTextEdit.NoWrap)

        self.links_title = QLabel()
        self.links_title.setWordWrap(True)
        self.links = QListWidget()
        self.links.setIconSize(QSize(48, 48))
        self.links.setWordWrap(True)
        self.links.itemActivated.connect(self._link_clicked)
        self.links.itemClicked.connect(self._link_clicked)
        self._link_items = {}
        links_box = QWidget()
        links_lay = QVBoxLayout(links_box)
        links_lay.setContentsMargins(6, 0, 0, 0)
        links_lay.addWidget(self.links_title)
        links_lay.addWidget(self.links, 1)
        hint = QLabel("Click one to jump to it.")
        hint.setStyleSheet("color: gray;")
        links_lay.addWidget(hint)

        self.picture = ImageView(show_links=False)
        self.picture.save_requested.connect(self.save_requested)
        self.outline = QGraphicsPolygonItem()
        pen = QPen(QColor(0, 255, 140))
        pen.setCosmetic(True)
        pen.setWidth(2)
        self.outline.setPen(pen)
        self.outline.setZValue(1)
        self.picture.scene.addItem(self.outline)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.picture, "Picture")
        self.tabs.addTab(self.details, "Components")
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.tabs)
        split.addWidget(links_box)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 600, 260])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header)
        lay.addWidget(split, 1)

    def show_prefab(self, name, reason, nodes, by_uid, used, icon, picture=None):
        """nodes: session.hierarchy(); used: [Asset] for the link list; icon: placeholder icon for links;
        picture: (PIL image, {node: canvas corners}, QTransform canvas -> image) of a UI prefab, or None."""
        self.nodes, self.by_uid = nodes, by_uid
        self.visible = prefab_info.visible_flags(nodes)
        self.header.setText(f"<b>{name}</b><br><span style='color: gray;'>{reason}"
                            f"{prefab_info.summary(nodes)}</span>")
        self.outline.setPolygon(QPolygonF())
        if picture is not None:
            img, self.boxes, self.to_image = picture
            self.picture.show_image(img, name)
        else:
            self.boxes, self.to_image = {}, None
            self.picture.image = None
            self.picture.pix_item.setPixmap(QPixmap())
        self.tabs.setTabEnabled(0, picture is not None)
        self.tabs.setCurrentIndex(0 if picture is not None else 1)
        self.tree.clear()
        items = []
        for i, n in enumerate(nodes):
            item = QTreeWidgetItem([n["name"], ", ".join(prefab_info.component_names(n))])
            item.setData(0, Qt.UserRole, i)
            if not self.visible[i]:
                for col in (0, 1):
                    item.setForeground(col, QBrush(QColor(128, 128, 128)))
            parent = n.get("parent", -1)
            if 0 <= parent < len(items):
                items[parent].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            items.append(item)
        self.tree.expandToDepth(2)
        if items:
            self.tree.setCurrentItem(items[0])
        else:
            self.details.setPlainText("No objects found.")

        self.links.clear()
        self._link_items = {}
        self.links_title.setText(f"<b>Uses {len(used)} asset(s)</b>")
        if not used:
            item = QListWidgetItem("No sprites, sounds or other assets referenced.")
            item.setFlags(Qt.NoItemFlags)
            self.links.addItem(item)
        for asset in used:
            item = QListWidgetItem(icon, f"{asset.name.rsplit('/', 1)[-1]}\n({asset.kind})")
            item.setData(Qt.UserRole, asset.key)
            self.links.addItem(item)
            self._link_items[asset.key] = item

    def set_link_icon(self, key, icon):
        item = self._link_items.get(key)
        if item is not None:
            item.setIcon(icon)

    def _node_selected(self, item, _previous=None):
        if item is None:
            return
        i = item.data(0, Qt.UserRole)
        self.details.setPlainText(prefab_info.node_text(self.nodes, i, self.by_uid, self.visible))
        pts = self.boxes.get(i)
        if pts is None or self.to_image is None:
            self.outline.setPolygon(QPolygonF())
        else:
            self.outline.setPolygon(QPolygonF([self.to_image.map(QPointF(float(x), float(y))) for x, y in pts]))

    def _link_clicked(self, item):
        key = item.data(Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)
