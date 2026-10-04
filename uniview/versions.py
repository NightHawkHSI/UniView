"""Game version diff: save a snapshot of a game's assets (content fingerprints) and compare two versions -
what was added, removed or changed. Works for any engine (it only needs content_hash())."""

import datetime
import glob
import gzip
import json
import os
import re

from uniview.constants import APP_DIR
from uniview.util import safe_filename

SNAPSHOT_DIR = os.path.join(APP_DIR, "cache", "snapshots")
SKIP_KINDS = ("scene",)


def asset_key(asset):
    """What identifies 'the same asset' across versions: kind, name and the path it's stored under (in Unity
    bundles that's the bundle's main asset path, shared by everything inside it)."""
    return f"{asset.kind}:{asset.name}" + (f"@{asset.path}" if asset.path and asset.path != asset.name else "")


def key_name(key):
    """The asset name part of an asset_key()."""
    return key.split(":", 1)[-1].split("@", 1)[0]


def entries(hashes):
    """Snapshot rows [kind, key, size, hash hex] from [(Asset, digest)]."""
    return [[a.kind, asset_key(a), a.size if a.size is not None else -1, d.hex()]
            for a, d in hashes if d is not None]


def steam_build_id(game_path):
    """Steam's build id of an installed game (from its appmanifest), or ''."""
    path = os.path.abspath(game_path)
    parts = path.replace("\\", "/").split("/")
    low = [p.lower() for p in parts]
    if "common" not in low or low.index("common") < 1:
        return ""
    i = low.index("common")
    steamapps, install_dir = "/".join(parts[:i]), parts[i + 1] if i + 1 < len(parts) else ""
    for manifest in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
        try:
            text = open(manifest, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if re.search(r'"installdir"\s+"' + re.escape(install_dir) + '"', text, re.I):
            m = re.search(r'"buildid"\s+"(\d+)"', text)
            return m.group(1) if m else ""
    return ""


def snapshot_folder(game_name):
    return os.path.join(SNAPSHOT_DIR, safe_filename(game_name) or "game")


def save_snapshot(game_name, rows, label="", engine="", folder=None):
    """Write a snapshot; returns its file path."""
    folder = folder or snapshot_folder(game_name)
    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime.now()
    name = f"{now:%Y-%m-%d_%H%M%S}" + (f"_{safe_filename(label)}" if label else "") + ".json.gz"
    path = os.path.join(folder, name)
    data = {"version": 1, "game": game_name, "label": label, "engine": engine,
            "created": now.isoformat(timespec="seconds"), "assets": rows}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    return path


def load_snapshot(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def list_snapshots(game_name, folder=None):
    """[(path, {"label", "created", "count"})] newest first."""
    out = []
    for path in glob.glob(os.path.join(folder or snapshot_folder(game_name), "*.json.gz")):
        try:
            data = load_snapshot(path)
        except Exception:
            continue
        out.append((path, {"label": data.get("label", ""), "created": data.get("created", ""),
                           "count": len(data.get("assets") or [])}))
    return sorted(out, key=lambda x: x[1]["created"], reverse=True)


def diff(old_rows, new_rows):
    """{"added", "removed", "changed": [(kind, key, old size, new size)], "same": count}. Assets sharing a key
    (copies in several bundles) are compared as a group: changed if their set of fingerprints differs."""
    def index(rows):
        out = {}
        for kind, key, size, digest in rows:
            entry = out.setdefault(key, [kind, [], []])
            entry[1].append(size)
            entry[2].append(digest)
        return out
    old, new = index(old_rows), index(new_rows)
    result = {"added": [], "removed": [], "changed": [], "same": 0}
    for key, (kind, sizes, digests) in new.items():
        if key not in old:
            result["added"].append((kind, key, None, max(sizes)))
        elif sorted(old[key][2]) != sorted(digests):
            result["changed"].append((kind, key, max(old[key][1]), max(sizes)))
        else:
            result["same"] += 1
    for key, (kind, sizes, _d) in old.items():
        if key not in new:
            result["removed"].append((kind, key, max(sizes), None))
    for k in ("added", "removed", "changed"):
        result[k].sort(key=lambda r: (r[0], r[1].lower()))
    return result
