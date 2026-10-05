"""The game's code for an exported Unity project (Mono games only). No Qt here.

The game's own assemblies are copied as they are into Assets/UniView/GameCode: Unity loads a game's
compiled DLLs even from much older versions, so prefabs get their script components without anything
having to compile. The assemblies that define the game's scripts are also decompiled to C# with ILSpy's
command-line tool (ilspycmd) into Assets/UniView/GameScripts~ - a folder Unity ignores - for reading;
UniView > Add the game's scripts (UniViewBuilder.cs) swaps that source in for the compiled code.
Obfuscated assemblies get no source (it can't compile).

IL2CPP games have no .NET code (only GameAssembly.dll + global-metadata.dat). Cpp2IL rebuilds their assemblies
from the metadata as stubs - every class, field and attribute ([SerializeField]...), method bodies that just
return defaults - which go into GameCode the same way: script components attach and keep their saved values,
but the game's logic isn't there.
"""

import glob
import os
import re
import shutil
import subprocess
import time

from engines import cpp2il
from uniview.constants import log

SCRIPTS_DIR = ("UniView", "GameScripts~")   # under Assets/
CODE_DIR = ("UniView", "GameCode")           # the game's compiled assemblies, used as they are
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
    """Command (list) that runs ilspycmd, or None (see uniview.tools)."""
    from uniview.tools import ilspy_command
    return ilspy_command()


def managed_dir(game_dir):
    """<Game>_Data/Managed of a Mono build, or None (IL2CPP builds don't have the game's code as .NET)."""
    for data in glob.glob(os.path.join(game_dir, "*_Data")):
        managed = os.path.join(data, "Managed")
        if os.path.isfile(os.path.join(managed, "Assembly-CSharp.dll")):
            return managed
    return None


def plan_assemblies(managed, script_assemblies, packages=None):
    """(assemblies to decompile, plain library DLLs to copy) - names without .dll.

    script_assemblies: names of the assemblies the game's MonoScripts come from. packages: the Unity
    packages the project gets (None = unknown). When known, a Unity package assembly none of them
    provides (Unity.Localization, Unity.Services.*...) is copied too - without it the game's own
    assemblies that reference it count as broken in Unity - and an assembly an installed package
    already has isn't (duplicate types)."""
    from uniview.unity_packages import provided_by
    decompile, libraries = [], []
    with_scripts = {os.path.splitext(a)[0] for a in script_assemblies}
    for path in sorted(glob.glob(os.path.join(managed, "*.dll"))):
        name = os.path.splitext(os.path.basename(path))[0]
        if TEST_ASSEMBLIES.search(name) or (packages is not None and provided_by(name, packages)):
            continue
        if ENGINE_ASSEMBLIES.match(name):
            if packages is not None and name.startswith("Unity."):
                libraries.append(name)
            continue
        if name in with_scripts or name.startswith("Assembly-CSharp"):
            decompile.append(name)
        else:
            libraries.append(name)
    return decompile, libraries


def decompile(ilspycmd, dll, out_dir, reference_dir, language="CSharp9_0", timeout=600, cancelled=None,
              keep_project=False):
    """Decompile one assembly to a folder of .cs files (one per type). Returns the number of files.
    ilspycmd: the command (list, or one path) that runs ilspycmd. cancelled() -> True stops it early.
    keep_project: keep ILSpy's .csproj, Properties and resources (for reading the code in an IDE, not Unity)."""
    from uniview.tools import ilspy_env
    os.makedirs(out_dir, exist_ok=True)
    command = list(ilspycmd) if isinstance(ilspycmd, (list, tuple)) else [ilspycmd]
    # --nested-directories: a folder per namespace part (Game/UI/Menu/), close to how projects are laid out
    proc = subprocess.Popen(command + ["-p", "--nested-directories", "-lv", language, "-o", out_dir,
                                       "-r", reference_dir, dll],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ilspy_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if (cancelled is not None and cancelled()) or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise RuntimeError("stopped" if cancelled is not None and cancelled() else "took too long")
    if proc.returncode != 0:
        raise RuntimeError((err or out or "ilspycmd failed").strip().splitlines()[-1])
    if keep_project:
        return sum(name.endswith(".cs") for _b, _d, files in os.walk(out_dir) for name in files)
    # Project files and assembly attributes would clash when everything compiles into Unity's assemblies;
    # extracted resources (odd names, binary data) are no use to Unity either.
    shutil.rmtree(os.path.join(out_dir, "Properties"), ignore_errors=True)
    count = 0
    for base, _dirs, files in os.walk(out_dir):
        for name in files:
            if name.endswith(".cs"):
                count += 1
            else:
                os.remove(os.path.join(base, name))
    return count


def looks_obfuscated(folder, share=0.2):
    """Decompiled by ILSpy from an obfuscated assembly? (many type names like 'rZkCodHxnHDKLfTEpwBxOzVzYX')"""
    names = [os.path.splitext(f)[0] for _b, _d, files in os.walk(folder) for f in files if f.endswith(".cs")]
    if len(names) < 20:
        return False

    def scrambled(name):
        letters = [c for c in name if c.isalpha()]
        return name.startswith("-") or (len(letters) >= 15 and sum(c.isupper() for c in letters) / len(letters) > 0.35)

    return sum(scrambled(n) for n in names) / len(names) >= share


def plugin_meta(guid):
    """.meta for a game assembly: explicitly referenced (Unity's own packages don't see its types, so a game
    namespace can't clash with theirs) and not validated (it may use APIs this Unity version dropped)."""
    return ("fileFormatVersion: 2\n"
            f"guid: {guid}\n"
            "PluginImporter:\n"
            "  externalObjects: {}\n"
            "  serializedVersion: 2\n"
            "  iconMap: {}\n"
            "  executionOrder: {}\n"
            "  defineConstraints: []\n"
            "  isPreloaded: 0\n"
            "  isOverridable: 0\n"
            "  isExplicitlyReferenced: 1\n"
            "  validateReferences: 0\n"
            "  platformData:\n"
            "  - first:\n"
            "      Any: \n"
            "    second:\n"
            "      enabled: 1\n"
            "      settings: {}\n"
            "  userData: \n"
            "  assetBundleName: \n"
            "  assetBundleVariant: \n")


def copy_assemblies(managed, names, assets_dir):
    """Copy the game's own assemblies into Assets/UniView/GameCode with plugin .metas; returns how many."""
    from uniview.unity_project import asset_guid, write_folder_metas
    code_dir = os.path.join(assets_dir, *CODE_DIR)
    shutil.rmtree(code_dir, ignore_errors=True)
    os.makedirs(code_dir)
    for name in names:
        target = os.path.join(code_dir, name + ".dll")
        shutil.copy2(os.path.join(managed, name + ".dll"), target)
        with open(target + ".meta", "w", encoding="utf-8", newline="\n") as f:
            f.write(plugin_meta(asset_guid(f"gamecode:{name}")))
    write_folder_metas(assets_dir, code_dir)
    return len(names)


def export_il2cpp_stubs(session, game_dir, assets_dir, progress=None, cancelled=None, packages=None):
    """export_scripts for an IL2CPP build: Cpp2IL's stub assemblies into GameCode (no source - there are no
    method bodies to read)."""
    if cpp2il.cpp2il_path() is None:
        return 0, 0, ("No game scripts: this is an IL2CPP build. Install Cpp2IL (Help → Optional tools...) to "
                      "rebuild its script classes, so prefabs and scenes get their script components.")
    shutil.rmtree(os.path.join(assets_dir, *SCRIPTS_DIR), ignore_errors=True)
    if progress is not None:
        progress(0, 1, "Rebuilding the game's script classes (Cpp2IL)")
    stubs = cpp2il.stub_assemblies(game_dir, cancelled=cancelled)
    script_assemblies = session.script_assemblies() if hasattr(session, "script_assemblies") else []
    to_copy, libraries = plan_assemblies(stubs, script_assemblies, packages)
    copied = copy_assemblies(stubs, to_copy + libraries, assets_dir)
    log.info("Game scripts: %d IL2CPP stub assemblies from Cpp2IL", copied)
    return 0, copied, (f"The game is IL2CPP, so its code was rebuilt from metadata as {copied} stub assemblies "
                       "(Assets/UniView/GameCode): prefabs and scenes get their script components with the "
                       "saved values, but the scripts do nothing when played (no method bodies).")


def export_scripts(session, assets_dir, progress=None, cancelled=None, unity_version="", packages=None):
    """The game's compiled assemblies into Assets/UniView/GameCode (used as they are), and their decompiled
    source into Assets/UniView/GameScripts~ (optional). Returns (C# files, assemblies copied, note)."""
    game_dir = session.path if os.path.isdir(session.path) else os.path.dirname(session.path)
    managed = managed_dir(game_dir) or managed_dir(os.path.dirname(game_dir))
    if os.path.isdir(os.path.join(assets_dir, "GameScripts")):
        # Exporting again into a project whose scripts were already added (and maybe fixed by hand): keep them.
        return 0, 0, "Kept the game's scripts already in Assets/GameScripts (delete that folder to get fresh ones)."
    if managed is None:
        il2cpp_dir = next((d for d in (game_dir, os.path.dirname(game_dir)) if cpp2il.is_il2cpp(d)), None)
        if il2cpp_dir is None:
            return 0, 0, "No game scripts: this build has no .NET code."
        return export_il2cpp_stubs(session, il2cpp_dir, assets_dir, progress, cancelled, packages)
    script_assemblies = session.script_assemblies() if hasattr(session, "script_assemblies") else []
    to_decompile, libraries = plan_assemblies(managed, script_assemblies, packages)
    copied = copy_assemblies(managed, to_decompile + libraries, assets_dir)
    notes = [f"The game's code is in the project as {copied} compiled assemblies (Assets/UniView/GameCode), so "
             "prefabs and scenes get their script components."]
    out_root = os.path.join(assets_dir, *SCRIPTS_DIR)
    shutil.rmtree(out_root, ignore_errors=True)
    ilspycmd = find_ilspycmd()
    if ilspycmd is None:
        notes.append("Install the ILSpy decompiler (Help → Optional tools...) to also get it as C# source.")
        return 0, copied, " ".join(notes)
    files, obfuscated = 0, []
    for n, name in enumerate(to_decompile, 1):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(n, len(to_decompile), f"Decompiling {name}.dll")
        folder = os.path.join(out_root, name)
        try:
            count = decompile(ilspycmd, os.path.join(managed, name + ".dll"), folder, managed,
                              csharp_version(unity_version), cancelled=cancelled)
        except Exception as e:
            log.warning("Could not decompile %s.dll: %s", name, e)
            continue
        if looks_obfuscated(folder):
            # Scrambled names: the source can't compile (and a huge scrambled assembly can stall Unity's import).
            shutil.rmtree(folder, ignore_errors=True)
            obfuscated.append(name)
        else:
            files += count
    renamed = fix_api_renames(out_root, unity_version)
    if renamed:
        log.info("Game scripts: updated renamed Unity APIs in %d file(s)", renamed)
    # Native plugins (steam_api64.dll...) the game's code loads when it runs.
    for native in glob.glob(os.path.join(os.path.dirname(managed), "Plugins", "**", "*.dll"), recursive=True):
        os.makedirs(os.path.join(out_root, "Plugins", "x86_64"), exist_ok=True)
        shutil.copy2(native, os.path.join(out_root, "Plugins", "x86_64"))
    if files:
        notes.append(f"Its decompiled C# source ({files:,} files) is in Assets/UniView/GameScripts~ for reading; "
                     "UniView → Add the game's scripts swaps it in for the compiled code (it usually needs "
                     "fixes before it compiles).")
    if obfuscated:
        notes.append(f"No source for {', '.join(obfuscated)}: obfuscated (scrambled names), so it can't be "
                     "recompiled. The compiled version works.")
    log.info("Game scripts: %d assemblies copied; %d C# file(s) decompiled; obfuscated: %s", copied, files,
             ", ".join(obfuscated) or "none")
    return files, copied, " ".join(notes)
