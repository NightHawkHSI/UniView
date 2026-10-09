"""Unity project export: the game's terrains as real Unity terrains. No Qt here.

Each TerrainData becomes a description (kind "terrain") in Assets/UniView/Build/Terrains/ plus its big
arrays as raw files in Assets/UniView/Build/TerrainData~/ (a "~" folder, which Unity doesn't import).
UniViewBuilder.cs turns them into a TerrainData .asset next to the terrain's GLB, with TerrainLayer
assets, splat maps, holes, trees and grass. Scenes then get the game's own Terrain and TerrainCollider
components pointing at it (their m_TerrainData is resolved like any other asset reference).

Raw files, all little-endian: heights uint16 (res x res, row = z, 32766 = top), holes uint8 (1 = solid),
alphamaps / details uint8 (layer-major, row = z), trees 9 x 4 bytes per instance
(position xyz, width, height, rotation as float32; color, lightmap color as RGBA bytes; prototype int32).
"""

import json
import os
import struct

import numpy as np

from uniview.constants import log

DATA_DIR = ("UniView", "Build", "TerrainData~")
DESC_DIR = ("UniView", "Build", "Terrains")


def is_terrain(asset):
    return asset.kind == "model" and isinstance(asset.ref, dict) and asset.ref.get("type") == "terrain"


def data_key(uid):
    """'terrain:<file>:<id>' (the terrain model asset) -> 'terraindata:<file>:<id>' (references to it)."""
    return "terraindata:" + uid.split(":", 1)[1] if uid.startswith("terrain:") else ""


def plan_terrains(jobs):
    """[(terrain asset, target .asset path)]: next to the terrain's exported model."""
    return [(asset, os.path.splitext(path)[0] + ".asset") for asset, path in jobs if is_terrain(asset)]


def _write_raw(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def terrain_description(data, target, layer_paths, asset_paths, prefab_objects, raw_base, unity_path):
    """The JSON description (dict) for one terrain_export() result and the raw files to write
    {path: bytes}. layer_paths: {layer uid: .terrainlayer path} shared by all terrains (filled in)."""
    files = {}

    def raw(suffix, array):
        if array is None:
            return ""
        path = f"{raw_base}.{suffix}.bytes"
        files[path] = np.ascontiguousarray(array).astype(array.dtype.newbyteorder("<")).tobytes()
        return unity_path(path)

    def texture(uid):
        path = asset_paths.get(uid) if uid else None
        return unity_path(path) if path else ""

    def prefab(key):
        found = (prefab_objects or {}).get(key) if key else None
        return found[0] if found and not found[1] else ""  # only a prefab's root can be a tree / detail mesh

    layers = []
    for layer in data["layers"]:
        path = layer_paths.setdefault(layer["uid"], os.path.join(
            os.path.dirname(target), "Terrain Layers", _safe(layer["name"]) + ".terrainlayer"))
        layers.append({**{k: v for k, v in layer.items() if k not in ("uid", "diffuse", "normal", "mask")},
                       "target": unity_path(path), "diffuse": texture(layer["diffuse"]),
                       "normal": texture(layer["normal"]), "mask": texture(layer["mask"])})
    trees = [{**t, "prefab": prefab(t["prefab"])} for t in data["trees"]]
    details = [{**d, "prefab": prefab(d["prefab"]), "texture": texture(d["texture"])} for d in data["details"]]
    instances = data["tree_instances"]
    tree_bytes = b"".join(struct.pack("<6fIIi", *t) for t in instances)
    if tree_bytes:
        files[f"{raw_base}.trees.bytes"] = tree_bytes
    holes = data["holes"]
    alphamaps, detail_map = data["alphamaps"], data["detail_map"]
    desc = {
        "version": 1, "kind": "terrain", "target": unity_path(target), "nodes": [],
        "terrain": {
            "name": data["name"], "resolution": data["resolution"], "size": data["size"],
            "heights": raw("heights", data["heights"].astype(np.uint16)),
            "holes": raw("holes", holes.astype(np.uint8) if holes is not None else None),
            "alphamapResolution": int(alphamaps.shape[1]) if alphamaps is not None else 0,
            "alphamaps": raw("alphamaps", alphamaps), "baseMapResolution": data["baseMapResolution"],
            "layers": layers, "trees": trees, "treeCount": len(instances),
            "treeInstances": unity_path(f"{raw_base}.trees.bytes") if tree_bytes else "",
            "details": details, "detailResolution": int(detail_map.shape[1]) if detail_map is not None else 0,
            "detailPatch": data["detailPatch"], "coverage": data["coverage"], "detailMap": raw("details", detail_map),
            "grassTint": data["grassTint"], "grass": data["grass"]}}
    return desc, files


def _safe(name):
    from uniview.util import safe_filename
    return safe_filename(name) or "Layer"


def write_terrain_descriptions(session, root, assets_dir, terrain_plan, asset_paths, prefab_objects, cancelled=None):
    """Describe every planned terrain; returns (written, failed, {"terraindata:<file>:<id>": .asset path})."""
    from uniview.unity_project import asset_guid, unity_path, write_folder_metas, write_meta
    written = failed = 0
    links, layer_paths, used = {}, {}, set()
    if not hasattr(session, "terrain_export"):
        return written, failed, links
    for asset, target in terrain_plan:
        if cancelled is not None and cancelled():
            break
        try:
            data = session.terrain_export(asset)
            if data is None:
                continue
            stem = os.path.splitext(os.path.basename(target))[0]
            name, n = stem, 1
            while name.lower() in used:
                n += 1
                name = f"{stem} {n}"
            used.add(name.lower())
            raw_base = os.path.join(assets_dir, *DATA_DIR, name)
            desc, files = terrain_description(data, target, layer_paths, asset_paths, prefab_objects, raw_base,
                                              lambda p: unity_path(root, p))
            for path, blob in files.items():
                _write_raw(path, blob)
            path = os.path.join(assets_dir, *DESC_DIR, name + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{asset.uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            links[data_key(asset.uid)] = target
            written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not describe terrain '%s': %s", asset.name, e)
    return written, failed, links
