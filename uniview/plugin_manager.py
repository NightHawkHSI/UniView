"""Options -> Plugin manager: install, switch off and remove engine plugins. No Qt here.

Changes take effect on the next start (a loaded plugin module can't be unloaded cleanly), so
everything here only touches files and the disabled list; the dialog offers a restart.
"""

import ast
import os
import posixpath
import shutil
import time
import zipfile
from dataclasses import dataclass

from uniview.constants import log

INSTALLABLE = (".py", ".zip")
REMOVED_DIR = "_removed"  # plugins/_removed/: removed plugins are moved here, not deleted


class PluginInstallError(Exception):
    pass


@dataclass(slots=True)
class Row:
    """One line in the plugin manager."""
    name: str
    version: str
    origin: str      # "built-in" or the file name in plugins/
    about: str
    key: str         # what goes in Settings.disabled_plugins
    enabled: bool
    removable: bool  # user plugins only; built-in engines can only be switched off
    failed: bool = False
    pending: str = ""  # "installed" / "removed" this session: applies after a restart


def rows(report, disabled, plugins_dir, installed=(), removed=()):
    """Manager rows from engines.load_report(), plus plugins installed or removed since the start.

    A file with several plugins in it gets one row per plugin, but they share the file's switch.
    """
    disabled = set(disabled)
    out, seen = [], set()
    for e in report:
        user = e.origin != "built-in"
        seen.add(e.origin)
        if e.disabled:
            about = "Switched off"
        elif not e.ok:
            about = f"Failed to load: {e.message}"
        else:
            about = e.plugin.description if e.plugin else ""
        version = e.plugin.version if e.plugin else "-"
        pending = "removed" if user and e.origin in removed else ""
        out.append(Row(e.name, version, e.origin, about, e.key, e.key not in disabled, user,
                       failed=not e.ok, pending=pending))
    for fname in installed:
        if fname in seen or not os.path.isfile(os.path.join(plugins_dir, fname)):
            continue
        out.append(Row(fname, "-", fname, "Installed - restart UniView to load it", fname,
                       fname not in disabled, True, pending="installed"))
    return out


def _check_python(name, data):
    try:
        return ast.parse(data, name)
    except SyntaxError as e:
        raise PluginInstallError(f"{name} has a Python error on line {e.lineno}: {e.msg}") from None


def _defines_plugin(tree):
    """True if a module sets PLUGIN = ... or PLUGINS = ... at the top level (what the loader looks for)."""
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id in ("PLUGIN", "PLUGINS") for t in targets):
            return True
    return False


def _zip_members(path):
    """[(relative path, bytes)] of a plugin zip, with one wrapping folder (GitHub downloads) removed."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise PluginInstallError(f"{os.path.basename(path)} is not a valid zip file") from None
    with zf:
        files = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = posixpath.normpath(info.filename.replace("\\", "/"))
            if name.startswith(("/", "../")) or name == ".." or ":" in name:
                raise PluginInstallError(f"The zip has an unsafe path in it: {info.filename}")
            if "__pycache__" in name.split("/"):
                continue
            files.append((name, zf.read(info)))
    tops = {name.split("/")[0] for name, _ in files}
    if len(tops) == 1 and all("/" in name for name, _ in files):
        top = tops.pop() + "/"
        files = [(name[len(top):], data) for name, data in files]
    return files


def plan(path):
    """[(relative path, bytes)] that installing this .py / .zip writes into plugins/. Checks it first."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in INSTALLABLE:
        raise PluginInstallError("A plugin is a .py file, or a .zip with the .py file(s) in it")
    if ext == ".py":
        with open(path, "rb") as f:
            files = [(os.path.basename(path), f.read())]
    else:
        files = _zip_members(path)
    entry = [name for name, _ in files if "/" not in name and name.endswith(".py") and not name.startswith("_")]
    if not entry:
        raise PluginInstallError("No plugin file found: it needs a .py file (not starting with '_') at the top level")
    for name, data in files:
        if name.endswith(".py"):
            tree = _check_python(name, data)
            if name in entry and not _defines_plugin(tree):
                raise PluginInstallError(
                    f"{name} isn't an engine plugin: it has no PLUGIN = ... line. Every .py file at the top of "
                    "the plugins folder is loaded as a plugin (helper modules go in a subfolder or start with '_').")
    return files


def top_level(files):
    """The names a plan puts directly in plugins/ (files and folders)."""
    return sorted({name.split("/")[0] for name, _ in files})


def conflicts(files, plugins_dir):
    """Names of the plan that already exist in plugins/ (installing replaces them)."""
    return [n for n in top_level(files) if os.path.exists(os.path.join(plugins_dir, n))]


def install(files, plugins_dir):
    """Write a plan into plugins/; returns the new plugin files (top-level .py not starting with '_')."""
    os.makedirs(plugins_dir, exist_ok=True)
    for name, data in files:
        dest = os.path.join(plugins_dir, *name.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
    added = [n for n in top_level(files) if n.endswith(".py") and not n.startswith("_")]
    log.info("Installed plugin file(s) %s into %s", ", ".join(added), plugins_dir)
    return added


def remove(fname, plugins_dir):
    """Move a user plugin file to plugins/_removed/ (so it can be put back). Returns where it went."""
    src = os.path.join(plugins_dir, fname)
    if os.path.basename(fname) != fname or not os.path.isfile(src):
        raise FileNotFoundError(f"No plugin file {fname} in {plugins_dir}")
    backup_dir = os.path.join(plugins_dir, REMOVED_DIR)
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, fname)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(fname)
        dest = os.path.join(backup_dir, f"{stem}.{time.strftime('%Y%m%d-%H%M%S')}{ext}")
    shutil.move(src, dest)
    log.info("Removed plugin %s (moved to %s)", fname, dest)
    return dest

