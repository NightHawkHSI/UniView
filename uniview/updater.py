"""Updates from GitHub releases: find the latest release, download its Windows zip, and replace the app with it.
No Qt here.

Only the packaged exe updates itself. A running exe can't overwrite its own files on Windows, so applying an
update unpacks the new version next to the app, writes a small batch script, starts it and quits: the script waits
for UniView to exit, copies the new files over the old ones (robocopy; `_internal` is mirrored so stale libraries
go away), and starts the new UniView. User files (settings.json, projects.json, plugins/, cache/, mods/, tools/,
logs) are never in the zip, so they're kept.

The download is checked against the SHA-256 GitHub publishes for the release asset before anything is unpacked.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass

from uniview import __version__
from uniview.constants import APP_DIR, RELEASES_API_URL, log

ASSET_PATTERN = re.compile(r"^UniView-v[\w.\-]+-win64\.zip$")
USER_AGENT = f"UniView/{__version__} updater"
CHUNK = 1 << 20


class UpdateError(Exception):
    pass


@dataclass(slots=True)
class Release:
    version: str        # "2.19.0"
    tag: str            # "v2.19.0"
    url: str            # the release page
    notes: str          # release notes (Markdown)
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    sha256: str = ""    # from the asset's "digest" (GitHub), "" if it didn't give one


def version_tuple(text):
    """'v2.18.0' -> (2, 18, 0); anything after the numbers (e.g. '-beta') sorts before the plain release."""
    m = re.match(r"v?(\d+(?:\.\d+)*)(.*)", str(text).strip())
    if not m:
        return ()
    nums = tuple(int(n) for n in m.group(1).split("."))
    nums += (0,) * (3 - len(nums))
    return nums + ((0,) if m.group(2) else (1,))


def is_newer(latest, current=__version__):
    return version_tuple(latest) > version_tuple(current)


def parse_release(data):
    """A Release from the GitHub API's JSON for one release (raises UpdateError if it isn't usable)."""
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("unexpected answer from GitHub")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("the latest release is a draft or pre-release")
    tag = data["tag_name"]
    release = Release(version=tag.lstrip("vV"), tag=tag, url=data.get("html_url", ""), notes=data.get("body") or "")
    for asset in data.get("assets") or []:
        if ASSET_PATTERN.match(asset.get("name", "")):
            release.asset_name = asset["name"]
            release.asset_url = asset.get("browser_download_url", "")
            release.asset_size = int(asset.get("size") or 0)
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                release.sha256 = digest.split(":", 1)[1].lower()
            break
    return release


def _open(url, timeout):
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-HTTPS download: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest_release(url=RELEASES_API_URL, timeout=10):
    with _open(url, timeout) as response:
        return parse_release(json.loads(response.read().decode("utf-8")))


def can_self_update():
    """Only the packaged exe replaces itself; from source, update with git (or download the zip)."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def temp_root():
    return tempfile.gettempdir()


def update_dir():
    return os.path.join(temp_root(), "UniView-update")


def writable(folder):
    """True if UniView can create files in `folder` (not the case under Program Files)."""
    probe = os.path.join(folder, f".uniview-write-test-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download(release, dest_dir=None, progress=None, cancelled=lambda: False):
    """Download the release zip into dest_dir (default: the temp update folder), checking its size and SHA-256.
    progress(done_bytes, total_bytes) is called as it goes. Returns the zip's path."""
    if not release.asset_url:
        raise UpdateError("this release has no Windows zip to download")
    if not release.sha256:
        raise UpdateError("GitHub didn't give a checksum for the download, so it can't be checked")
    dest_dir = dest_dir or update_dir()
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, release.asset_name)
    sha = hashlib.sha256()
    done = 0
    with _open(release.asset_url, 30) as response, open(path + ".part", "wb") as f:
        total = int(response.headers.get("Content-Length") or release.asset_size or 0)
        while True:
            if cancelled():
                raise UpdateError("cancelled")
            chunk = response.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if release.asset_size and done != release.asset_size:
        raise UpdateError(f"download incomplete ({done:,} of {release.asset_size:,} bytes)")
    if sha.hexdigest() != release.sha256:
        os.remove(path + ".part")
        raise UpdateError("the download doesn't match the release's SHA-256 checksum - not installed")
    os.replace(path + ".part", path)
    return path


def unpack(zip_path, dest_dir=None):
    """Unpack the release zip; returns the folder holding UniView.exe. Refuses paths that leave dest_dir."""
    dest_dir = dest_dir or os.path.join(update_dir(), "new")
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = os.path.realpath(os.path.join(root, name))
            if os.path.commonpath([root, target]) != root:
                raise UpdateError(f"unsafe path in the update zip: {name}")
        z.extractall(root)
    for dirpath, _dirs, files in os.walk(root):
        if "UniView.exe" in files:
            return dirpath
    raise UpdateError("UniView.exe isn't in the update zip")


def apply_script(new_dir, app_dir, exe_name, pid):
    """The batch script that swaps the files once UniView (process `pid`) has exited, then starts it again."""
    # Windows tools by full path: another find.exe (e.g. Git's) can come first on PATH. The log goes next to the
    # script (%~dp0). robocopy /IS /IT copies files even when their size and time match the old ones.
    return "\r\n".join([
        "@echo off",
        "setlocal",
        f'set "NEW={new_dir}"',
        f'set "APP={app_dir}"',
        'set "LOG=%~dp0update.log"',
        'set "SYS=%SystemRoot%\\System32"',
        "echo Updating UniView... > \"%LOG%\"",
        ":wait",
        f'"%SYS%\\tasklist.exe" /FI "PID eq {pid}" /NH 2>nul | "%SYS%\\find.exe" " {pid} " >nul',
        "if not errorlevel 1 (",
        '    "%SYS%\\ping.exe" -n 2 127.0.0.1 >nul',  # a 1 s sleep (timeout.exe needs a console)
        "    goto wait",
        ")",
        # _internal is mirrored (old libraries removed); the rest is copied over, keeping the user's own files.
        '"%SYS%\\robocopy.exe" "%NEW%\\_internal" "%APP%\\_internal" /MIR /IS /IT /R:5 /W:1 /NFL /NDL /NP >> "%LOG%"',
        "if errorlevel 8 goto failed",
        '"%SYS%\\robocopy.exe" "%NEW%" "%APP%" /E /IS /IT /XD "%NEW%\\_internal" /R:5 /W:1 /NFL /NDL /NP >> "%LOG%"',
        "if errorlevel 8 goto failed",
        f'start "" "%APP%\\{exe_name}"',
        'rmdir /S /Q "%NEW%"',
        "exit /b 0",
        ":failed",
        "echo Update failed, see the log above. >> \"%LOG%\"",
        f'start "" "%APP%\\{exe_name}"',
        "exit /b 1",
        "",
    ])


def apply(new_dir, app_dir=APP_DIR, exe_path=None, pid=None):
    """Start the swap script (detached). The caller must quit UniView right after."""
    if not can_self_update():
        raise UpdateError("only the packaged UniView.exe can update itself")
    exe_name = os.path.basename(exe_path or sys.executable)
    script = os.path.join(update_dir(), "apply_update.bat")  # outside new_dir, which the script deletes
    with open(script, "w", encoding="utf-8", newline="") as f:
        f.write(apply_script(new_dir, app_dir, exe_name, pid or os.getpid()))
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=flags, close_fds=True, cwd=os.path.dirname(script))
    log.info("Update to %s started (%s)", new_dir, script)
