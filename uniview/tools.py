"""Optional helper programs UniView uses, whether they're there, and how to install them. No Qt here.

Downloads go into the app's tools folder (next to UniView.exe), never system-wide, except Blender and the
Unity editor: those are big applications with their own installers (winget / Unity Hub).
"""

import io
import os
import re
import shutil
import subprocess
import urllib.request
import webbrowser
import zipfile

from engines import cpp2il, extdecode
from uniview.constants import log

ILSPY_VERSION = "9.1.0.7988"  # runs on .NET 8+; 11.x doesn't install on the .NET 9 SDK
ILSPY_URL = f"https://www.nuget.org/api/v2/package/ilspycmd/{ILSPY_VERSION}"
DOTNET_INSTALL_URL = "https://dot.net/v1/dotnet-install.ps1"
BLENDER_PAGE = "https://www.blender.org/download/"
UNITY_PAGE = "https://unity.com/download"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tools_dir():
    return extdecode.tools_dir()


def _download(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "UniView"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


# --------------------------------------------------------------------------- .NET runtime + ILSpy

def _runtime_ok(dotnet):
    """Does this dotnet have a .NET runtime 8 or newer?"""
    try:
        out = subprocess.run([dotnet, "--list-runtimes"], capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(int(m) >= 8 for m in re.findall(r"Microsoft\.NETCore\.App (\d+)\.", out))


def find_dotnet():
    """A dotnet.exe with a .NET 8+ runtime: the system one, else UniView's own copy in tools/dotnet."""
    own = os.path.join(tools_dir(), "dotnet", "dotnet.exe")
    for candidate in (shutil.which("dotnet"), own):
        if candidate and os.path.isfile(candidate) and _runtime_ok(candidate):
            return candidate
    return None


def install_dotnet_runtime():
    """Microsoft's official dotnet-install.ps1, runtime only, into tools/dotnet (no admin rights needed)."""
    target = os.path.join(tools_dir(), "dotnet")
    os.makedirs(target, exist_ok=True)
    script = os.path.join(target, "dotnet-install.ps1")
    with open(script, "wb") as f:
        f.write(_download(DOTNET_INSTALL_URL))
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                    "-Runtime", "dotnet", "-Channel", "8.0", "-InstallDir", target, "-NoPath"],
                   check=True, capture_output=True, timeout=900, creationflags=NO_WINDOW)
    exe = os.path.join(target, "dotnet.exe")
    if not _runtime_ok(exe):
        raise RuntimeError("The .NET runtime didn't install correctly")
    return exe


def ilspy_command():
    """Command (list) that runs ilspycmd, or None: UniView's own copy (with a .NET runtime), else a
    `dotnet tool install -g ilspycmd` one."""
    dll = os.path.join(tools_dir(), "ilspycmd", "ilspycmd.dll")
    if os.path.isfile(dll):
        dotnet = find_dotnet()
        if dotnet:
            return [dotnet, dll]
    found = shutil.which("ilspycmd") or os.path.join(os.path.expanduser("~"), ".dotnet", "tools", "ilspycmd.exe")
    return [found] if os.path.isfile(found) else None


def ilspy_env():
    """Environment for running ilspycmd: allow a newer runtime than the .NET 8 it was built for."""
    env = dict(os.environ)
    env["DOTNET_ROLL_FORWARD"] = "Major"
    own = os.path.join(tools_dir(), "dotnet")
    if os.path.isfile(os.path.join(own, "dotnet.exe")):
        env.setdefault("DOTNET_ROOT", own)
    return env


def install_ilspy(progress=None):
    """ilspycmd from NuGet into tools/ilspycmd (plus a private .NET runtime if the PC has none)."""
    if find_dotnet() is None:
        if progress:
            progress("Installing the .NET runtime (about 30 MB)...")
        install_dotnet_runtime()
    if progress:
        progress("Downloading the ILSpy decompiler (about 4 MB)...")
    target = os.path.join(tools_dir(), "ilspycmd")
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    prefix = "tools/net8.0/any/"
    with zipfile.ZipFile(io.BytesIO(_download(ILSPY_URL))) as zf:
        for name in zf.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                out = os.path.join(target, *name[len(prefix):].split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(zf.read(name))
    command = ilspy_command()
    if command is None:
        raise RuntimeError("ilspycmd didn't install correctly")
    return command[-1]


# --------------------------------------------------------------------------- Cpp2IL

def install_cpp2il(progress=None):
    if progress:
        progress("Downloading Cpp2IL (about 15 MB)...")
    return cpp2il.install()


# --------------------------------------------------------------------------- Blender / Unity

def winget():
    return shutil.which("winget")


def install_blender(progress=None):
    """With winget (Windows' package manager) when there is one, else the download page."""
    if winget():
        if progress:
            progress("Installing Blender with winget (a few hundred MB, this takes a while)...")
        subprocess.run(["winget", "install", "-e", "--id", "BlenderFoundation.Blender", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"],
                       check=True, capture_output=True, timeout=3600, creationflags=NO_WINDOW)
        from uniview.settings import find_blender
        exe = find_blender(None)
        if exe:
            return exe
        raise RuntimeError("winget finished, but blender.exe wasn't found - restart UniView")
    webbrowser.open(BLENDER_PAGE)
    return None


def install_unity(progress=None):
    webbrowser.open(UNITY_PAGE)  # Unity Hub's installer is interactive (and needs a Unity account)
    return None


# --------------------------------------------------------------------------- the list shown to the user

class Tool:
    def __init__(self, id, name, purpose, status, install, install_label="Install", recommended=True, where=""):
        self.id, self.name, self.purpose = id, name, purpose
        self.status, self.install = status, install          # status() -> path/text or None
        self.install_label, self.recommended, self.where = install_label, recommended, where


def all_tools(blender_path=""):
    from uniview.settings import find_blender
    from uniview.unity_project import installed_editors

    def unity_status():
        editors = installed_editors()
        return ", ".join(sorted(editors)) if editors else None

    return [
        Tool("vgmstream", "vgmstream (game audio decoder)",
             "Plays and exports Wwise .wem/.bnk, FMOD .bank/.fsb and other game sound formats.",
             extdecode.vgmstream_path, lambda progress=None: extdecode.install_vgmstream(),
             where=os.path.join(tools_dir(), "vgmstream")),
        Tool("ilspy", "ILSpy decompiler",
             "Turns a Unity game's code back into C# for Export as Unity project (Mono games).",
             lambda: (ilspy_command() or [None])[-1], install_ilspy,
             where=os.path.join(tools_dir(), "ilspycmd")),
        Tool("cpp2il", "Cpp2IL (IL2CPP code rebuilder)",
             "Rebuilds an IL2CPP Unity game's script classes for Export as Unity project, so prefabs and "
             "scenes keep their script components.",
             cpp2il.cpp2il_path, install_cpp2il, where=os.path.join(tools_dir(), "cpp2il")),
        Tool("blender", "Blender", "Open in Blender (Ctrl+B) and checking exported models.",
             lambda: find_blender(blender_path or None), install_blender,
             install_label="Install" if winget() else "Download page", recommended=False),
        Tool("unity", "Unity editor", "Opening projects made with Export as Unity project.",
             unity_status, install_unity, install_label="Download page", recommended=False),
    ]


def missing_recommended(blender_path=""):
    out = []
    for tool in all_tools(blender_path):
        try:
            present = tool.status()
        except Exception as e:
            log.debug("Checking %s: %s", tool.id, e)
            present = None
        if tool.recommended and not present:
            out.append(tool)
    return out
