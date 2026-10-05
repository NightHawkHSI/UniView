"""File -> Decompile game code...: pick the game's assemblies, decompile them to C# projects with ILSpy."""

import threading
import time

from PySide6.QtCore import QEventLoop, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from uniview import decompile_game
from uniview.constants import log
from uniview.ui.theme import role
from uniview.unity_scripts import find_ilspycmd
from uniview.util import fmt_size, open_path


class DecompileDialog(QDialog):
    def __init__(self, game_name, kind, assemblies, out_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Decompile game code - {game_name}")
        self.resize(560, 620)
        note = ("Mono game: the full C# code, method bodies included." if kind == "mono" else
                "IL2CPP game: classes, fields and method signatures from Cpp2IL - no method bodies.")
        intro = QLabel(f"<b>{game_name}</b><br>{note}<br>Game code is picked; bundled libraries are listed "
                       "but left out unless you tick them.")
        intro.setWordWrap(True)

        self.list = QListWidget()
        for name, size, picked in assemblies:
            item = QListWidgetItem(f"{name}    {fmt_size(size)}")
            item.setData(Qt.UserRole, name)
            item.setData(Qt.UserRole + 1, picked)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if picked else Qt.Unchecked)
            self.list.addItem(item)
        self.list.itemChanged.connect(self._update_count)
        buttons = QHBoxLayout()
        for text, mode in (("Game code", "default"), ("All", "all"), ("None", "none")):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, m=mode: self._select(m))
            buttons.addWidget(btn)
        buttons.addStretch()
        self.count = role(QLabel(), "muted")
        buttons.addWidget(self.count)

        self.out = QLineEdit(out_dir)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out, 1)
        out_row.addWidget(browse)
        legal = role(QLabel("The code still belongs to the game's makers - fine for studying and modding your "
                            "own copy, not for publishing."), "muted")
        legal.setWordWrap(True)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Decompile")
        role(box.button(QDialogButtonBox.Ok), "primary")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.list, 1)
        lay.addLayout(buttons)
        lay.addWidget(QLabel("Save to (one folder per assembly, each with a .csproj):"))
        lay.addLayout(out_row)
        lay.addWidget(legal)
        lay.addWidget(box)
        self._update_count()

    def _select(self, mode):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            on = mode == "all" or (mode == "default" and item.data(Qt.UserRole + 1))
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.list.blockSignals(False)
        self._update_count()

    def _update_count(self, *_):
        n = len(self.chosen())
        self.count.setText(f"{n} of {self.list.count()} picked")

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Save the decompiled code in", self.out.text())
        if path:
            self.out.setText(path)

    def chosen(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class _Relay(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object, object)  # result, exception


def _run_in_thread(parent, title, label, work):
    """Run work(progress, cancelled) on a worker thread behind a cancellable progress dialog.
    Returns (result, error, cancelled)."""
    dlg = QProgressDialog(label, "Cancel", 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(460)
    dlg.setAutoReset(False)
    dlg.setAutoClose(False)
    dlg.show()
    relay, stop, loop, outcome, last = _Relay(), threading.Event(), QEventLoop(), {}, [0.0]

    def show_progress(done, total, name):
        dlg.setMaximum(total)
        dlg.setValue(done)
        dlg.setLabelText(f"{label}\n{done:,} / {total:,}   {name[:60]}")

    def finished(result, error):
        outcome["result"], outcome["error"] = result, error
        loop.quit()

    relay.progress.connect(show_progress)
    relay.finished.connect(finished)
    dlg.canceled.connect(stop.set)

    def progress(done, total, name):
        now = time.monotonic()
        if now - last[0] >= 0.05 or done >= total:
            last[0] = now
            relay.progress.emit(done, total, name)

    def run():
        try:
            relay.finished.emit(work(progress, stop.is_set), None)
        except Exception as e:
            log.exception("%s failed", title)
            relay.finished.emit(None, e)

    threading.Thread(target=run, name="decompile-game", daemon=True).start()
    loop.exec()
    cancelled = stop.is_set()
    dlg.canceled.disconnect(stop.set)
    dlg.close()
    return outcome.get("result"), outcome.get("error"), cancelled


def decompile_game_code(parent, game_dir, game_name):
    """The whole flow: check ILSpy, find the assemblies (running Cpp2IL for IL2CPP), ask, decompile, report."""
    title = "Decompile game code"
    ilspycmd = find_ilspycmd()
    if ilspycmd is None:
        QMessageBox.information(parent, title, "This needs the ILSpy decompiler.\n\n"
                                "Install it under Help → Optional tools..., then try again.")
        return
    found, error, cancelled = _run_in_thread(
        parent, title, "Finding the game's code (IL2CPP games run Cpp2IL first, which can take minutes)...",
        lambda _progress, _cancelled: decompile_game.code_folder(game_dir))
    if cancelled:
        return
    if error is not None:
        QMessageBox.warning(parent, title, str(error))
        return
    folder, kind = found
    if folder is None:
        QMessageBox.warning(parent, title, kind)
        return
    assemblies = decompile_game.list_assemblies(folder)
    if not assemblies:
        QMessageBox.information(parent, title, "No game assemblies found to decompile.")
        return
    dlg = DecompileDialog(game_name, kind, assemblies, decompile_game.default_out_dir(game_name), parent)
    if not dlg.exec() or not dlg.chosen():
        return
    names, out_root = dlg.chosen(), dlg.out.text().strip()
    log.info("Decompiling %d assembl%s of '%s' to %s", len(names), "y" if len(names) == 1 else "ies",
             game_name, out_root)
    started = time.monotonic()
    results, error, cancelled = _run_in_thread(
        parent, title, f"Decompiling {game_name} with ILSpy...",
        lambda progress, stop: decompile_game.run(ilspycmd, folder, names, out_root, progress, stop))
    if error is not None:
        QMessageBox.warning(parent, title, str(error))
        return
    decompile_game.write_readme(out_root, game_name, kind, results)
    ok = [n for n, r in results.items() if isinstance(r, int)]
    failed = [n for n, r in results.items() if not isinstance(r, int) and r != "stopped"]
    files = sum(r for r in results.values() if isinstance(r, int))
    log.info("Decompiled %d assemblies (%d files) in %.0fs, %d failed", len(ok), files,
             time.monotonic() - started, len(failed))
    text = (f"{'Stopped' if cancelled else 'Done'}: {len(ok):,} assembl{'y' if len(ok) == 1 else 'ies'}, "
            f"{files:,} C# files."
            + (f"\n{len(failed)} failed: {', '.join(failed[:8])}{'...' if len(failed) > 8 else ''} (see README.txt)"
               if failed else "")
            + f"\n\n{out_root}\n\nOpen the folder now?")
    if QMessageBox.question(parent, title, text) == QMessageBox.Yes:
        open_path(out_root)
