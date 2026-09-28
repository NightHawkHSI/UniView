"""Unity packages a game was built with, found from the assemblies it ships, for the exported project's
Packages/manifest.json. Versions are the ones the target editor recommends. No Qt here."""

import glob
import json
import os
import re

# Assembly name -> packages that provide it, preferred first (TextMeshPro moved into uGUI in Unity 6).
ASSEMBLY_PACKAGES = [
    (r"UnityEngine\.UI", ["com.unity.ugui"]),
    (r"Unity\.TextMeshPro", ["com.unity.textmeshpro", "com.unity.ugui"]),
    (r"Unity\.Timeline", ["com.unity.timeline"]),
    (r"Unity\.Postprocessing\.Runtime", ["com.unity.postprocessing"]),
    (r"(Unity\.)?Cinemachine", ["com.unity.cinemachine"]),
    (r"Unity\.InputSystem", ["com.unity.inputsystem"]),
    (r"Unity\.AI\.Navigation", ["com.unity.ai.navigation"]),
    (r"Unity\.Mathematics", ["com.unity.mathematics"]),
    (r"Unity\.Burst", ["com.unity.burst"]),
    (r"Unity\.Collections", ["com.unity.collections"]),
    (r"Unity\.(Addressables|ResourceManager)", ["com.unity.addressables"]),
    (r"Unity\.Animation\.Rigging", ["com.unity.animation.rigging"]),
    (r"Unity\.ProBuilder", ["com.unity.probuilder"]),
    (r"Unity\.Splines", ["com.unity.splines"]),
    (r"Unity\.Netcode\.Runtime", ["com.unity.netcode.gameobjects"]),
    (r"Unity\.Recorder", ["com.unity.recorder"]),
    (r"Unity\.VisualEffectGraph\.Runtime", ["com.unity.visualeffectgraph"]),
]


def recommended_versions(editor_exe):
    """{package: version} the editor recommends (its PackageManager/Editor/manifest.json), or {}."""
    path = os.path.join(os.path.dirname(editor_exe or ""), "Data", "Resources", "PackageManager", "Editor",
                        "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            packages = json.load(f).get("packages", {})
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for name, info in packages.items():
        version = info.get("version") if isinstance(info, dict) else info
        if isinstance(version, str) and version:
            out[name] = version
    return out


def packages_for(assembly_names, recommended):
    """{package: version} for the assemblies a game ships (names with or without .dll)."""
    out = {}
    names = {os.path.splitext(n)[0] if n.lower().endswith(".dll") else n for n in assembly_names}
    for pattern, candidates in ASSEMBLY_PACKAGES:
        if any(re.fullmatch(pattern, n) for n in names):
            package = next((c for c in candidates if c in recommended), None)
            if package:
                out[package] = recommended[package]
    return out


def game_assemblies(game_dir):
    """Names of the managed assemblies a Mono build ships ([] for IL2CPP)."""
    out = []
    for data in glob.glob(os.path.join(game_dir, "*_Data")):
        out += [os.path.basename(p) for p in glob.glob(os.path.join(data, "Managed", "*.dll"))]
    return out
