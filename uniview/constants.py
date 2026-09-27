"""App-wide constants: names, file locations, list/thumbnail sizes, the shared logger."""

import logging
import logging.handlers
import os
import sys

from PySide6.QtCore import Qt

APP_SHORT = "UniView"

APP_TITLE = "UniView - Game Asset Viewer"

MAX_TEXT_CHARS = 200_000

THUMB_SIZE = 40      # icon size in the list

GRID_THUMB = 96      # thumbnail size generated (grid view uses it 1:1)

THUMB_CACHE_MAX = 4000

GRID_LIMIT = 20000

REPO_URL = "https://github.com/NightHawkHSI/UniView"

COMPAT_URL = "https://raw.githubusercontent.com/NightHawkHSI/UniView/main/compat.json"

SORT_ROLE = Qt.UserRole + 10   # value used when sorting a tree column

TYPE_ROLE = Qt.UserRole + 11   # asset kind stored on group rows

if getattr(sys, "frozen", False):
    # Packaged .exe: user files live next to the exe, bundled files in the unpack dir.
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the folder above uniview/
    RESOURCE_DIR = APP_DIR

ICON_FILE = os.path.join(RESOURCE_DIR, "Icon.png")

LOG_FILE = os.path.join(APP_DIR, "viewer.log")

SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

COMPAT_CACHE = os.path.join(APP_DIR, "compat_cache.json")

COMPAT_BUNDLED = os.path.join(RESOURCE_DIR, "compat.json")

CRASH_FILE = os.path.join(APP_DIR, "crash.log")

PLUGINS_DIR = os.path.join(APP_DIR, "plugins")

log = logging.getLogger("viewer")

VIEW_OPTION_ITEMS = (
    ("hide_unreadable", "Hide assets that can't be shown",
     "Hide list entries that failed to decode or are empty placeholders (e.g. 0x0 textures made while the game runs)"),
    ("hide_skybox", "Hide 3D skybox in maps",
     "Source maps: leave out the small copy of the scenery around the sky_camera"),
    ("hide_tool_surfaces", "Hide tool brushes in maps",
     "Source maps: leave out nodraw, trigger, clip, hint and sky brushes"),
    ("hide_lods", "Hide lower-detail LODs in scenes",
     "Scenes and prefabs: show only the most detailed copy of objects that have LODs"),
    ("hide_inactive", "Hide switched-off objects in scenes",
     "Scenes and prefabs: leave out objects and renderers the game has disabled"),
)

PROJECTS_FILE = os.path.join(APP_DIR, "projects.json")
