"""Help -> Check for updates: the latest GitHub release, its notes, and Update now (download, check, install on
restart). The work is in uniview.updater. Also checked quietly at startup (Help -> Check for updates at startup).
"""

import shutil
import threading
import webbrowser

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from uniview import __version__, updater
from uniview.constants import APP_DIR, REPO_URL, log


class UpdateCheck(QObject):
    """Asks GitHub for the latest release in a background thread."""

    finished = Signal(object, str)   # Release or None, error text

    def start(self):
        def work():
            try:
                release, error = updater.latest_release(), ""
            except Exception as e:
                release, error = None, f"{type(e).__name__}: {e}"
            try:
                self.finished.emit(release, error)
            except RuntimeError:  # the window was closed meanwhile
                pass

        threading.Thread(target=work, daemon=True, name="update-check").start()


class UpdateDialog(QDialog):
    """A newer release: notes, Update now / Release page / Skip this version / Later."""

    _progress = Signal(int, int)
    _downloaded = Signal(str, str)   # folder with the new UniView.exe, error

    def __init__(self, release, settings, quit_app, parent=None):
        super().__init__(parent)
        self.release, self.settings, self.quit_app = release, settings, quit_app
        self._cancel = False
        self._busy = False
        self._new_dir = ""  # downloaded and unpacked, waiting for the restart
        self.setWindowTitle("Update available")
        self.resize(640, 480)

        title = QLabel(f"<h3>UniView {release.version} is available</h3>You have {__version__}.")
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "_No release notes._")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.hide()

        self.update_button = QPushButton("Update now")
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(self.start_update)
        page = QPushButton("Release page")
        page.clicked.connect(lambda: webbrowser.open(release.url or f"{REPO_URL}/releases/latest"))
        self.skip_button = QPushButton("Skip this version")
        self.skip_button.clicked.connect(self.skip)
        self.later_button = QPushButton("Later")
        self.later_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.update_button)
        buttons.addWidget(page)
        buttons.addStretch(1)
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.later_button)

        if not updater.can_self_update():
            self.update_button.setEnabled(False)
            self.status.setText("Running from source: update with <code>git pull</code>, or download the new "
                                "release from the release page.")
        elif not release.asset_url:
            self.update_button.setEnabled(False)
            self.status.setText("This release has no Windows download yet. Try again later, or see the release page.")
        else:
            size = f" ({release.asset_size / 1e6:.0f} MB)" if release.asset_size else ""
            self.status.setText(f"Update now downloads {release.asset_name}{size} from GitHub, checks it, and "
                                "restarts UniView with the new version. Your settings, games, plugins and mods "
                                "are kept.")

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(notes, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addLayout(buttons)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)

    def skip(self):
        self.settings.skipped_update = self.release.version
        self.settings.save()
        log.info("Skipping update %s", self.release.version)
        self.reject()

    def start_update(self):
        if self._new_dir:
            self._install()
            return
        if not updater.writable(APP_DIR):
            QMessageBox.warning(self, "Update", f"UniView can't write to its folder:\n{APP_DIR}\n\nMove UniView "
                                "to a folder you can write to (not Program Files), or download the new version "
                                "from the release page.")
            return
        free = shutil.disk_usage(updater.temp_root()).free
        if self.release.asset_size and free < self.release.asset_size * 4:
            QMessageBox.warning(self, "Update", "Not enough free disk space for the update (it needs about "
                                f"{self.release.asset_size * 4 / 1e9:.1f} GB in your Temp folder).")
            return
        self._busy = True
        for b in (self.update_button, self.skip_button):
            b.setEnabled(False)
        self.later_button.setText("Cancel")
        self.bar.show()
        self.bar.setRange(0, 0)
        self.status.setText("Downloading ...")

        def work():
            try:
                zip_path = updater.download(self.release, progress=self._progress.emit,
                                            cancelled=lambda: self._cancel)
                self._progress.emit(-1, 0)
                new_dir = updater.unpack(zip_path)
                updater.remove_quietly(zip_path)
                self._downloaded.emit(new_dir, "")
            except Exception as e:
                self._downloaded.emit("", str(e))

        threading.Thread(target=work, daemon=True, name="update-download").start()

    def _on_progress(self, done, total):
        if done < 0:
            self.bar.setRange(0, 0)
            self.status.setText("Checked. Unpacking ...")
            return
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            self.status.setText(f"Downloading ... {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _on_downloaded(self, new_dir, error):
        self._busy = False
        self.bar.hide()
        self.later_button.setText("Later")
        if error:
            log.warning("Update failed: %s", error)
            self.status.setText(f"The update failed: {error}")
            self.update_button.setEnabled(not self._cancel)
            self.skip_button.setEnabled(True)
            if self._cancel:
                self.reject()
            return
        self._new_dir = new_dir
        self.update_button.setText("Restart and install")
        self.update_button.setEnabled(True)
        self.status.setText(f"UniView {self.release.version} is downloaded and checked.")
        answer = QMessageBox.question(self, "Update ready",
                                      f"UniView {self.release.version} is downloaded and checked. Restart now to "
                                      "install it? (UniView closes, updates itself and opens again.)")
        if answer == QMessageBox.Yes:
            self._install()

    def _install(self):
        new_dir = self._new_dir
        try:
            updater.apply(new_dir)
        except Exception as e:
            QMessageBox.warning(self, "Update", f"Couldn't start the update:\n\n{e}")
            return
        self.accept()
        self.quit_app()

    def reject(self):
        if self._busy:
            self._cancel = True
            self.status.setText("Cancelling ...")
            return
        super().reject()


def check_for_updates(parent, settings, quit_app, quiet):
    """Look for a newer release; show the update window if there is one. quiet = at startup: say nothing when
    there's no update or GitHub can't be reached, and don't remind about a skipped version."""
    check = UpdateCheck(parent)

    def done(release, error):
        check.deleteLater()
        if release is None:
            log.info("Update check failed: %s", error)
            if not quiet:
                QMessageBox.information(parent, "Check for updates", f"Couldn't reach GitHub:\n\n{error}")
            return
        if not updater.is_newer(release.version):
            log.info("Up to date (latest release %s)", release.version)
            if not quiet:
                QMessageBox.information(parent, "Check for updates", f"UniView {__version__} is the latest version.")
            return
        if quiet and settings.skipped_update == release.version:
            log.info("Update %s available (skipped by the user)", release.version)
            return
        log.info("Update available: %s", release.version)
        UpdateDialog(release, settings, quit_app, parent).exec()

    check.finished.connect(done, Qt.QueuedConnection)
    check.start()
    return check
