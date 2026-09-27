"""Saved games (projects.json), Steam library discovery and engine detection."""

import os
import re
import time

import engines
from uniview.constants import PROJECTS_FILE, log
from uniview.util import norm_path, read_json, write_json

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"

EXE_SKIP = ("crash", "unins", "setup", "redist", "launcher", "helper", "bootstrap", "report")

def game_exe(game_dir):
    """The game's main .exe (for its icon), or None."""
    from engines.unity import unity_data_dir
    data = unity_data_dir(game_dir)
    if data:
        exe = data[: -len("_Data")] + ".exe"
        if os.path.isfile(exe):
            return exe
    if not os.path.isdir(game_dir):
        return None
    # Top level first (Unity, Unreal, Source), then Source 2's game/bin/win64.
    for folder in (game_dir, os.path.join(game_dir, "game", "bin", "win64")):
        try:
            exes = [f for f in os.listdir(folder) if f.lower().endswith(".exe")]
        except OSError:
            continue
        exes = [f for f in exes if not any(s in f.lower() for s in EXE_SKIP)]
        if exes:
            folder_name = os.path.basename(os.path.normpath(game_dir)).lower()
            exes.sort(key=lambda f: (folder_name.replace(" ", "") not in f.lower().replace(" ", ""), len(f)))
            return os.path.join(folder, exes[0])
    return None

def steam_library_dirs():
    libs = [STEAM_DEFAULT]
    vdf = os.path.join(STEAM_DEFAULT, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as f:
            libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
    except OSError:
        pass
    seen, out = set(), []
    for lib in libs:
        common = os.path.join(lib, "steamapps", "common")
        key = os.path.normcase(os.path.abspath(common))
        if key not in seen and os.path.isdir(common):
            seen.add(key)
            out.append(common)
    return out

def find_steam_games(progress=None, cancelled=None):
    """[(name, path, plugin)] for every game in the Steam libraries that an engine plugin recognizes.

    progress(done, total, folder name) is called before each folder is checked; cancelled() -> True stops early.
    """
    folders = []
    for common in steam_library_dirs():
        try:
            names = sorted(os.listdir(common))
        except OSError:
            continue
        folders += [(name, os.path.join(common, name)) for name in names]
    games = []
    for i, (name, path) in enumerate(folders):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(i, len(folders), name)
        if not os.path.isdir(path):
            continue
        try:
            plugin, score = engines.detect(path)
        except Exception:
            log.exception("Checking %s failed", path)
            continue
        if plugin is not None and score >= 50:
            games.append((name, path, plugin))
    return games

def version_key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))

def project_info(project):
    return {"engine_version": project.get("engine_version", ""), "detail": project.get("engine_detail", "")}

def engine_info_text(project):
    """'Unity 2019.4.40f1 · IL2CPP', 'Source · tf, hl2', ... for a project (or '' if unknown)."""
    plugin = engines.get(project.get("engine") or "")
    info = project_info(project)
    parts = []
    if plugin is not None:
        parts.append(plugin.label(info))
    elif project.get("engine"):
        parts.append(f"{project['engine']} (plugin missing)")
    if info["detail"]:
        parts.append(info["detail"])
    return " \u00b7 ".join(p for p in parts if p)

def engine_group(project):
    """Group title for 'Group by engine version'."""
    plugin = engines.get(project.get("engine") or "")
    if plugin is None:
        return "Unknown engine"
    try:
        return plugin.short_version(project_info(project)) or plugin.name
    except Exception:
        log.debug("%s.short_version() failed", plugin.id, exc_info=True)
        return plugin.name

def detect_project_engine(path, engine_id=None):
    """{'engine', 'engine_version', 'engine_detail'} for a game folder (reads headers only)."""
    plugin = engines.get(engine_id) if engine_id else None
    if plugin is None:
        plugin, _score = engines.detect(path)
    if plugin is None:
        return {"engine": "", "engine_version": "", "engine_detail": ""}
    info = plugin.game_info(path) or {}
    return {"engine": plugin.id, "engine_version": info.get("engine_version", ""),
            "engine_detail": info.get("detail", "")}

class ProjectStore:
    """List of saved games, persisted to projects.json."""

    def __init__(self, path=PROJECTS_FILE):
        self.path = path
        data = read_json(path, "project list")
        self.projects = data if isinstance(data, list) else []
        # Projects saved by UniView 1.x were always Unity games.
        migrated = False
        for project in self.projects:
            if "unity_version" in project and "engine" not in project:
                project["engine"] = "unity"
                project["engine_version"] = project.pop("unity_version")
                project["engine_detail"] = project.pop("backend", "")
                migrated = True
        if migrated:
            self.save()

    def save(self):
        write_json(self.path, self.projects)

    def get(self, path):
        key = norm_path(path)
        return next((p for p in self.projects if norm_path(p["path"]) == key), None)

    def has(self, path):
        return self.get(path) is not None

    def set_counts(self, path, files=None, assets=None):
        project = self.get(path)
        if project is None:
            return
        if files is not None:
            project["file_count"] = files
        if assets is not None:
            project["asset_count"] = assets
        self.save()

    def update(self, path, **fields):
        project = self.get(path)
        if project is not None:
            project.update(fields)
            self.save()

    def all_tags(self):
        return sorted({t for p in self.projects for t in p.get("tags", [])}, key=str.lower)

    def add(self, name, path, engine=None):
        if not self.has(path):
            project = {"name": name, "path": path, "last_opened": 0}
            if engine:
                project["engine"] = engine
            self.projects.append(project)
            self.save()
            log.info("Added game '%s' (%s)", name, path)

    def remove(self, project):
        self.projects.remove(project)
        self.save()

    def touch(self, project):
        project["last_opened"] = time.time()
        self.save()

    SORTS = {
        "recent": lambda p: (-p.get("last_opened", 0), p["name"].lower()),
        "name": lambda p: p["name"].lower(),
        "engine": lambda p: (p.get("engine") or "~", tuple(-n for n in version_key(p.get("engine_version")))),
        "assets": lambda p: (-(p.get("asset_count") or 0), -(p.get("file_count") or 0)),
    }

    def sorted(self, by="recent"):
        """Pinned games first, then by the chosen order (most recently opened by default)."""
        key = self.SORTS.get(by, self.SORTS["recent"])
        return sorted(self.projects, key=lambda p: (not p.get("pinned"), key(p), p["name"].lower()))

def project_options(project, plugin):
    """The settings the user saved for this game's engine plugin (Engine settings...)."""
    return dict((project.get("engine_options") or {}).get(plugin.id, {}))
