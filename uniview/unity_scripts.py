"""The game's code for an exported Unity project (Mono games only). No Qt here.

The assemblies that define the game's scripts are decompiled to C# with ILSpy's command-line tool
(ilspycmd, a .NET tool) into Assets/UniView/GameScripts~ - a folder Unity ignores, so a project whose
decompiled code doesn't compile yet still opens and builds its prefabs and scenes. UniView > Add the
game's scripts (UniViewBuilder.cs) moves it into Assets/GameScripts. Plain libraries the code uses
(Steamworks, JSON...) are copied as plugins instead of decompiled.

IL2CPP games have no managed code to decompile (only GameAssembly.dll), so they get no scripts.
"""

import glob
import os
import re
import shutil
import subprocess

from uniview.constants import log

SCRIPTS_DIR = ("UniView", "GameScripts~")   # under Assets/
ILSPY_VERSION = "9.1.0.7988"                 # works with the .NET 8/9 SDK
INSTALL_HINT = f"dotnet tool install -g ilspycmd --version {ILSPY_VERSION}"

# Assemblies that come with Unity itself or with a Unity package (added as packages, not decompiled).
ENGINE_ASSEMBLIES = re.compile(
    r"^(mscorlib|netstandard|System(\..+)?|Mono\..+|Microsoft\..+|UnityEngine(\..+)?|UnityEditor(\..+)?|"
    r"Unity\..+|nunit\..+|Boo\..+|UnityScript(\..+)?|ExCSS\..+)$", re.I)
TEST_ASSEMBLIES = re.compile(r"\.Tests?$|TestRunner", re.I)

# Unity API renames that turned into compile errors: (min editor version, pattern, replacement).
API_RENAMES = [
    ((6000, 1), r"\bPhysicMaterialCombine\b", "PhysicsMaterialCombine"),
    ((6000, 1), r"\bPhysicMaterial\b", "PhysicsMaterial"),
]


def csharp_version(unity_version):
    """The newest C# the target editor compiles (decompiled code must not use anything newer)."""
    v = tuple(int(n) for n in re.findall(r"\d+", unity_version or "")[:2])
    if v >= (2021, 2):
        return "CSharp9_0"
    if v >= (2020, 2):
        return "CSharp8_0"
    return "CSharp7_3"


def fix_api_renames(folder, unity_version):
    """Apply API_RENAMES for the target editor to the .cs files under folder; returns files changed."""
    v = tuple(int(n) for n in re.findall(r"\d+", unity_version or "")[:2])
    renames = [(re.compile(pattern), repl) for since, pattern, repl in API_RENAMES if v >= since]
    changed = 0
    if not renames:
        return 0
    for base, _dirs, files in os.walk(folder):
        for name in files:
            if not name.endswith(".cs"):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            new = text
            for pattern, repl in renames:
                new = pattern.sub(repl, new)
            if new != text:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
                changed += 1
    return changed


def find_ilspycmd():
    """Path of ilspycmd, or None."""
    found = shutil.which("ilspycmd")
    if found:
        return found
    tool = os.path.join(os.path.expanduser("~"), ".dotnet", "tools", "ilspycmd.exe")
    return tool if os.path.isfile(tool) else None


def managed_dir(game_dir):
    """<Game>_Data/Managed of a Mono build, or None (IL2CPP builds don't have the game's code as .NET)."""
    for data in glob.glob(os.path.join(game_dir, "*_Data")):
        managed = os.path.join(data, "Managed")
        if os.path.isfile(os.path.join(managed, "Assembly-CSharp.dll")):
            return managed
    return None


def plan_assemblies(managed, script_assemblies):
    """(assemblies to decompile, plain library DLLs to copy) - names without .dll.

    script_assemblies: names of the assemblies the game's MonoScripts come from."""
    decompile, libraries = [], []
    with_scripts = {os.path.splitext(a)[0] for a in script_assemblies}
    for path in sorted(glob.glob(os.path.join(managed, "*.dll"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if ENGINE_ASSEMBLIES.match(name) or TEST_ASSEMBLIES.search(name):
            continue
        if name in with_scripts or name.startswith("Assembly-CSharp"):
            decompile.append(name)
        else:
            libraries.append(name)
    return decompile, libraries


def decompile(ilspycmd, dll, out_dir, reference_dir, language="CSharp9_0", timeout=600):
    """Decompile one assembly to a folder of .cs files (one per type). Returns the number of files."""
    os.makedirs(out_dir, exist_ok=True)
    result = subprocess.run([ilspycmd, "-p", "-lv", language, "-o", out_dir, "-r", reference_dir, dll], capture_output=True,
                            text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ilspycmd failed").strip().splitlines()[-1])
    # Project files and assembly attributes would clash when everything compiles into Unity's assemblies.
    shutil.rmtree(os.path.join(out_dir, "Properties"), ignore_errors=True)
    for name in os.listdir(out_dir):
        if name.endswith((".csproj", ".sln")):
            os.remove(os.path.join(out_dir, name))
    return sum(len([f for f in files if f.endswith(".cs")]) for _r, _d, files in os.walk(out_dir))


def export_scripts(session, assets_dir, progress=None, cancelled=None, unity_version=""):
    """Decompile the game's code into Assets/UniView/GameScripts~. Returns (C# files, libraries, note)."""
    game_dir = session.path if os.path.isdir(session.path) else os.path.dirname(session.path)
    managed = managed_dir(game_dir) or managed_dir(os.path.dirname(game_dir))
    if managed is None:
        return 0, 0, "No game scripts: this build has no .NET code to decompile (IL2CPP)."
    ilspycmd = find_ilspycmd()
    if ilspycmd is None:
        return 0, 0, f"No game scripts: install the decompiler first ({INSTALL_HINT}), then export again."
    script_assemblies = session.script_assemblies() if hasattr(session, "script_assemblies") else []
    to_decompile, libraries = plan_assemblies(managed, script_assemblies)
    out_root = os.path.join(assets_dir, *SCRIPTS_DIR)
    shutil.rmtree(out_root, ignore_errors=True)
    files = copied = 0
    for n, name in enumerate(to_decompile, 1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(n, len(to_decompile), f"Decompiling {name}.dll")
        try:
            files += decompile(ilspycmd, os.path.join(managed, name + ".dll"), os.path.join(out_root, name), managed,
                               csharp_version(unity_version))
        except Exception as e:
            log.warning("Could not decompile %s.dll: %s", name, e)
    renamed = fix_api_renames(out_root, unity_version)
    if renamed:
        log.info("Game scripts: updated renamed Unity APIs in %d file(s)", renamed)
    plugins = os.path.join(out_root, "Plugins")
    for name in libraries:
        os.makedirs(plugins, exist_ok=True)
        shutil.copy2(os.path.join(managed, name + ".dll"), plugins)
        copied += 1
    # Native plugins (steam_api64.dll...) the libraries load.
    for native in glob.glob(os.path.join(os.path.dirname(managed), "Plugins", "**", "*.dll"), recursive=True):
        os.makedirs(os.path.join(plugins, "x86_64"), exist_ok=True)
        shutil.copy2(native, os.path.join(plugins, "x86_64"))
        copied += 1
    log.info("Game scripts: %d C# file(s) from %s; %d librar(ies) copied", files, ", ".join(to_decompile), copied)
    return files, copied, ""
