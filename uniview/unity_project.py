"""Export a loaded Unity game as a Unity project folder that opens in the Unity editor. No Qt here.

Layout: Assets/<game folder layout>/... with a .meta per file, ProjectSettings/ProjectVersion.txt
set to the game's Unity version. GUIDs are derived from each asset's uid, so exporting the same game
again gives the same GUIDs and references (materials -> textures, prefabs -> meshes) stay valid.
"""

import hashlib
import os
import re

from uniview.constants import log
from uniview.export import export_ext, export_stem, export_subfolder, write_asset

# Kinds copied as plain files in this version of the exporter.
FILE_KINDS = ("texture", "audio", "text", "font", "video")
# Unity's own built-in resources: every Unity install has them already.
BUILTIN_SOURCES = ("unity default resources", "unity_builtin_extra")
VERSION_RE = re.compile(r"^\d{4}\.\d+\.\d+[abfp]\d+$|^\d{4}\.\d+\.\d+$")


def asset_guid(uid):
    """Stable 32-hex-digit Unity GUID for an asset."""
    return hashlib.md5(f"uniview:{uid}".encode("utf-8")).hexdigest()


def unity_version(*candidates):
    """First candidate that looks like a Unity editor version ('2019.4.19f1'), else ''."""
    for v in candidates:
        v = (v or "").strip()
        if VERSION_RE.match(v):
            return v
    return ""


def write_meta(path, guid):
    """Minimal .meta next to `path`; Unity fills in the importer settings with its defaults."""
    with open(path + ".meta", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"fileFormatVersion: 2\nguid: {guid}\n")


def write_folder_metas(assets_dir, folder):
    """.meta files for `folder` and its parents up to Assets/ (Unity makes them anyway; this keeps GUIDs stable)."""
    folder = os.path.normpath(folder)
    while os.path.normcase(folder) != os.path.normcase(os.path.normpath(assets_dir)):
        meta = folder + ".meta"
        if not os.path.exists(meta):
            rel = os.path.relpath(folder, assets_dir).replace("\\", "/")
            with open(meta, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"fileFormatVersion: 2\nguid: {asset_guid('folder:' + rel)}\nfolderAsset: yes\n")
        folder = os.path.dirname(folder)


def write_project_settings(root, version):
    settings = os.path.join(root, "ProjectSettings")
    os.makedirs(settings, exist_ok=True)
    if version:
        with open(os.path.join(settings, "ProjectVersion.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(f"m_EditorVersion: {version}\n")


def exportable(asset):
    return asset.kind in FILE_KINDS and asset.source not in BUILTIN_SOURCES


def plan(assets, assets_dir):
    """[(asset, target path)] for the files to write, with unique names per folder."""
    out, used = [], set()
    for asset in assets:
        if not exportable(asset):
            continue
        ext = export_ext(asset)
        folder = os.path.normpath(os.path.join(assets_dir, export_subfolder(asset)))
        base = export_stem(asset, ext)
        name, i = base, 1
        while (folder.lower(), name.lower()) in used:
            i += 1
            name = f"{base}_{i}"
        used.add((folder.lower(), name.lower()))
        out.append((asset, os.path.join(folder, f"{name}.{ext}")))
    return out


def export_unity_project(session, root, version="", progress=None, cancelled=None):
    """Write the project; returns (files written, failed count). progress(done, total, text) is called
    now and then; cancelled() -> True stops early."""
    assets_dir = os.path.join(root, "Assets")
    os.makedirs(assets_dir, exist_ok=True)
    write_project_settings(root, version)
    jobs = plan(session.assets, assets_dir)
    log.info("Exporting %d asset(s) as a Unity %s project to %s", len(jobs), version or "(unknown version)", root)
    written = failed = 0
    for n, (asset, path) in enumerate(jobs, 1):
        if cancelled is not None and cancelled():
            log.info("Unity project export cancelled after %d file(s)", written)
            break
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            for out in write_asset(session, asset, path):
                write_meta(out, asset_guid(asset.uid))
                write_folder_metas(assets_dir, os.path.dirname(out))
                written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not export %s '%s': %s", asset.kind, asset.name, e)
        if progress is not None and (n % 10 == 0 or n == len(jobs)):
            progress(n, len(jobs), asset.name)
    log.info("Unity project: %d file(s) written, %d failed", written, failed)
    return written, failed
