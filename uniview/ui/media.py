"""Image, video, audio and animation preview widgets."""

import os
import tempfile
from html import escape as html_escape

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from uniview.constants import APP_SHORT, log
from uniview.util import checker_brush, fmt_size, pil_to_pixmap, safe_filename, wheel_steps
from uniview.ui.theme import role


class ZoomGraphicsView(QGraphicsView):
    """Graphics view that zooms under the mouse on wheel and pans by dragging."""

    zoomed = Signal()
    MIN_SCALE, MAX_SCALE = 0.02, 64.0

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(checker_brush())
        self.setFrameShape(QGraphicsView.NoFrame)

    def scale_factor(self):
        return self.transform().m11()

    def zoom_by(self, factor):
        new = min(max(self.scale_factor() * factor, self.MIN_SCALE), self.MAX_SCALE)
        factor = new / self.scale_factor()
        self.scale(factor, factor)
        self.zoomed.emit()

    def wheelEvent(self, event):
        self.zoom_by(1.25 ** wheel_steps(event))

def region_color(i, alpha=255):
    """Distinct colour for sprite sheet region number i (golden-ratio hues)."""
    c = QColor.fromHsvF((i * 0.618034) % 1.0, 0.75, 1.0)
    c.setAlpha(alpha)
    return c


class SheetMap(QWidget):
    """A sprite sheet shrunk to fit, with the region(s) the current frame is cut from highlighted and the other
    frames' regions faintly outlined, each region in its own colour (region_color)."""

    MAX_SIDE = 1024  # the sheet is shrunk to this once; painting scales it further

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pix = None
        self.scale = 1.0  # sheet pixels -> self.pix pixels
        self.sheet_h = 0
        self.current = []  # [(x, y from bottom, w, h)] in sheet pixels
        self.others = []
        self.setMinimumHeight(120)

    def set_sheet(self, img):
        """img: PIL image of the sheet, or None to clear."""
        self.pix, self.current, self.others = None, [], []
        if img is not None:
            self.scale = min(1.0, self.MAX_SIDE / max(img.width, img.height, 1))
            if self.scale < 1.0:
                img = img.resize((max(1, round(img.width * self.scale)), max(1, round(img.height * self.scale))))
            self.pix = pil_to_pixmap(img)
            self.sheet_h = img.height / self.scale
        self.update()

    def set_regions(self, current, others=()):
        self.current, self.others = list(current), list(others)
        self.update()

    def heightForWidth(self, w):
        return self._fit_height(w)

    def hasHeightForWidth(self):
        return self.pix is not None

    def _fit_height(self, w):
        if self.pix is None or self.pix.width() == 0:
            return 120
        return max(60, round(w * self.pix.height() / self.pix.width()))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), checker_brush())
        if self.pix is None:
            painter.end()
            return
        pw, ph = self.pix.width(), self.pix.height()
        k = min(self.width() / pw, self.height() / ph)
        dw, dh = pw * k, ph * k
        ox, oy = (self.width() - dw) / 2, (self.height() - dh) / 2
        painter.setRenderHint(QPainter.SmoothPixmapTransform, k < 1)
        painter.drawPixmap(QRectF(ox, oy, dw, dh), self.pix, QRectF(0, 0, pw, ph))
        # Darken the sheet a little so the outlines read on any colours.
        painter.fillRect(QRectF(ox, oy, dw, dh), QColor(0, 0, 0, 70))
        s = self.scale * k

        def box(r):
            x, y, w, h = r
            return QRectF(ox + x * s, oy + (self.sheet_h - y - h) * s, max(w * s, 2.0), max(h * s, 2.0))

        number = {r: i for i, r in enumerate(self.others)}
        painter.setBrush(Qt.NoBrush)
        for i, r in enumerate(self.others):
            painter.setPen(QPen(region_color(i, 110), 1))
            painter.drawRect(box(r))
        for r in self.current:
            i = number.get(r, 0)
            painter.setPen(QPen(region_color(i), 2))
            painter.setBrush(region_color(i, 50))
            painter.drawRect(box(r))
        painter.end()


class ImageView(QWidget):
    """Texture viewer: wheel to zoom, drag to pan, save as PNG. Optional 'used by' links on the right, and under
    them a text box (set_details) - e.g. the code that drives a sprite animation."""

    save_requested = Signal()
    goto_requested = Signal(object)  # asset key
    MAX_FIT = 2.0  # Fit never zooms small images past this

    def __init__(self, parent=None, show_links=True):
        super().__init__(parent)
        self.image = None
        self.scene = QGraphicsScene(self)
        self.pix_item = QGraphicsPixmapItem()
        self.scene.addItem(self.pix_item)
        self.view = ZoomGraphicsView(self.scene)
        self.view.zoomed.connect(self._update_zoom)

        fit = QPushButton("Fit")
        fit.clicked.connect(self.fit)
        actual = QPushButton("100%")
        actual.clicked.connect(self.actual_size)
        zoom_in = QPushButton("+")
        zoom_in.clicked.connect(lambda: self.view.zoom_by(1.25))
        zoom_out = QPushButton("−")
        zoom_out.clicked.connect(lambda: self.view.zoom_by(0.8))
        for b in (zoom_in, zoom_out):
            b.setFixedWidth(32)
        self.zoom_label = QLabel()
        self.info = QLabel()
        save = QPushButton("Save PNG...")
        save.clicked.connect(self.save_requested)
        self.outlines = QCheckBox("Sprite outlines")
        self.outlines.setToolTip("Outline the sprites cut from this texture (sprite sheet)")
        self.outlines.setChecked(True)
        self.outlines.toggled.connect(lambda _on: self._refresh_pixmap())
        self.outlines.hide()
        self.rects = []
        self.sprite_boxes = QCheckBox("Sprite boxes")
        self.sprite_boxes.setToolTip("Box each piece of the sprite sheet the playing frame is made of, in the same "
                                     "colour as on the sheet in the side bar")
        self.sprite_boxes.hide()
        self._overlay = []  # graphics items of set_overlay
        self.layers_3d = QCheckBox("3D")
        self.layers_3d.setToolTip("Pull the pieces of the frame apart in depth (back to front) and tilt the view")
        self.layers_3d.hide()
        self.turn = QSlider(Qt.Horizontal, minimum=-80, maximum=80, value=35)
        self.turn.setToolTip("Turn the 3D view left / right")
        self.tilt = QSlider(Qt.Horizontal, minimum=-60, maximum=60, value=15)
        self.tilt.setToolTip("Tilt the 3D view up / down")
        self.depth = QSlider(Qt.Horizontal, minimum=0, maximum=100, value=30)
        self.depth.setToolTip("Space between the layers")
        self.layer_controls = QWidget()
        controls = QHBoxLayout(self.layer_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        for text, slider in (("Turn", self.turn), ("Tilt", self.tilt), ("Depth", self.depth)):
            slider.setFixedWidth(90)
            controls.addWidget(QLabel(text))
            controls.addWidget(slider)
        self.layer_controls.hide()
        self.layers_3d.toggled.connect(self.layer_controls.setVisible)
        self._layers = []  # graphics items of show_layers

        bar = QHBoxLayout()
        for w in (fit, actual, zoom_in, zoom_out, self.zoom_label, self.outlines, self.sprite_boxes, self.layers_3d,
                  self.layer_controls):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.info)
        bar.addWidget(save)
        self.links_title = QLabel()
        self.links_title.setWordWrap(True)
        self.links = QListWidget()
        self.links.setIconSize(QSize(48, 48))
        self.links.setWordWrap(True)
        self.links.itemActivated.connect(self._link_clicked)
        self.links.itemClicked.connect(self._link_clicked)
        links_box = QWidget()
        links_lay = QVBoxLayout(links_box)
        links_lay.setContentsMargins(6, 0, 0, 0)
        links_lay.addWidget(self.links_title)
        links_lay.addWidget(self.links, 1)
        self.links_hint = QLabel("Click one to jump to it.")
        role(self.links_hint, "muted")
        links_lay.addWidget(self.links_hint)
        self._link_items = {}
        self.map_title = QLabel()
        self.map_title.setWordWrap(True)
        self.sheet_map = SheetMap()
        self.map_info = QLabel()
        self.map_info.setWordWrap(True)
        role(self.map_info, "muted")
        self.map_box = QWidget()
        map_lay = QVBoxLayout(self.map_box)
        map_lay.setContentsMargins(6, 0, 0, 0)
        map_lay.addWidget(self.map_title)
        map_lay.addWidget(self.sheet_map, 1)
        map_lay.addWidget(self.map_info)
        self.map_box.hide()
        self.details_title = QLabel()
        self.details_title.setWordWrap(True)
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.details.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.details_box = QWidget()
        details_lay = QVBoxLayout(self.details_box)
        details_lay.setContentsMargins(6, 0, 0, 0)
        details_lay.addWidget(self.details_title)
        details_lay.addWidget(self.details, 1)
        self.details_box.hide()
        side = QSplitter(Qt.Vertical)
        side.addWidget(links_box)
        side.addWidget(self.map_box)
        side.addWidget(self.details_box)
        side.setStretchFactor(2, 1)
        side.setSizes([140, 260, 400])
        side.setVisible(show_links)

        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 260])
        self.split = split

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

    def set_links(self, title, entries, empty_text):
        """entries: [(key, text, icon)] shown in the side list. Hides the details box (set_details shows it)."""
        self.links_title.setText(f"<b>{title}</b>")
        self.links_hint.setVisible(bool(entries))
        self.set_details("", "")
        self.set_sheet_map(None)
        self.sprite_boxes.hide()
        self.layers_3d.hide()
        self.layer_controls.hide()
        self.links.clear()
        self._link_items = {}
        if not entries:
            item = QListWidgetItem(empty_text)
            item.setFlags(Qt.NoItemFlags)
            self.links.addItem(item)
        for key, text, icon in entries:
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, key)
            self.links.addItem(item)
            self._link_items[key] = item

    def set_details(self, title, text):
        """Text under the side list (monospace, scrollable); hidden when `text` is empty."""
        self.details_title.setText(f"<b>{html_escape(title)}</b>" if title else "")
        self.details.setPlainText(text)
        self.details_box.setVisible(bool(text))
        sizes = self.split.sizes()
        if text and sizes[1] < 420 and sum(sizes) > 900:  # room for the code
            self.split.setSizes([sum(sizes) - 420, 420])

    def set_sheet_map(self, img, title=""):
        """Show the sheet `img` (PIL) under the side list, for set_regions; None hides it."""
        self.sheet_map.set_sheet(img)
        self.map_title.setText(f"<b>{html_escape(title)}</b>" if title else "")
        self.map_info.setText("")
        self.map_box.setVisible(img is not None)

    def set_regions(self, current, others=(), info=""):
        """Highlight `current` [(x, y from bottom, w, h)] on the sheet map, outline `others` faintly."""
        self.sheet_map.set_regions(current, others)
        self.map_info.setText(info)

    def set_link_icon(self, key, icon):
        item = self._link_items.get(key)
        if item is not None:
            item.setIcon(icon)

    def _link_clicked(self, item):
        key = item.data(Qt.UserRole)
        if key is not None:
            self.goto_requested.emit(key)

    def show_image(self, img, name="", rects=None, fit=True):
        """rects: sprites cut from this image [(x, y from bottom, w, h)] - drawn as outlines."""
        self.clear_overlay()
        self.clear_layers()
        self.image = img
        self.rects = list(rects or [])
        self.outlines.setVisible(bool(self.rects))
        self._refresh_pixmap()
        self.info.setText(f"{name}  {img.width}x{img.height}" + (f"  \u00b7 {len(self.rects)} sprites"
                                                                 if self.rects else ""))
        if fit:
            QTimer.singleShot(0, self.fit)  # after the view has its final size

    def _refresh_pixmap(self):
        if self.image is None:
            return
        pix = pil_to_pixmap(self.image)
        if self.rects and self.outlines.isChecked():
            painter = QPainter(pix)
            pen = QPen(QColor(0, 255, 140, 220))
            pen.setWidth(max(1, round(max(self.image.size) / 1024)))
            painter.setPen(pen)
            h = self.image.height
            for x, y, w, rh in self.rects:
                painter.drawRect(QRectF(x, h - y - rh, w, rh))
            painter.end()
        self.pix_item.setPixmap(pix)
        self.scene.setSceneRect(QRectF(pix.rect()))

    def set_overlay(self, shapes):
        """Coloured boxes over the shown image: shapes [(corners [(x, y)] in image pixels with y from the top,
        colour number (region_color; -1 = grey), label)]. Replaced by the next show_image / set_overlay."""
        self.clear_overlay()
        label_them = len(shapes) <= 200  # past that the labels are just noise
        for corners, number, label in shapes:
            color = region_color(number) if number >= 0 else QColor(200, 200, 200)
            item = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in corners]))
            pen = QPen(color, 2)
            pen.setCosmetic(True)  # same width at any zoom
            item.setPen(pen)
            fill = QColor(color)
            fill.setAlpha(40 if len(shapes) < 4 else 15)  # many overlapping pieces: mostly outlines
            item.setBrush(fill)
            item.setZValue(1)
            self.scene.addItem(item)
            if label_them and label:
                text = QGraphicsSimpleTextItem(label, item)
                text.setFlag(QGraphicsItem.ItemIgnoresTransformations)  # same size at any zoom
                text.setPos(min(x for x, _y in corners), min(y for _x, y in corners))
                text.setBrush(color)
                font = text.font()
                font.setBold(True)
                text.setFont(font)
                back = QGraphicsRectItem(text.boundingRect().adjusted(-2, 0, 2, 0), text)  # readable on any image
                back.setFlag(QGraphicsItem.ItemStacksBehindParent)
                back.setPen(Qt.NoPen)
                back.setBrush(QColor(0, 0, 0, 170))
            self._overlay.append(item)

    def show_layers(self, size, layers, name="", boxes=False):
        """A frame pulled apart in 3D: size (w, h) of the frame; layers [(piece QPixmap, corners [(x, y)] in the
        frame (y down) its top-left, top-right, bottom-right, bottom-left land at, colour number)] back to front.
        The Turn / Tilt / Depth sliders set the view; boxes: outline each piece too."""
        self.clear_overlay()
        self.clear_layers()
        self.image = None
        self.pix_item.setPixmap(QPixmap())
        w, h = size
        yaw, pitch = np.radians(self.turn.value()), np.radians(self.tilt.value())
        spacing = self.depth.value() / 100 * max(w, h) / max(len(layers), 1) * 2.5
        cy, sy, cp, sp = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch)
        rot = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]]) @ np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        distance = max(w, h) * 3.0  # camera distance: mild perspective
        middle = (len(layers) - 1) / 2
        for i, (piece, corners, number) in enumerate(layers):
            z = (middle - i) * spacing  # back layers further away
            # Frame point (u, v) -> 3D (u - w/2, v - h/2, z) -> rotated -> projected: a homography for the plane.
            m = rot @ np.array([[1, 0, -w / 2], [0, 1, -h / 2], [0, 0, z]], float)
            m[2] += (0, 0, distance)
            m[0:2] *= distance
            plane = QTransform(m[0, 0], m[1, 0], m[2, 0], m[0, 1], m[1, 1], m[2, 1], m[0, 2], m[1, 2], m[2, 2])
            pw, ph = piece.width(), piece.height()
            to_frame = QTransform.quadToQuad(
                QPolygonF([QPointF(0, 0), QPointF(pw, 0), QPointF(pw, ph), QPointF(0, ph)]),
                QPolygonF([QPointF(x, y) for x, y in corners]))
            if to_frame is None:
                continue
            item = QGraphicsPixmapItem(piece)
            item.setTransformationMode(Qt.SmoothTransformation)
            item.setTransform(to_frame * plane)
            item.setZValue(i)
            self.scene.addItem(item)
            self._layers.append(item)
            if boxes:
                outline = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in corners]))
                pen = QPen(region_color(number) if number >= 0 else QColor(200, 200, 200), 2)
                pen.setCosmetic(True)
                outline.setPen(pen)
                outline.setTransform(plane)
                outline.setZValue(i + 0.5)
                self.scene.addItem(outline)
                self._layers.append(outline)
        side = max(w, h) * 1.5  # fixed bounds around the frame's centre: the view doesn't jump between frames
        self.scene.setSceneRect(QRectF(-side, -side, 2 * side, 2 * side))
        self.info.setText(f"{name}  {len(layers)} layers")

    def clear_layers(self):
        for item in self._layers:
            self.scene.removeItem(item)
        self._layers = []

    def clear_overlay(self):
        for item in self._overlay:
            self.scene.removeItem(item)
        self._overlay = []

    def fit(self):
        """Shrink big images to fit the view; small ones show at MAX_FIT (the same zoom every time, not blown up
        to fill the view)."""
        self.view.resetTransform()
        rect = self.scene.itemsBoundingRect() if self._layers else self.pix_item.sceneBoundingRect()
        self.view.fitInView(rect, Qt.KeepAspectRatio)
        if self.view.scale_factor() > self.MAX_FIT:
            self.view.resetTransform()
            self.view.scale(self.MAX_FIT, self.MAX_FIT)
            self.view.centerOn(rect.center())
        self._update_zoom()

    def actual_size(self):
        self.view.resetTransform()
        self._update_zoom()

    def _update_zoom(self):
        scale = self.view.scale_factor()
        # Blend when shrinking, keep pixels crisp when magnifying.
        mode = Qt.SmoothTransformation if scale < 1 else Qt.FastTransformation
        self.pix_item.setTransformationMode(mode)
        self.zoom_label.setText(f"{scale * 100:.0f}%")

class VideoView(QWidget):
    """Video player: picture, play/pause, seek, volume, save."""

    save_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.settings = settings
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(settings.volume)
        self.player.setAudioOutput(self.output)
        self.screen = QVideoWidget()
        self.screen.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.screen)
        self._file_index = 0

        self.title = QLabel()
        role(self.title, "heading")
        self.status = QLabel()
        self.status.setWordWrap(True)
        role(self.status, "warn")
        self.play_btn = QPushButton("\u25b6 Play")
        self.play_btn.setMinimumWidth(110)
        self.play_btn.clicked.connect(self.toggle)
        stop = QPushButton("\u25a0 Stop")
        stop.clicked.connect(self.player.stop)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("0:00 / 0:00")
        self.loop = QCheckBox("Loop")
        self.loop.setChecked(True)
        save = QPushButton("Save video...")
        save.clicked.connect(self.save_requested)
        controls = QHBoxLayout()
        for w in (self.play_btn, stop):
            controls.addWidget(w)
        controls.addWidget(self.seek, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.loop)
        controls.addWidget(save)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.screen, 1)
        lay.addLayout(controls)
        lay.addWidget(self.status)
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(lambda ms: self.seek.setRange(0, ms))
        self.player.playbackStateChanged.connect(self._state)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(lambda _e, text: self.status.setText(f"Can't play this video: {text}"))

    def _position(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time.setText(f"{AudioView._fmt(ms)} / {AudioView._fmt(self.player.duration())}")

    def _state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        self.play_btn.setText("\u23f8 Pause" if state == QMediaPlayer.PlayingState else "\u25b6 Play")

    def _media_status(self, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.EndOfMedia and self.loop.isChecked():
            self.player.setPosition(0)
            self.player.play()

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def load(self, name, data, ext):
        self.stop()
        folder = os.path.join(tempfile.gettempdir(), APP_SHORT)
        os.makedirs(folder, exist_ok=True)
        self._file_index ^= 1
        path = os.path.join(folder, f"video_{self._file_index}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        self.title.setText(f"{name}   ({ext.upper()}, {fmt_size(len(data))})")
        self.status.setText("")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

    def show_error(self, name, message):
        self.stop()
        self.title.setText(name)
        self.status.setText(f"{message}\n\nSave video... still exports the file as it is stored in the game.")

class AudioView(QWidget):
    """Sound player: play/pause, seek, volume, save."""

    save_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self.settings = settings
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(settings.volume)
        self.player.setAudioOutput(self.output)
        self._file_index = 0

        self.title = QLabel()
        self.title.setWordWrap(True)
        role(self.title, "heading")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setWordWrap(True)
        role(self.status, "warn")

        self.play_btn = QPushButton("\u25b6 Play")
        self.play_btn.setMinimumWidth(110)
        self.play_btn.clicked.connect(self.toggle)
        stop = QPushButton("\u25a0 Stop")
        stop.clicked.connect(self.player.stop)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("0:00 / 0:00")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(self.output.volume() * 100))
        self.volume.setFixedWidth(120)
        self.volume.valueChanged.connect(self._set_volume)
        self.autoplay = QCheckBox("Autoplay")
        self.autoplay.setToolTip("Start playing as soon as a sound is picked (handy for flipping through sounds)")
        self.autoplay.setChecked(settings.autoplay)
        self.autoplay.toggled.connect(lambda on: (setattr(settings, "autoplay", on), settings.save()))
        save = QPushButton("Save sound...")
        save.clicked.connect(self.save_requested)

        controls = QHBoxLayout()
        for w in (self.play_btn, stop):
            controls.addWidget(w)
        controls.addWidget(self.seek, 1)
        controls.addWidget(self.time)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Volume"))
        controls.addWidget(self.volume)
        controls.addWidget(self.autoplay)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(self.title)
        lay.addWidget(self.details)
        lay.addSpacing(12)
        lay.addLayout(controls)
        lay.addWidget(self.status)
        lay.addStretch()
        lay.addWidget(save, 0, Qt.AlignLeft)

        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(self._duration)
        self.player.playbackStateChanged.connect(self._state)
        self.player.errorOccurred.connect(lambda _e, text: self.status.setText(f"Can't play this sound: {text}"))

    @staticmethod
    def _fmt(ms):
        sec = max(0, ms) // 1000
        return f"{sec // 60}:{sec % 60:02d}"

    def _set_volume(self, value):
        self.output.setVolume(value / 100)
        self.settings.volume = value / 100
        self.settings.save()

    def _position(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time.setText(f"{self._fmt(ms)} / {self._fmt(self.player.duration())}")

    def _duration(self, ms):
        self.seek.setRange(0, ms)
        self.time.setText(f"{self._fmt(self.player.position())} / {self._fmt(ms)}")

    def _state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        self.play_btn.setText("\u23f8 Pause" if state == QMediaPlayer.PlayingState else "\u25b6 Play")

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def load(self, name, data, ext, rows):
        """Play-ready bytes (wav/mp3/ogg...) -> a temp file the player opens."""
        self.stop()
        folder = os.path.join(tempfile.gettempdir(), APP_SHORT)
        os.makedirs(folder, exist_ok=True)
        self._file_index ^= 1  # alternate files: the previous one may still be locked by the player
        path = os.path.join(folder, f"preview_{self._file_index}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        self.title.setText(name)
        self.details.setText("<br>".join(f"<b>{html_escape(k)}:</b> {html_escape(v)}" for k, v in rows)
                             + f"<br><b>Plays as:</b> {ext.upper()}, {fmt_size(len(data))}")
        self.status.setText("")
        self.seek.setRange(0, 0)
        self.time.setText("0:00 / 0:00")
        self.player.setSource(QUrl.fromLocalFile(path))
        if self.autoplay.isChecked():
            self.player.play()

    def show_error(self, name, message, rows):
        self.stop()
        self.title.setText(name)
        self.details.setText("<br>".join(f"<b>{html_escape(k)}:</b> {html_escape(v)}" for k, v in rows))
        self.status.setText(f"{message}\n\nSave sound... still exports the file as it is stored in the game.")
        self.seek.setRange(0, 0)
        self.time.setText("")

class AnimationView(QWidget):
    """An animation clip: its summary, and the models it can be played on."""

    play_requested = Signal(object)  # model Asset
    export_requested = Signal(object)  # model Asset: save it rigged + animated
    flipbook_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.targets = QComboBox()
        self.targets.setMinimumWidth(320)
        self.play = QPushButton("\u25b6 Play on model")
        self.play.clicked.connect(self._play)
        self.export = QPushButton("Save animated GLB...")
        self.export.setToolTip("The model with its skeleton and this animation, e.g. for Blender")
        self.export.clicked.connect(self._export)
        self.flipbook = QPushButton("\u25b6 Play sprite animation")
        self.flipbook.clicked.connect(self.flipbook_requested)
        self.flipbook.hide()
        self.note = QLabel()
        role(self.note, "muted")
        row = QHBoxLayout()
        row.addWidget(QLabel("Model:"))
        row.addWidget(self.targets, 1)
        row.addWidget(self.play)
        row.addWidget(self.export)
        row.addWidget(self.flipbook)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        lay.addWidget(self.note)
        lay.addWidget(self.text, 1)

    def show_clip(self, text, targets, note="", flipbook=0):
        self.text.setPlainText(text)
        self.flipbook.setVisible(bool(flipbook))
        self.flipbook.setText(f"\u25b6 Play sprite animation ({flipbook} frames)")
        if flipbook and not targets:
            note = note or "A 2D sprite animation: press Play sprite animation to watch it."
        self.targets.clear()
        for model in targets:
            self.targets.addItem(model.name, model)
        self.play.setEnabled(bool(targets))
        self.export.setEnabled(bool(targets))
        self.targets.setEnabled(bool(targets))
        self.note.setText(note or ("Pick a model to play this animation on (best matches first)." if targets else
                                   "No model with a matching skeleton was found for this animation."))

    def _play(self):
        model = self.targets.currentData()
        if model is not None:
            self.play_requested.emit(model)

    def _export(self):
        model = self.targets.currentData()
        if model is not None:
            self.export_requested.emit(model)

class ImageWindow(QDialog):
    """Separate zoomable image window (used for UV layouts)."""

    def __init__(self, img, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 900)
        self.view = ImageView(show_links=False)
        self.view.save_requested.connect(self._save)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self.view.show_image(img, title)
        self._title = title

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save", f"{safe_filename(self._title)}.png", "PNG image (*.png)")
        if path:
            self.view.image.save(path)
            log.info("Saved %s", path)
