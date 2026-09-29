"""Cpp2IL (https://github.com/SamboyCoding/Cpp2IL, MIT license): rebuilds an IL2CPP Unity game's .NET
assemblies from GameAssembly.dll + global-metadata.dat.

The rebuilt assemblies are stubs - every class, field and custom attribute ([SerializeField],
[FormerlySerializedAs]...), with method bodies that just return defaults. That is all script field
layouts need (they're read with the Mono type tree generator, which is far more reliable than the
native IL2CPP one) and all an exported Unity project needs to attach the game's script components.

UniView doesn't ship Cpp2IL; Help -> Optional tools downloads its Windows build into tools/cpp2il.
Results are cached per game build in cache/cpp2il (a few MB each).
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from .extdecode import tools_dir
from .sdk import cache_dir

log = logging.getLogger("viewer.cpp2il")

VERSION = "2022.1.0-pre-release.21"  # IL2CPP metadata up to v31 (Unity 6)
URL = f"https://github.com/SamboyCoding/Cpp2IL/releases/download/{VERSION}/Cpp2IL-{VERSION}-Windows.exe"
DONE_MARKER = "uniview-done.txt"

_lock = threading.Lock()  # one Cpp2IL run at a time (the viewer and an export can both ask)
_failed = {}  # output folder -> error, so a build Cpp2IL can't handle isn't retried every time it's asked


def cpp2il_path():
    """UniView's copy of Cpp2IL.exe (a self-contained program, no .NET needed), or None."""
    exe = os.path.join(tools_dir(), "cpp2il", "Cpp2IL.exe")
    return exe if os.path.isfile(exe) else None


def install():
    """Download Cpp2IL's Windows build into tools/cpp2il (about 15 MB). Returns the exe path."""
    target = os.path.join(tools_dir(), "cpp2il")
    os.makedirs(target, exist_ok=True)
    exe = os.path.join(target, "Cpp2IL.exe")
    req = urllib.request.Request(URL, headers={"User-Agent": "UniView"})
    with urllib.request.urlopen(req, timeout=300) as response, open(exe + ".part", "wb") as f:
        shutil.copyfileobj(response, f)
    os.replace(exe + ".part", exe)
    return exe


def il2cpp_files(game_dir):
    """(GameAssembly.dll, global-metadata.dat) of an IL2CPP build, or None."""
    binary = os.path.join(game_dir, "GameAssembly.dll")
    if not os.path.isfile(binary):
        return None
    try:
        names = os.listdir(game_dir)
    except OSError:
        return None
    for name in names:
        metadata = os.path.join(game_dir, name, "il2cpp_data", "Metadata", "global-metadata.dat")
        if name.endswith("_Data") and os.path.isfile(metadata):
            return binary, metadata
    return None


def is_il2cpp(game_dir):
    return il2cpp_files(game_dir) is not None


def _cache_folder(game_dir, files):
    """cache/cpp2il/<game>-<hash>: the hash changes when the game updates or Cpp2IL does."""
    key = [VERSION]
    for path in files:
        st = os.stat(path)
        key += [os.path.abspath(path).lower(), str(st.st_size), str(int(st.st_mtime))]
    digest = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.basename(os.path.normpath(game_dir)))[:40] or "game"
    return os.path.join(cache_dir(), "cpp2il", f"{name}-{digest}")


def stub_assemblies(game_dir, timeout=900, cancelled=None):
    """Folder of stub assemblies for an IL2CPP game (like a Mono build's Managed folder), running Cpp2IL
    the first time. None if the game isn't IL2CPP or Cpp2IL isn't installed; RuntimeError if it fails."""
    files = il2cpp_files(game_dir)
    exe = cpp2il_path()
    if files is None or exe is None:
        return None
    out_dir = _cache_folder(game_dir, files)
    with _lock:
        if os.path.isfile(os.path.join(out_dir, DONE_MARKER)):
            return out_dir
        if out_dir in _failed:
            raise RuntimeError(_failed[out_dir])
        # Older results for the same game (before an update) aren't needed any more.
        prefix = os.path.basename(out_dir).rsplit("-", 1)[0] + "-"
        parent = os.path.dirname(out_dir)
        if os.path.isdir(parent):
            for old in os.listdir(parent):
                if old.startswith(prefix):
                    shutil.rmtree(os.path.join(parent, old), ignore_errors=True)
        try:
            _run(exe, game_dir, out_dir, timeout, cancelled)
        except RuntimeError as e:
            if str(e) != "stopped":
                _failed[out_dir] = str(e)
            raise
        with open(os.path.join(out_dir, DONE_MARKER), "w", encoding="utf-8") as f:
            f.write(f"Cpp2IL {VERSION}\n{game_dir}\n")
        return out_dir


def _run(exe, game_dir, out_dir, timeout, cancelled):
    """attributeanalyzer restores the custom attributes Unity serialization depends on; dll_default
    gives methods bodies that return defaults, so the assemblies load (and verify) cleanly."""
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()
    log.info("Rebuilding the game's IL2CPP code with Cpp2IL: %s", game_dir)
    # cwd: Cpp2IL loads plugins from ./Plugins
    proc = subprocess.Popen([exe, "--game-path", game_dir, "--use-processor", "attributeanalyzer",
                             "--output-as", "dll_default", "--output-to", out_dir],
                            cwd=os.path.dirname(exe), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = started + timeout
    while True:
        try:
            out, _ = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if (cancelled is not None and cancelled()) or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                shutil.rmtree(out_dir, ignore_errors=True)
                raise RuntimeError("stopped" if cancelled is not None and cancelled() else "took too long")
    if proc.returncode != 0 or not os.path.isfile(os.path.join(out_dir, "Assembly-CSharp.dll")):
        shutil.rmtree(out_dir, ignore_errors=True)
        text = re.sub(r"\x1b\[[0-9;]*m", "", out or "")  # drop colour codes
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        errors = [ln for ln in lines if "error" in ln.lower() or "exception" in ln.lower()]
        raise RuntimeError((errors or lines or ["Cpp2IL failed"])[-1])
    log.info("Cpp2IL rebuilt %d assemblies in %.1fs", len([n for n in os.listdir(out_dir) if n.endswith(".dll")]),
             time.monotonic() - started)
