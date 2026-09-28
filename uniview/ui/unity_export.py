"""'Export as Unity project...': pick a folder, run the export with a progress dialog."""

import os
import threading
import time

from PySide6.QtCore import QEventLoop, QObject, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

from uniview.constants import log
from uniview.unity_project import export_unity_project
from uniview.util import open_path, safe_filename


def ask_project_folder(parent, game_name, start_dir):
    """Folder for the new project (<picked folder>/<game> Unity Project), or None."""
    base = QFileDialog.getExistingDirectory(parent, "Export as Unity project - where to put it?", start_dir)
    if not base:
        return None
    root = os.path.join(base, f"{safe_filename(game_name)} Unity Project")
    if os.path.isdir(root) and os.listdir(root):
        answer = QMessageBox.question(
            parent, "Export as Unity project",
            f"{root}\nalready exists.\n\nExport into it anyway? Files with the same names are replaced; "
            "anything else in it is kept.")
        if answer != QMessageBox.Yes:
            return None
    return root


class _Relay(QObject):
    """Carries the worker thread's progress and result to the UI thread."""
    progress = Signal(int, int, str)
    finished = Signal(object, object)  # (written, failed, skipped) or None, exception or None


def run_export(parent, session, root, version, editor_exe=None, game_version=""):
    """Export with a cancellable progress dialog, then offer to open the folder.

    The export runs on a worker thread (decompiling one assembly can take a minute), so the window
    keeps repainting and Cancel works; the exporter checks the cancel flag between items."""
    dlg = QProgressDialog("Exporting as a Unity project...", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Export as Unity project")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(460)
    dlg.setAutoReset(False)  # several sections each run to 100%
    dlg.setAutoClose(False)
    dlg.show()

    relay = _Relay()
    stop = threading.Event()
    loop = QEventLoop()
    outcome = {}
    last = [0.0]

    def show_progress(done, total, name):
        dlg.setMaximum(total)
        dlg.setValue(done)
        dlg.setLabelText(f"Exporting as a Unity project...\n{done:,} / {total:,}   {name[:60]}")

    def finished(result, error):
        outcome["result"], outcome["error"] = result, error
        loop.quit()

    relay.progress.connect(show_progress)
    relay.finished.connect(finished)
    dlg.canceled.connect(stop.set)

    def progress(done, total, name):  # worker thread: at most ~20 updates a second reach the UI
        now = time.monotonic()
        if now - last[0] >= 0.05 or done >= total:
            last[0] = now
            relay.progress.emit(done, total, name)

    notes = []

    def work():
        try:
            result = export_unity_project(session, root, version, progress, stop.is_set, editor_exe, notes=notes)
            relay.finished.emit(result, None)
        except Exception as e:
            log.exception("Exporting the Unity project failed")
            relay.finished.emit(None, e)

    threading.Thread(target=work, name="unity-export", daemon=True).start()
    loop.exec()
    cancelled = stop.is_set()  # before close(): closing the dialog emits canceled
    dlg.canceled.disconnect(stop.set)
    dlg.close()
    if outcome["error"] is not None:
        QMessageBox.warning(parent, "Export as Unity project", str(outcome["error"]))
        return
    written, failed, skipped = outcome["result"]
    made_with = f" (the game was made with {game_version})" if game_version and game_version != version else ""
    text = (f"{'Stopped' if cancelled else 'Done'}: {written:,} file(s) written"
            + (f", {failed:,} couldn't be exported (see the log)" if failed else "")
            + (f", {skipped:,} skipped (no data in the game files)" if skipped else "") + f".\n\n{root}\n\n"
            + (f"Open it with Unity {version}{made_with}: Unity Hub → Add → this folder." if version else
               "Open this folder with Unity Hub (Add → this folder).")
            + "\nThe first time, Unity takes a while to import everything, then builds the game's prefabs and scenes "
              "(menu: UniView → Rebuild prefabs and scenes)."
            + "".join("\n\n" + n for n in notes) + "\n\nOpen the folder now?")
    if QMessageBox.question(parent, "Export as Unity project", text) == QMessageBox.Yes:
        open_path(root)
