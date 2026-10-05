"""UniView - Game Asset Viewer, the application package (unity_viewer.py starts it)."""

import os

__version__ = "2.7.1"

# pyvista/pyvistaqt pick their Qt binding from this; it has to be set before they're imported.
os.environ.setdefault("QT_API", "pyside6")
