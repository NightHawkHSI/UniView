"""Links from a game data entry to the C# code that runs it, decompiled with ILSpy on demand.

Some games (ECS ones especially) put no behaviour on their prefabs at all: a block's data names an
operation ("LogicOperation": "And") and a function in the game's DLLs does the work. RULES says, per
game data field, which assembly and types hold those functions and how the value maps to a method name.
Add a rule here to teach UniView another game's convention."""

import os
import re
import subprocess

from uniview.unity_scripts import find_ilspycmd, managed_dir

RULES = [
    {   # Robocraft 2 / Techblox (Svelto ECS): logic and maths blocks
        "field": "LogicOperation",
        "assembly": "Gamecraft.Blocks.LogicBlock.dll",
        "types": ("Gamecraft.Blocks.LogicBlock.WiresLogicFunctions",
                  "Gamecraft.Blocks.LogicBlock.WiresMathsFunctions"),
        "methods": ("Logical{}Function", "Maths{}Function"),
        "strip": ("V3",),  # "V3Angle" -> MathsAngleFunction
    },
]

_sources = {}  # (dll, type) -> decompiled C# of the type


def links(game_path, rows):
    """Code links for an entry's (field, value) rows: [{"title", "dll", "managed", "types", "methods"}]."""
    managed = managed_dir(game_path if os.path.isdir(game_path) else os.path.dirname(game_path))
    if managed is None:
        return []
    out = []
    for field, value in rows:
        leaf = field.rsplit(".", 1)[-1]
        for rule in RULES:
            if leaf != rule["field"]:
                continue
            dll = os.path.join(managed, rule["assembly"])
            if not os.path.isfile(dll):
                continue
            op = value.split("  →", 1)[0].strip().strip('"')
            for prefix in rule.get("strip", ()):
                if op.startswith(prefix) and len(op) > len(prefix):
                    op = op[len(prefix):]
            methods = [m.format(op) for m in rule["methods"]]
            out.append({"title": f"{leaf} = {op}", "dll": dll, "managed": managed, "types": rule["types"],
                        "methods": methods})
    return out


def decompile_type(dll, managed, type_name, timeout=120):
    """C# source of one type (cached). Raises RuntimeError when ILSpy is missing or fails."""
    key = (dll, type_name)
    if key in _sources:
        return _sources[key]
    from uniview.tools import ilspy_env
    command = find_ilspycmd()
    if command is None:
        raise RuntimeError("The ILSpy decompiler isn't installed (Help → Optional tools...).")
    proc = subprocess.run(command + ["-t", type_name, "-r", managed, dll], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=ilspy_env(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or "class" not in proc.stdout:
        raise RuntimeError((proc.stderr or proc.stdout or "ilspycmd failed").strip().splitlines()[-1])
    _sources[key] = proc.stdout
    return proc.stdout


def extract_method(source, name):
    """The full text of method `name` in decompiled source (attributes included), or None. Prefers the
    plain-C# body of a Burst function (name$BurstManaged) over its function-pointer wrapper."""
    lines = source.splitlines()
    best = None
    pattern = re.compile(rf"\b{re.escape(name)}(\$BurstManaged)?\s*\(", re.I)
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if not m or "delegate " in line or ";" in line or not re.search(r"\b(static|void|public|private)\b", line):
            continue
        if best is None or (m.group(1) and not best[1]):
            best = (i, bool(m.group(1)))
    if best is None:
        return None
    start = best[0]
    while start > 0 and lines[start - 1].strip().startswith("["):
        start -= 1
    depth, end, opened = 0, None, False
    for j in range(best[0], len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        opened = opened or "{" in lines[j]
        if opened and depth <= 0:
            end = j
            break
    if end is None:
        return None
    body = lines[start:end + 1]
    indent = min((len(s) - len(s.lstrip()) for s in body if s.strip()), default=0)
    return "\n".join(s[indent:] for s in body)


def method_source(link):
    """(type name, method name, C# text) of the first of the link's methods found, or raise RuntimeError."""
    for type_name in link["types"]:
        source = decompile_type(link["dll"], link["managed"], type_name)
        for method in link["methods"]:
            text = extract_method(source, method)
            if text:
                return type_name, method, text
    raise RuntimeError(f"None of {', '.join(link['methods'])} was found in {', '.join(link['types'])}.")
