"""UniView - Game Asset Viewer. Browse models, textures and text assets from Unity, Source,
Source 2 and Unreal games - and any other engine someone writes a plugin for.

Starts on a projects page with a box per saved game (add one by folder or let it find games
in your Steam libraries). Each game is read by an engine plugin (engines/ for the built-in
ones, plugins/ for your own - see PLUGINS.md); pick an item in the list and it previews on
the right. Models render in 3D with their texture when the plugin can find it, textures
show as images.
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import engines
from uniview import __version__  # first: sets QT_API before Qt / pyvista load
from uniview.constants import APP_SHORT, APP_TITLE, PLUGINS_DIR
from uniview.logs import install_crash_handlers, setup_logging
from uniview.selftest import self_test
from uniview.ui.main_window import MainWindow, load_app_icon


def main():
    handler = setup_logging()
    install_crash_handlers()
    engines.load_plugins(PLUGINS_DIR)
    if "--self-test" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--self-test"]
        QApplication(sys.argv)
        sys.exit(self_test(args[0] if args else None))
    if sys.platform == "win32":
        # Own taskbar entry/icon instead of grouping under python.exe.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_SHORT)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(load_app_icon())
    win = MainWindow(handler)
    win.show()
    QTimer.singleShot(800, win.check_tools_at_startup)
    if len(sys.argv) > 1:
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
