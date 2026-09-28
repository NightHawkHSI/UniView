"""'Export as Unity project...': pick a folder, run the export with a progress dialog."""

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog

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


def run_export(parent, session, root, version, editor_exe=None, game_version=""):
    """Export with a cancellable progress dialog, then offer to open the folder."""
    dlg = QProgressDialog("Exporting as a Unity project...", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Export as Unity project")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(460)
    dlg.show()

    def progress(done, total, name):
        dlg.setMaximum(total)
        dlg.setValue(done)
        dlg.setLabelText(f"Exporting as a Unity project...\n{done:,} / {total:,}   {name[:60]}")
        QApplication.processEvents()

    QApplication.processEvents()
    try:
        notes = []
        written, failed, skipped = export_unity_project(session, root, version, progress, dlg.wasCanceled,
                                                        editor_exe, notes=notes)
    except Exception as e:
        log.exception("Exporting the Unity project failed")
        dlg.close()
        QMessageBox.warning(parent, "Export as Unity project", str(e))
        return
    cancelled = dlg.wasCanceled()
    dlg.close()
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
