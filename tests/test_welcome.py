"""The startup welcome window: 'Don't show this at startup' is saved, Get Engines / Plugins is reported."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from uniview.settings import Settings  # noqa: E402
from uniview.ui.welcome_dialog import WelcomeDialog  # noqa: E402

app = QApplication.instance() or QApplication([])


def test_dont_show_again_is_saved(tmp_path):
    settings = Settings(path=str(tmp_path / "settings.json"))
    assert settings.show_welcome
    dialog = WelcomeDialog(settings)
    dialog.dont_show.setChecked(True)
    dialog.accept()
    assert not settings.show_welcome
    assert not Settings.load(str(tmp_path / "settings.json")).show_welcome
    assert not dialog.open_plugins


def test_get_plugins_button(tmp_path):
    settings = Settings(path=str(tmp_path / "settings.json"))
    dialog = WelcomeDialog(settings)
    dialog._get_plugins()
    assert dialog.open_plugins and settings.show_welcome
