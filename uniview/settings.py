"""User settings (settings.json), atomic JSON files, Blender lookup and the compatibility list."""

import glob
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from uniview import __version__
from uniview.constants import COMPAT_BUNDLED, COMPAT_CACHE, COMPAT_URL, REPO_URL, SETTINGS_FILE, log
from uniview.projects import engine_info_text, steam_library_dirs
from uniview.util import read_json, write_json

SETTING_CHOICES = {"model_format": ("obj", "glb"), "view": ("list", "grid")}

_SETTINGS_INTERNAL = ("options", "path", "extra")

@dataclass(slots=True)
class Settings:
    """Per-user settings (settings.json next to the app).

    Every value is checked when it's assigned, so a typo'd name or a wrong type fails right there
    instead of silently breaking the save. Load with Settings.load().
    """

    last_dir: str = field(default_factory=lambda: os.path.expanduser("~"))
    export_dir: str = ""
    model_format: str = "obj"
    keep_structure: bool = True
    volume: float = 0.7  # 0..1
    autoplay: bool = False
    view: str = "list"
    home_engine: str = ""
    home_group: str = "engine"
    home_sort: str = "recent"
    online_compat: bool = True
    blender_path: str = ""
    check_tools: bool = True  # startup check for missing optional tools
    options: dict = field(default_factory=dict)  # Options menu (VIEW_OPTION_ITEMS): {key: bool}, missing = on
    path: str = SETTINGS_FILE
    extra: dict = field(default_factory=dict)  # keys this version doesn't know, written back unchanged

    def __setattr__(self, name, value):
        kind = self.__dataclass_fields__[name].type if name in self.__dataclass_fields__ else None
        if kind is None:
            raise AttributeError(f"Unknown setting '{name}'")
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if type(value) is not kind:
            raise TypeError(f"Setting '{name}' must be {kind.__name__}, not {type(value).__name__}")
        if name in SETTING_CHOICES and value not in SETTING_CHOICES[name]:
            raise ValueError(f"Setting '{name}' must be one of {SETTING_CHOICES[name]}, not {value!r}")
        if name == "volume" and not 0.0 <= value <= 1.0:
            raise ValueError(f"Setting 'volume' must be 0..1, not {value}")
        object.__setattr__(self, name, value)

    @classmethod
    def load(cls, path=SETTINGS_FILE):
        settings = cls(path=path)
        data = read_json(path, "settings")
        if data is None:
            return settings
        if not isinstance(data, dict):
            log.warning("Ignoring the settings file %s: not a JSON object", path)
            return settings
        for key, value in data.items():
            if key.startswith("opt_"):
                if isinstance(value, bool):
                    settings.options[key[4:]] = value
                else:
                    log.warning("Ignoring the setting %s: %r isn't true/false", key, value)
            elif key in cls.__dataclass_fields__ and key not in _SETTINGS_INTERNAL:
                try:
                    setattr(settings, key, value)
                except (TypeError, ValueError) as e:
                    log.warning("Ignoring a saved setting: %s", e)
            else:
                settings.extra[key] = value
        return settings

    def option(self, key):
        return self.options.get(key, True)

    def set_option(self, key, on):
        if not isinstance(on, bool):
            raise TypeError(f"Option '{key}' must be bool, not {type(on).__name__}")
        self.options[key] = on

    def to_json(self):
        data = dict(self.extra)
        data.update((name, getattr(self, name)) for name in self.__dataclass_fields__ if name not in _SETTINGS_INTERNAL)
        data.update(("opt_" + key, on) for key, on in self.options.items())
        return data

    def save(self):
        try:
            write_json(self.path, self.to_json())
        except (OSError, TypeError, ValueError) as e:
            log.warning("Could not save settings: %s", e)

def _version_key(path):
    return [int(n) for n in re.findall(r"\d+", os.path.basename(os.path.dirname(path)))]

def find_blender(saved=None):
    """Path of blender.exe: saved choice, PATH, registry, Program Files, then Steam."""
    candidates = [saved, shutil.which("blender")]
    if sys.platform == "win32":
        try:
            import winreg
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe") as key:
                        candidates.append(winreg.QueryValue(key, None).strip('"'))
                except OSError:
                    pass
        except ImportError:
            pass
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                found = glob.glob(os.path.join(base, "Blender Foundation", "*", "blender.exe"))
                candidates += sorted(found, key=_version_key, reverse=True)
        try:
            candidates += [os.path.join(common, "Blender", "blender.exe") for common in steam_library_dirs()]
        except Exception:
            log.debug("Looking for Blender in the Steam libraries failed", exc_info=True)
    return next((c for c in candidates if c and os.path.isfile(c)), None)

BLENDER_IMPORT = (
    "import bpy\n"
    "for o in list(bpy.data.objects):\n"
    "    bpy.data.objects.remove(o, do_unlink=True)\n"
    "bpy.ops.import_scene.gltf(filepath={path!r})\n"
)

COMPAT_STATUS = {"works": "\u2714 Works well", "partial": "\u25d0 Partly works", "broken": "\u2716 Doesn't work"}

def load_compat():
    """Community compatibility list: {install folder name (lowercase): entry}."""
    for path in (COMPAT_CACHE, COMPAT_BUNDLED):
        try:
            with open(path, encoding="utf-8") as f:
                games = json.load(f).get("games")
            if isinstance(games, dict):
                return {name.lower(): entry for name, entry in games.items() if isinstance(entry, dict)}
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError) as e:
            log.warning("Ignoring the compatibility list %s: %s", path, e)
            continue
    return {}

def fetch_compat():
    """Download the latest list from the repo (called from a background thread)."""
    with urllib.request.urlopen(COMPAT_URL, timeout=8) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data.get("games"), dict):
        raise ValueError("unexpected compat.json format")
    write_json(COMPAT_CACHE, data)
    return len(data["games"])

def compat_report_url(project):
    name = project["name"]
    body = (f"**Game:** {name}\n"
            f"**Install folder name:** {os.path.basename(os.path.normpath(project['path']))}\n"
            f"**Status:** works / partial / broken  (keep one)\n"
            f"**UniView version:** {__version__}\n"
            f"**Engine:** {engine_info_text(project) or '?'}\n"
            f"**Game files / assets:** {project.get('file_count', '?')} / {project.get('asset_count', '?')}\n\n"
            f"**What works / what doesn't:**\n\n")
    query = urllib.parse.urlencode({"title": f"Compatibility: {name}", "body": body, "labels": "compatibility"})
    return f"{REPO_URL}/issues/new?{query}"
