"""Decompile a Unity game's own C# code (every game assembly, not only the ones scripts come from) to
a folder of C# projects with ILSpy.

Mono builds ship their code as .NET DLLs in <Game>_Data/Managed, which decompile to full C#. IL2CPP
builds don't; with Cpp2IL installed, its stub assemblies give the classes, fields and method
signatures (no method bodies)."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from uniview.unity_scripts import ENGINE_ASSEMBLIES, TEST_ASSEMBLIES, decompile, managed_dir

# Well-known libraries games bundle: listed, but not picked by default.
THIRD_PARTY = re.compile(  # name prefixes; short or common words need a boundary after them
    r"^(Newtonsoft|Google|AWSSDK|Rewired|DOTween|DemiLib|FMOD|Steamworks|Facebook|Photon|Sirenix|LZ4|LiteNetLib|"
    r"EOSSDK|Prometheus|Novell|Accessibility|Analytics|DDNA|EasyButtons|Telepathy|kcp2k|Cinemachine|"
    r"TextMeshPro|ICSharpCode|Ionic|SharpZipLib|protobuf|MessagePack|UniTask|UniRx|Zenject|Discord|"
    r"Sentry|PlayFab|Firebase|Amplify|Boxophobic|GPUInstancer|GameLift|Havok|BakeryRuntime|LeanTween|"
    r"MoreMountains|NaughtyAttributes|Pathfinding|QFSW|XNode|I18N|Purchasing|Ludiq|log4net|Serilog|"
    r"websocket-sharp|ZFBrowser|DnsClient|BouncyCastle|NLog|Assembly-CSharp-firstpass"
    r"|(Epic|Mirror|Obi|Shapes|Bolt|Lean|Coffee|com)(\.|_|$))", re.I)
WORKERS = 4


def code_folder(game_dir):
    """(folder of the game's .NET assemblies, "mono" or "il2cpp") or (None, reason). May run Cpp2IL."""
    managed = managed_dir(game_dir)
    if managed:
        return managed, "mono"
    from engines import cpp2il
    if cpp2il.is_il2cpp(game_dir):
        if cpp2il.cpp2il_path() is None:
            return None, ("This game is IL2CPP: its code is compiled to machine code. Install Cpp2IL "
                          "(Help → Optional tools...) to get its classes, fields and method signatures.")
        return cpp2il.stub_assemblies(game_dir), "il2cpp"
    return None, "No .NET assemblies found - is this a Unity game?"


def list_assemblies(folder):
    """[(name, size in bytes, picked by default)] of the decompilable assemblies, game code first."""
    names = [os.path.splitext(f)[0] for f in os.listdir(folder) if f.lower().endswith(".dll")]
    names = [n for n in names if not ENGINE_ASSEMBLIES.match(n) and not TEST_ASSEMBLIES.search(n)]
    prefixes = {}
    for n in names:
        prefixes[n.split(".", 1)[0].lower()] = prefixes.get(n.split(".", 1)[0].lower(), 0) + 1
    out = []
    for n in names:
        if THIRD_PARTY.match(n):
            picked = False
        elif n.startswith("Assembly-CSharp") or "." not in n:
            picked = True
        else:
            picked = prefixes[n.split(".", 1)[0].lower()] >= 3  # a family of the studio's own modules
        out.append((n, os.path.getsize(os.path.join(folder, n + ".dll")), picked))
    out.sort(key=lambda a: (not a[2], a[0].lower()))
    return out


def run(ilspycmd, folder, names, out_root, progress=None, cancelled=None):
    """Decompile `names` (assemblies in `folder`) to out_root/<name>/ (a .csproj and its .cs files),
    a few at a time. progress(done, total, name). Returns {name: file count or error text}."""
    os.makedirs(out_root, exist_ok=True)
    results = {}
    done = 0

    def one(name):
        if cancelled is not None and cancelled():
            return name, "stopped"
        try:
            return name, decompile(ilspycmd, os.path.join(folder, name + ".dll"), os.path.join(out_root, name),
                                   folder, language="Latest", timeout=900, cancelled=cancelled, keep_project=True)
        except Exception as e:
            return name, str(e) or type(e).__name__

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="decompile") as pool:
        futures = [pool.submit(one, n) for n in names]
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result
            done += 1
            if progress is not None:
                progress(done, len(names), name)
    return results


def write_readme(out_root, game_name, kind, results):
    ok = {n: r for n, r in results.items() if isinstance(r, int)}
    failed = {n: r for n, r in results.items() if not isinstance(r, int)}
    lines = [
        f"{game_name} - decompiled C# code",
        f"Made by UniView with ILSpy on {datetime.now():%Y-%m-%d %H:%M}.",
        "",
        ("IL2CPP game: these come from Cpp2IL's stub assemblies - classes, fields and method signatures only, "
         "the method bodies are empty." if kind == "il2cpp" else
         "Mono game: full method bodies. Original comments and local variable names aren't in the game files."),
        "Each folder is one assembly with its .csproj - open the folder in VS Code / Rider / Visual Studio.",
        "",
        "The code still belongs to the game's makers: fine for studying and modding your own copy,",
        "not for publishing.",
        "",
        f"{len(ok)} assembl{'y' if len(ok) == 1 else 'ies'} ({sum(ok.values()):,} files):",
    ]
    lines += [f"  {n}  ({c:,} files)" for n, c in sorted(ok.items())]
    if failed:
        lines += ["", "Failed:"] + [f"  {n}: {e}" for n, e in sorted(failed.items())]
    with open(os.path.join(out_root, "README.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def default_out_dir(game_name):
    base = os.path.join(os.path.expanduser("~"), "Documents", "UniView", "Decompiled")
    safe = re.sub(r'[<>:"/\\|?*]+', "_", game_name).strip() or "Game"
    path = os.path.join(base, safe)
    return path if not os.path.exists(path) else f"{path} {time.strftime('%Y-%m-%d %H%M')}"
