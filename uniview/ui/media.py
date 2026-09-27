"""Image, video, audio and animation preview widgets."""

import os
import tempfile
from html import escape as html_escape

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
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

class ImageView(QWidget):
    """Texture viewer: wheel to zoom, drag to pan, save as PNG. Optional 'used by' links on the right."""

    save_requested = Signal()
    goto_requested = Signal(object)  # asset key

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

        bar = QHBoxLayout()
        for w in (fit, actual, zoom_in, zoom_out, self.zoom_label, self.outlines):
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
        hint = QLabel("Click one to jump to it.")
        hint.setStyleSheet("color: gray;")
        links_lay.addWidget(hint)
        links_box.setVisible(show_links)
        self._link_items = {}

        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(links_box)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 260])

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

    def set_links(self, title, entries, empty_text):
        """entries: [(key, text, icon)] shown in the side list."""
        self.links_title.setText(f"<b>{title}</b>")
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

    def fit(self):
        self.view.resetTransform()
        self.view.fitInView(self.pix_item, Qt.KeepAspectRatio)
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
        self.title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #e0a030;")
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
        self.title.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #e0a030;")

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
        self.note.setStyleSheet("color: gray;")
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
