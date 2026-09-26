"""External sound decoder: vgmstream (https://github.com/vgmstream/vgmstream, ISC license).

vgmstream plays hundreds of game audio formats - Wwise (.wem/.bnk), FMOD (.bank/.fsb), ADX,
HCA, XMA and more. UniView doesn't ship it; Help -> Install sound decoder downloads the
official Windows build into tools/vgmstream next to UniView, or put vgmstream-cli.exe there
(or on PATH) yourself.
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

from .sdk import app_dir

log = logging.getLogger("viewer.decoder")

VGMSTREAM_URL = "https://github.com/vgmstream/vgmstream/releases/latest/download/vgmstream-win64.zip"
VGMSTREAM_EXTS = {"wem", "bnk", "bank", "fsb", "xwb", "xsb", "adx", "hca", "at3", "at9", "xma", "bik",
                  "binka", "awb", "acb", "sab", "sb", "nus3bank", "msf", "vag", "dsp", "brstm", "bcstm",
                  "bfstm", "genh", "txth", "ktss", "lopus", "ogv", "wv", "aix", "ahx", "fev", "wav", "ogg", "acm"}


def tools_dir():
    return os.path.join(app_dir(), "tools")


def vgmstream_path():
    """vgmstream-cli.exe if UniView can find it, else None."""
    for path in (os.path.join(tools_dir(), "vgmstream", "vgmstream-cli.exe"),
                 os.path.join(app_dir(), "vgmstream-cli.exe"),
                 os.path.join(app_dir(), "plugins", "vgmstream-cli.exe")):
        if os.path.isfile(path):
            return path
    return shutil.which("vgmstream-cli")


def install_vgmstream():
    """Download the official vgmstream Windows build into tools/vgmstream. Returns the exe path."""
    target = os.path.join(tools_dir(), "vgmstream")
    os.makedirs(target, exist_ok=True)
    log.info("Downloading vgmstream from %s", VGMSTREAM_URL)
    with urllib.request.urlopen(VGMSTREAM_URL, timeout=60) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(target)
    exe = os.path.join(target, "vgmstream-cli.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("The download didn't contain vgmstream-cli.exe")
    log.info("vgmstream installed in %s", target)
    return exe


def to_wav(data, ext, subsong=None):
    """Decode a game sound (bytes with its original extension) to WAV bytes with vgmstream."""
    exe = vgmstream_path()
    if exe is None:
        raise NotImplementedError(f"Playing .{ext} sounds needs the vgmstream decoder: "
                                  "Help → Install sound decoder (vgmstream).")
    with tempfile.TemporaryDirectory(prefix="uniview_") as tmp:
        src = os.path.join(tmp, f"sound.{ext}")
        out = os.path.join(tmp, "sound.wav")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [exe, "-o", out]
        if subsong:
            cmd += ["-s", str(subsong)]
        cmd.append(src)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=flags)
        if result.returncode != 0 or not os.path.isfile(out):
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip().splitlines()
            log.debug("vgmstream failed on .%s: %s", ext, detail[-1] if detail else result.returncode)
            raise NotImplementedError("vgmstream found no playable sound in this file - it may only hold "
                                      "metadata (event/strings banks) or be encrypted.")
        with open(out, "rb") as f:
            return f.read()
