"""Mod Maker window: the replacements made for the open game, and export / install / restore of the mod."""

import os
import threading

from PySide6.QtCore import QEventLoop, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from uniview import modmaker
from uniview.constants import log
from uniview.util import open_path, safe_filename
from uniview.ui.theme import role

TITLE = "Mod Maker"


class _Relay(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object, object)  # result, exception


def run_task(parent, label, fn):
    """fn(progress, cancelled) on a worker thread with a cancellable progress dialog.
    Returns (result, error, cancelled)."""
    dlg = QProgressDialog(label, "Cancel", 0, 0, parent)
    dlg.setWindowTitle(TITLE)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(460)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.show()
    relay, stop, loop, outcome = _Relay(), threading.Event(), QEventLoop(), {}

    def show(done, total, name):
        dlg.setMaximum(total)
        dlg.setValue(done)
        dlg.setLabelText(f"{label}\n{name}")

    def finished(result, error):
        outcome["result"], outcome["error"] = result, error
        loop.quit()

    relay.progress.connect(show)
    relay.finished.connect(finished)
    dlg.canceled.connect(stop.set)

    def work():
        try:
            relay.finished.emit(fn(relay.progress.emit, stop.is_set), None)
        except Exception as e:
            if not isinstance(e, modmaker.ModError):
                log.exception("%s failed", label)
            relay.finished.emit(None, e)

    threading.Thread(target=work, name="modmaker", daemon=True).start()
    loop.exec()
    cancelled = stop.is_set()
    dlg.canceled.disconnect(stop.set)
    dlg.close()
    return outcome["result"], outcome["error"], cancelled


def replace_asset(parent, session, asset, start_dir):
    """Ask for a file and add the replacement to the game's mod. Returns the folder the file was in, or None."""
    if not modmaker.can_replace(session, asset):
        QMessageBox.information(parent, TITLE, "Mod Maker can replace Unity textures, sprites, text assets, "
                                               "data assets, fonts, sounds and models (meshes) for now.")
        return None
    hint = ""
    if asset.kind == "data":
        hint = " (edit the JSON that Save... writes for this asset)"
    elif asset.kind == "model":
        hint = " (GLB is best: Save... this model as GLB, edit it in Blender, export GLB)"
    path, _ = QFileDialog.getOpenFileName(parent, f"Replace '{asset.name}' with{hint}...", start_dir,
                                          modmaker.FILE_FILTERS[asset.kind])
    if not path:
        return None
    options = {}
    if asset.kind == "texture":
        try:
            from PIL import Image
            with session.lock:
                tex = asset.ref.read()
            size = (int(tex.m_Width), int(tex.m_Height))
            with Image.open(path) as img:
                new = img.size
        except Exception:
            size = new = None
        if size and new and size != new:
            box = QMessageBox(parent)
            box.setWindowTitle(TITLE)
            box.setIcon(QMessageBox.Question)
            box.setText(f"The game's texture is {size[0]} x {size[1]}, your image is {new[0]} x {new[1]}.")
            box.setInformativeText("Resizing to the game's size is safest. A different size works for textures on "
                                   "3D models, but sprites and UI images cut from this texture would break.")
            resize = box.addButton(f"Resize to {size[0]} x {size[1]}", QMessageBox.AcceptRole)
            keep = box.addButton(f"Keep {new[0]} x {new[1]}", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(resize)
            box.exec()
            if box.clickedButton() not in (resize, keep):
                return None
            options["keep_size"] = box.clickedButton() is keep
    project = modmaker.ModProject(session.path)
    try:
        notes = project.add(session, asset, path, options)
    except modmaker.ModError as e:
        QMessageBox.warning(parent, TITLE, str(e))
        return None
    except Exception as e:
        log.exception("Adding the replacement for '%s' failed", asset.name)
        QMessageBox.warning(parent, TITLE, f"Couldn't add the replacement: {e}")
        return None
    if notes:
        QMessageBox.information(parent, TITLE, f"'{asset.name}' added to the mod.\n\n" + "\n\n".join(notes))
    return os.path.dirname(path)


class ModMakerDialog(QDialog):
    """host: the main window (session, settings, goto_asset(), reload_game())."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.project = None
        self.setWindowTitle(TITLE)
        self.resize(980, 520)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Mod name")
        self.name.editingFinished.connect(self._rename)
        top = QHBoxLayout()
        top.addWidget(QLabel("Mod name:"))
        top.addWidget(self.name, 1)

        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Asset", "Kind", "Game file", "Replaced with", "Added"])
        self.list.setColumnWidth(0, 260)
        self.list.setColumnWidth(1, 70)
        self.list.setColumnWidth(2, 300)
        self.list.setColumnWidth(3, 180)
        self.list.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.list.setRootIsDecorated(False)
        self.list.itemDoubleClicked.connect(self._goto)

        help_text = QLabel("Right-click an asset in the list → <b>Replace with file...</b> to add it here. "
                           "Textures, sprites, text assets, data assets (JSON as <i>Save...</i> writes it), fonts, sounds and models.")
        help_text.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        role(self.status, "muted")

        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove)
        self.folder_btn = QPushButton("Open mod folder")
        self.folder_btn.setToolTip("Where this mod's replacement files and the game's backups are kept")
        self.folder_btn.clicked.connect(lambda: self.project and (os.makedirs(self.project.dir, exist_ok=True),
                                                                  open_path(self.project.dir)))
        self.export_btn = QPushButton("Export mod folder...")
        self.export_btn.setToolTip("Writes the modded game files + a README to a folder you can zip and share")
        self.export_btn.clicked.connect(self._export)
        self.install_btn = QPushButton("Install into game")
        self.install_btn.setToolTip("Backs up the game's files, then writes the modded ones into the game folder")
        self.install_btn.clicked.connect(self._install)
        self.restore_btn = QPushButton("Restore original files")
        self.restore_btn.clicked.connect(self._restore)
        buttons = QHBoxLayout()
        for b in (self.remove_btn, self.folder_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        for b in (self.export_btn, self.install_btn, self.restore_btn):
            buttons.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(help_text)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.status)
        lay.addLayout(buttons)

    # ---- state
    def refresh(self):
        session = self.host.session
        if not modmaker.can_mod(session):
            self.project = None
            self.setWindowTitle(TITLE)
            self.name.clear()
            self.list.clear()
            self.status.setText("Open a Unity game to make a mod for it." if session is None else
                                f"Mod Maker supports Unity games for now ({session.plugin.name} isn't supported yet).")
            for w in (self.name, self.remove_btn, self.folder_btn, self.export_btn, self.install_btn,
                      self.restore_btn):
                w.setEnabled(False)
            return
        self.project = p = modmaker.ModProject(session.path)
        self.setWindowTitle(f"{TITLE} - {os.path.basename(os.path.normpath(session.path))}")
        self.name.setText(p.name)
        self.list.clear()
        for e in p.edits:
            item = QTreeWidgetItem([e["name"], e["kind"], e["target"]["file"], e["original_name"],
                                    e.get("added", "")])
            item.setData(0, Qt.UserRole, e["uid"])
            item.setToolTip(3, os.path.join(p.files_dir, e["file"]))
            self.list.addTopLevelItem(item)
        files = p.changed_files()
        text = (f"{len(p.edits)} replacement(s) in {len(files)} game file(s). " if p.edits else
                "No replacements yet. ")
        if p.installed:
            text += f"Installed: {len(p.installed)} game file(s) are modded (originals are backed up)."
        else:
            text += "Not installed - the game's files are untouched."
        for note in modmaker.warnings_for(p):
            text += "\n" + note
        self.status.setText(text)
        self.name.setEnabled(True)
        self.folder_btn.setEnabled(True)
        for w in (self.remove_btn, self.export_btn, self.install_btn):
            w.setEnabled(bool(p.edits))
        self.restore_btn.setEnabled(bool(p.installed))

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def _rename(self):
        if self.project is not None and self.name.text().strip() and self.name.text().strip() != self.project.name:
            self.project.name = self.name.text().strip()
            self.project.save()

    def _goto(self, item, _column=0):
        session = self.host.session
        uid = item.data(0, Qt.UserRole)
        asset = next((a for a in session.assets if a.uid == uid), None) if session else None
        if asset is not None:
            self.host.goto_asset(asset.key)

    def _remove(self):
        if self.project is None:
            return
        for item in self.list.selectedItems():
            self.project.remove(item.data(0, Qt.UserRole))
        self.refresh()

    # ---- actions
    def _export(self):
        p, session = self.project, self.host.session
        start = self.host.settings.export_dir or self.host.settings.last_dir
        base = QFileDialog.getExistingDirectory(self, "Export mod - where to put the mod folder?", start)
        if not base:
            return
        out = os.path.join(base, safe_filename(p.name))
        if os.path.isdir(out) and os.listdir(out):
            if QMessageBox.question(self, TITLE, f"{out}\nalready exists. Write into it anyway?") != QMessageBox.Yes:
                return
        result, error, cancelled = run_task(self, "Building the mod...",
                                            lambda progress, stop: modmaker.export(p, session, out, progress, stop))
        if error is not None:
            QMessageBox.warning(self, TITLE, str(error))
            return
        notes = "".join("\n\n" + n for n in modmaker.warnings_for(p))
        if QMessageBox.question(self, TITLE, f"{'Stopped' if cancelled else 'Done'}: {len(result)} modded game "
                                f"file(s) written to\n{out}{notes}\n\nOpen the folder?") == QMessageBox.Yes:
            open_path(out)

    def _install(self):
        p, session = self.project, self.host.session
        files = p.changed_files()
        if QMessageBox.question(
                self, TITLE,
                f"Write the modded files into the game folder?\n\n{p.game_dir}\n\n" + "\n".join(files[:12])
                + ("\n..." if len(files) > 12 else "")
                + "\n\nThe originals are backed up first; 'Restore original files' puts them back. Close the game "
                  "first. UniView reloads it afterwards.") != QMessageBox.Yes:
            return
        staged, error, cancelled = run_task(self, "Building the mod...",
                                            lambda progress, stop: modmaker.stage(p, session, progress, stop))
        if error is not None or cancelled:
            if error is not None:
                QMessageBox.warning(self, TITLE, str(error))
            return
        # The loaded game reads its files lazily: let go of them before they're replaced.
        self.host.unload_for_mod()
        try:
            modmaker.install_staged(p, staged)
            message = f"Installed: {len(staged)} game file(s) replaced. Start the game to try the mod."
        except Exception as e:
            log.exception("Installing the mod failed")
            message = f"Installing stopped: {e}\n\nFiles written so far stay installed; 'Restore original files' " \
                      "undoes them."
        self.host.reload_game()
        QMessageBox.information(self, TITLE, message + "".join("\n\n" + n for n in modmaker.warnings_for(p)))
        self.refresh()

    def _restore(self):
        p = self.project
        if QMessageBox.question(self, TITLE, f"Put the game's original files back?\n\n{p.game_dir}") != QMessageBox.Yes:
            return
        self.host.unload_for_mod()
        try:
            modmaker.restore(p)
            message = "The game's original files are back. Your replacements are kept, so you can install again."
        except Exception as e:
            log.exception("Restoring the game's files failed")
            message = f"Restoring stopped: {e}\n\nClose the game and try again."
        self.host.reload_game()
        QMessageBox.information(self, TITLE, message)
        self.refresh()
