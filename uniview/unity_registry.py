"""Unity packages from Unity's package registry, shipped with an exported project as .tgz files in
LocalPackages/ (manifest: "file:../LocalPackages/..."), so the project carries the game's dependencies
instead of downloading them on first open. No Qt here.

Tarballs come from Unity's own download cache when the Hub/editor already fetched them
(%LOCALAPPDATA%/Unity/cache/npm), else from packages.unity.com, kept in cache/unity-packages."""

import contextlib
import json
import os
import re
import shutil
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from engines.sdk import cache_dir
from uniview.constants import log

REGISTRY = "https://packages.unity.com"
UNITY_NPM_CACHE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Unity", "cache", "npm", "packages.unity.com")
BUNDLE_DIR = "LocalPackages"  # next to Assets/ and Packages/


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "UniView"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


# --------------------------------------------------------------------------- versions

def semver_key(version):
    """Sort key for package versions: '1.2.3' > '1.2.3-preview.4' > '1.2.2'."""
    core, _, pre = (version or "").partition("-")
    nums = tuple(int(n) for n in re.findall(r"\d+", core)[:3])
    pre_parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.]", pre)) if pre else ()
    return nums, (1,) if not pre else (0,) + pre_parts


def editor_key(version):
    """'2021.3.5f1' -> (2021, 3, 5)."""
    return tuple(int(n) for n in re.findall(r"\d+", version or "")[:3])


def supports(info, editor_version):
    """Does a registry version's `unity`/`unityRelease` allow this editor?"""
    need = info.get("unity")
    if not need or not editor_version:
        return True
    return editor_key(f"{need}.{info.get('unityRelease', '0')}") <= editor_key(editor_version)


def pick_version(meta, editor_version):
    """Newest version that supports the editor: a release if there is one, else a preview."""
    ok = [v for v, info in (meta.get("versions") or {}).items() if supports(info, editor_version)]
    releases = [v for v in ok if "-" not in v]
    return max(releases or ok, key=semver_key) if ok else None


# --------------------------------------------------------------------------- resolving

class Registry:
    """Package metadata from packages.unity.com, fetched once per package."""

    def __init__(self, fetch=None):
        self._fetch = fetch or (lambda name: json.loads(_get(f"{REGISTRY}/{name}")))
        self._meta = {}

    def meta(self, name):
        if name not in self._meta:
            try:
                self._meta[name] = self._fetch(name)
            except Exception as e:  # offline, unknown package...
                log.info("Unity package registry: no data for %s (%s)", name, e)
                self._meta[name] = None
        return self._meta[name]


def resolve(wanted, editor_version, builtin=(), registry=None):
    """{package: version} for the packages `wanted` ({name: version or None = newest that fits the editor})
    and everything they depend on. Like Unity's Package Manager, the highest version anything asks for
    wins. Built-in modules and the editor's own built-in packages are left out (the editor has them)."""
    registry = registry or Registry()
    builtin = set(builtin)
    chosen, queue = {}, []

    def want(name, version):
        if name.startswith("com.unity.modules.") or name in builtin:
            return
        if name in chosen and (version is None or semver_key(version) <= semver_key(chosen[name])):
            return
        if version is None:
            meta = registry.meta(name)
            version = pick_version(meta, editor_version) if meta else None
            if version is None:
                return
        chosen[name] = version
        queue.append(name)

    for name, version in wanted.items():
        want(name, version)
    while queue:
        name = queue.pop()
        meta = registry.meta(name)
        info = ((meta or {}).get("versions") or {}).get(chosen[name]) or {}
        for dep, version in (info.get("dependencies") or {}).items():
            want(dep, version)
    return chosen


# --------------------------------------------------------------------------- bundling

def tarball_path(name, version, registry=None):
    """A local package.tgz: Unity's download cache, else ours (downloaded from the registry once)."""
    ours = os.path.join(cache_dir(), "unity-packages", f"{name}-{version}.tgz")
    for path in (os.path.join(UNITY_NPM_CACHE, name, version, "package.tgz"), ours):
        if os.path.isfile(path):
            return path
    meta = (registry or Registry()).meta(name) or {}
    url = (((meta.get("versions") or {}).get(version) or {}).get("dist") or {}).get("tarball")         or f"https://download.packages.unity.com/{name}/-/{name}-{version}.tgz"
    data = _get(url, timeout=300)
    os.makedirs(os.path.dirname(ours), exist_ok=True)
    with open(ours + ".part", "wb") as f:
        f.write(data)
    os.replace(ours + ".part", ours)
    return ours


def bundle(root, packages, registry=None, cancelled=None, progress=None):
    """Copy {name: version} as .tgz files into <root>/LocalPackages. Returns ({name: manifest reference
    'file:../LocalPackages/<name>-<version>.tgz'}, failed names).

    Tarballs, not folders in Packages/: Unity installs a tarball read-only like a registry package. An
    unpacked (embedded) package counts as the project's own code, and old ones then fail to compile
    (Entities 0.51's com.unity.serialization uses APIs Unity would have to rewrite first)."""
    folder = os.path.join(root, BUNDLE_DIR)
    os.makedirs(folder, exist_ok=True)
    refs, failed, finished = {}, [], []
    lock = threading.Lock()

    def one(name, version):
        if cancelled is not None and cancelled():
            return
        file_name = f"{name}-{version}.tgz"
        target = os.path.join(folder, file_name)
        try:
            if not os.path.isfile(target):
                shutil.copyfile(tarball_path(name, version, registry), target + ".part")
                os.replace(target + ".part", target)
            ok = True
        except Exception as e:
            ok = False
            log.warning("Could not get Unity package %s %s: %s", name, version, e)
        with lock:
            if ok:
                refs[name] = f"file:../{BUNDLE_DIR}/{file_name}"
            else:
                failed.append(name)
            finished.append(name)
            if progress is not None:
                progress(len(finished), len(packages), f"package {name} {version}")

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="unity-package") as pool:  # network-bound
        list(pool.map(lambda item: one(*item), sorted(packages.items())))
    keep = {os.path.basename(r) for r in refs.values()}
    for old in os.listdir(folder):  # other versions from an earlier export
        if old.endswith(".tgz") and old not in keep and any(old.startswith(n + "-") for n in packages):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, old))
    return refs, sorted(failed)
