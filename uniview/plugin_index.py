"""The Plugins tab's logic: the online plugin list (plugins.json in the UniView-Plugins repo), what's installed,
and downloading a plugin into plugins/. No Qt here.

A download is checked before anything is written: every file must be listed in the index with its size and
SHA-256, sit inside plugins/ (no absolute paths or '..'), be a .py file, and pass the same checks as a plugin
installed from a file (uniview.plugin_manager.plan: valid Python, the entry file sets PLUGIN = ...).
"""

import hashlib
import json
import os
import posixpath
import re
import urllib.request
from dataclasses import dataclass, field

from uniview import plugin_manager
from uniview.constants import PLUGIN_INDEX_CACHE, PLUGINS_INDEX_URL, log
from uniview.settings import write_json

INDEX_FORMAT = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
USER_AGENT = "UniView plugin manager"
_SAFE_PART = re.compile(r"^[A-Za-z0-9_.\- ]+$")


class PluginIndexError(ValueError):
    """plugins.json is missing fields or lists a file UniView won't install."""


@dataclass(slots=True)
class Available:
    """One plugin of the online list."""
    file: str
    id: str
    name: str
    version: str
    api_version: int
    description: str
    games: list[str]
    files: list[dict]   # {"path", "size", "sha256"}
    author: str = ""


@dataclass(slots=True)
class PluginRow:
    """A line of the Plugins tab: a plugin from the online list, one installed by hand, or both."""
    file: str
    name: str
    installed: str          # version loaded now ("" = not installed)
    available: str          # version in the online list ("" = not listed)
    description: str = ""
    games: list[str] = field(default_factory=list)
    state: str = ""         # "not installed" / "installed" / "update" / "local" / "failed" / "needs newer UniView"
    entry: Available | None = None
    pending: str = ""       # "installed" this session: loads after a restart


def version_key(text):
    """'1.10' > '1.9' > '1.2b': numbers compared as numbers, anything after them as text."""
    parts = []
    for piece in re.split(r"[.\-+ ]", str(text).strip().lstrip("vV")):
        m = re.match(r"(\d*)(.*)", piece)
        parts.append((int(m.group(1)) if m.group(1) else -1, m.group(2)))
    return tuple(parts)


def _safe_path(rel):
    """A file path from the index, checked to stay inside plugins/ and to be a .py file."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\\" in rel or ":" in rel:
        raise PluginIndexError(f"bad file path in the plugin list: {rel!r}")
    norm = posixpath.normpath(rel)
    parts = norm.split("/")
    if norm != rel or any(p in ("", ".", "..") or not _SAFE_PART.match(p) for p in parts):
        raise PluginIndexError(f"bad file path in the plugin list: {rel!r}")
    if not rel.endswith(".py") or parts[0] == plugin_manager.REMOVED_DIR:
        raise PluginIndexError(f"the plugin list may only install .py files: {rel!r}")
    return rel


def parse_index(data):
    """[Available] from a plugins.json dict; raises PluginIndexError if it isn't one UniView understands."""
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise PluginIndexError("not a plugin list")
    if data.get("format", 0) > INDEX_FORMAT:
        raise PluginIndexError("the plugin list needs a newer UniView")
    out = []
    for p in data["plugins"]:
        try:
            files = [{"path": _safe_path(f["path"]), "size": int(f["size"]), "sha256": str(f["sha256"]).lower()}
                     for f in p["files"]]
            entry = Available(file=_safe_path(p["file"]), id=str(p["id"]), name=str(p["name"]),
                              version=str(p["version"]), api_version=int(p.get("api_version", 1)),
                              description=str(p.get("description", "")), games=[str(g) for g in p.get("games", [])],
                              files=files, author=str(p.get("author", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise PluginIndexError(f"bad plugin entry {p.get('file') if isinstance(p, dict) else p!r}: {e}") from e
        if "/" in entry.file or entry.file.startswith("_") or entry.file not in [f["path"] for f in files]:
            raise PluginIndexError(f"{entry.file}: the plugin file must be a top-level .py listed in its files")
        out.append(entry)
    return out


def base_url(data, index_url=PLUGINS_INDEX_URL):
    """Where the index's files are downloaded from: its base_url, else the folder the index came from."""
    url = data.get("base_url") if isinstance(data, dict) else None
    if isinstance(url, str) and url.startswith("https://"):
        return url if url.endswith("/") else url + "/"
    return index_url.rsplit("/", 1)[0] + "/"


def _get(url, timeout=15, limit=MAX_FILE_BYTES):
    if not url.startswith("https://"):
        raise ValueError(f"refusing to download over plain HTTP: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is bigger than {limit // (1024 * 1024)} MB")
    return data


def fetch_index(url=PLUGINS_INDEX_URL, cache=PLUGIN_INDEX_CACHE):
    """Download plugins.json (and keep a copy for offline use). Returns the parsed dict."""
    data = json.loads(_get(url).decode("utf-8"))
    parse_index(data)  # check it before caching
    try:
        write_json(cache, data)
    except OSError as e:
        log.debug("Couldn't save the plugin list: %s", e)
    return data


def cached_index(cache=PLUGIN_INDEX_CACHE):
    """The last plugins.json downloaded, or None."""
    try:
        with open(cache, encoding="utf-8") as f:
            data = json.load(f)
        parse_index(data)
        return data
    except (OSError, ValueError):
        return None


def download(entry, url_base, fetch=_get):
    """[(relative path, bytes)] of a plugin's files, downloaded and checked against the index. Writes nothing."""
    files = []
    for f in entry.files:
        data = fetch(url_base + f["path"])
        if len(data) != f["size"] or hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise plugin_manager.PluginInstallError(
                f"{f['path']} doesn't match the plugin list (size or SHA-256) - not installed. The download may "
                "be incomplete, or the list is being updated: try again in a minute.")
        files.append((f["path"], data))
    # The same checks as installing from a file: valid Python, the entry file defines a plugin.
    for name, data in files:
        tree = plugin_manager._check_python(name, data)
        if name == entry.file and not plugin_manager._defines_plugin(tree):
            raise plugin_manager.PluginInstallError(f"{name} has no PLUGIN = ... line")
    return files


def rows(available, report, installed_now=(), api_version=1):
    """Plugins tab rows: every listed plugin (with its installed version, if any), then plugins installed by hand
    that aren't in the list. `report` is engines.load_report(); installed_now = {file: version} installed this
    session (loaded after a restart)."""
    installed_now = dict(installed_now)
    loaded = {}   # file -> (version, ok)
    for e in report:
        if e.origin == "built-in":
            continue
        version = e.plugin.version if e.plugin else ""
        loaded.setdefault(e.origin, (str(version), e.ok, e.disabled))
    out, listed = [], set()
    for a in available:
        listed.add(a.file)
        version, ok, disabled = loaded.get(a.file, ("", True, False))
        row = PluginRow(a.file, a.name, version, a.version, a.description, list(a.games), entry=a)
        if a.file in installed_now:
            row.installed, row.pending, row.state = installed_now[a.file], "installed", "installed"
        elif a.file in loaded and not ok:
            row.state = "failed"
        elif disabled:
            row.state = "switched off"
        elif not version:
            row.state = "not installed"
        elif version_key(a.version) > version_key(version):
            row.state = "update"
        else:
            row.state = "installed"
        if a.api_version > api_version and row.state in ("not installed", "update"):
            row.state = "needs newer UniView"
        out.append(row)
    for fname, (version, ok, _disabled) in loaded.items():
        if fname not in listed:
            out.append(PluginRow(fname, fname, version, "", state="local" if ok else "failed"))
    return out


def missing_plugin(compat_entry, loaded_ids):
    """The plugin id a game needs (from the compatibility list's "plugin" field) if that plugin isn't loaded."""
    plugin_id = (compat_entry or {}).get("plugin")
    if isinstance(plugin_id, str) and plugin_id and plugin_id not in set(loaded_ids):
        return plugin_id
    return None


def entry_for(available, plugin_id):
    return next((a for a in available if a.id == plugin_id), None)


def files_in_use(plugins_dir, files):
    """Which of a download's files already exist in plugins/ (they get replaced)."""
    return [name for name, _ in files if os.path.exists(os.path.join(plugins_dir, *name.split("/")))]
