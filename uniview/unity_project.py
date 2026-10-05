"""Export a loaded Unity game as a Unity project folder that opens in the Unity editor. No Qt here.

Layout: Assets/<game folder layout>/... with a .meta per file, ProjectSettings/ProjectVersion.txt
set to the Unity version the project is meant for. GUIDs are derived from each asset's uid, so
exporting the same game again gives the same GUIDs and references (model -> texture) stay valid.

Models are GLB files read by Unity's glTFast package (added to Packages/manifest.json) when the
project targets Unity 2020.3 or newer, else OBJ + MTL. GLBs reference the project's PNG textures
instead of carrying copies.
"""

import contextlib
import glob
import hashlib
import json
import os
import re
from types import SimpleNamespace

from engines.sdk import Material
from uniview import pbr
from uniview.constants import log
from uniview.export import (
    export_ext,
    rig_for_export,
    session_materials,
    write_asset,
    write_glb,
    write_obj,
)
from uniview.exr import write_exr
from uniview.search import is_unreadable
from uniview.unity_builder import BUILDER_CS
from uniview.unity_layout import KIND_FOLDERS, Layout, collect_usage
from uniview.unity_layout import target as layout_target
from uniview.unity_materials import convert, mat_yaml
from uniview.unity_packages import (
    ASSEMBLY_PACKAGES,
    builtin_packages,
    game_assemblies,
    packages_for,
    recommended_versions,
)
from uniview.util import safe_filename

FILE_KINDS = ("texture", "audio", "text", "font", "video")
MODEL_KINDS = ("model",)
# Unity's own built-in resources: every Unity install has them already.
BUILTIN_SOURCES = ("unity default resources", "unity_builtin_extra")
VERSION_RE = re.compile(r"^\d{4}\.\d+\.\d+[abfp]\d+$|^\d{4}\.\d+\.\d+$")
HUB_EDITORS = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Unity", "Hub", "Editor")
GLTFAST_MIN = (2020, 3)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tga", ".exr")


# --------------------------------------------------------------------------- versions

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


def version_tuple(version):
    """'2019.4.19f1' -> (2019, 4, 19); '' -> ()."""
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def installed_editors(hub_dirs=None):
    """{version: Unity.exe} of the editors installed through Unity Hub."""
    dirs = list(hub_dirs) if hub_dirs is not None else [HUB_EDITORS] + _hub_secondary_dirs()
    found = {}
    for base in dirs:
        for exe in glob.glob(os.path.join(base, "*", "Editor", "Unity.exe")):
            version = os.path.basename(os.path.dirname(os.path.dirname(exe)))
            if VERSION_RE.match(version):
                found[version] = exe
    return found


def _hub_secondary_dirs():
    try:
        with open(os.path.join(os.environ.get("APPDATA", ""), "UnityHub", "secondaryInstallPath.json"),
                  encoding="utf-8") as f:
            path = json.load(f)
        return [path] if isinstance(path, str) and path else []
    except (OSError, ValueError):
        return []


def target_version(game_version, installed):
    """The Unity version to set up the project for: the game's own if it's installed, else the newest
    installed editor (it upgrades the project), else the game's."""
    if game_version in installed or not installed:
        return game_version
    return max(installed, key=version_tuple)


def uses_gltf(version):
    return version_tuple(version) >= GLTFAST_MIN


def gltfast_version(version):
    return "6.14.1" if version_tuple(version) >= (6000,) else "6.0.1"


# --------------------------------------------------------------------------- project files

def meta_text(guid, texture_type=None, data=None):
    """A minimal .meta; Unity fills in the rest. Textures need serializedVersion, else Unity reads the
    settings as a very old format and imports them as cubemaps.

    data: settings of a float texture that holds data (UnitySession.float_texture) - imported exactly:
    linear, uncompressed, not resized, with the game's filter/wrap mode and mipmaps."""
    text = f"fileFormatVersion: 2\nguid: {guid}\n"
    if texture_type is not None:
        text += f"TextureImporter:\n  serializedVersion: 4\n  textureType: {texture_type}\n"
        if data is not None:
            wrap = data.get("wrap_mode", 0)
            text += (f"  mipmaps:\n    enableMipMap: {int(bool(data.get('mipmaps')))}\n    sRGBTexture: 0\n"
                     f"  textureSettings:\n    serializedVersion: 2\n    filterMode: {data.get('filter_mode', 1)}\n"
                     f"    wrapU: {wrap}\n    wrapV: {wrap}\n    wrapW: {wrap}\n"
                     "  nPOTScale: 0\n  maxTextureSize: 16384\n  alphaUsage: 0\n"
                     "  platformSettings:\n  - buildTarget: DefaultTexturePlatform\n    maxTextureSize: 16384\n"
                     "    textureFormat: -1\n    textureCompression: 0\n")
    return text


def write_meta(path, guid, normal_map=False, data=None):
    is_image = path.lower().endswith(IMAGE_EXTS)
    with open(path + ".meta", "w", encoding="utf-8", newline="\n") as f:
        f.write(meta_text(guid, (1 if normal_map else 0) if is_image else None, data))


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


MANIFEST_RECORD = "uniview-packages.json"  # in Packages/: what the last export put in manifest.json
EXPORT_PACKAGES = {"com.unity.cloud.gltfast", "com.unity.2d.sprite"} | {c for _, cs in ASSEMBLY_PACKAGES for c in cs}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(root, editor_exe, gltfast, extra=None, version=""):
    """Packages/manifest.json: every built-in module the editor ships (as Unity's own default) plus glTFast.
    Without an editor to read the module list from, Unity's default manifest is left alone.

    Re-exporting keeps packages the user added, but not the ones an earlier export chose for another
    editor: a Unity 6 manifest (com.unity.modules.accessibility, collections 6.5...) doesn't resolve in 2021."""
    builtin = os.path.join(os.path.dirname(editor_exe or ""), "Data", "Resources", "PackageManager", "BuiltInPackages")
    if not editor_exe or not os.path.isdir(builtin):
        return False
    modules = sorted(n for n in os.listdir(builtin)
                     if n.startswith("com.unity.modules.") and "." not in n[len("com.unity.modules."):])
    deps = {m: "1.0.0" for m in modules}
    deps["com.unity.cloud.gltfast"] = gltfast
    deps.update(extra or {})  # the Unity packages the game was built with (UI, TextMeshPro, Timeline...)
    if os.path.isdir(os.path.join(builtin, "com.unity.2d.sprite")):
        deps.setdefault("com.unity.2d.sprite", "1.0.0")  # UniViewBuilder.cs cuts sprite sheets with it
    folder = os.path.join(root, "Packages")
    path, record_path = os.path.join(folder, "manifest.json"), os.path.join(folder, MANIFEST_RECORD)
    os.makedirs(folder, exist_ok=True)
    old = _read_json(path).get("dependencies")
    user_added = {}
    if isinstance(old, dict) and old:
        record = _read_json(record_path)
        if record:
            ours = set(record.get("packages", []))
        else:  # exported before the record existed: everything an export could have written
            ours = {n for n in old if n.startswith("com.unity.modules.")} | EXPORT_PACKAGES
        user_added = {n: v for n, v in old.items() if n not in ours and n not in deps}
        if record.get("editor") != version:
            # Resolved for another editor (or unknown); Unity rebuilds the lock file from the new manifest.
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, "packages-lock.json"))
        if user_added:
            log.info("Keeping package(s) added to the project earlier: %s", ", ".join(sorted(user_added)))
    with open(record_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"editor": version, "packages": sorted(deps)}, f, indent=2)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"dependencies": dict(sorted({**user_added, **deps}.items()))}, f, indent=2)
    return True


# --------------------------------------------------------------------------- prefabs (built by Unity)

BUILD_DIR = ("UniView", "Build")        # JSON descriptions, under Assets/
BUILDER_PATH = ("UniView", "Editor", "UniViewBuilder.cs")
# Meshes from Unity's built-in resources that every editor has (Resources.GetBuiltinResource<Mesh>).
BUILTIN_MESHES = ("Cube", "Sphere", "Capsule", "Cylinder", "Plane", "Quad")


def is_prefab(asset):
    return asset.kind == "scene" and isinstance(asset.key, tuple) and asset.key[:1] in (("prefab",), ("root",))


def plan_prefabs(assets, assets_dir, layout=None):
    """[(prefab asset, target .prefab path)], unique per folder."""
    layout = layout or Layout(assets)
    out, used = [], set()
    for asset in assets:
        if not is_prefab(asset) or asset.source in BUILTIN_SOURCES:
            continue
        folder, stem, _how = layout.place(asset, KIND_FOLDERS["prefab"])
        out.append((asset, layout_target(assets_dir, folder, stem or "prefab", "prefab", used)))
    return out


def unity_path(root, path):
    """'Assets/...' path Unity uses for a file in the project."""
    return os.path.relpath(path, root).replace("\\", "/")


class MaterialLibrary:
    """Writes each game material as a .mat the first time a renderer uses it (next to the one prefab
    that uses it, else Assets/Materials/)."""

    def __init__(self, session, root, assets_dir, texture_paths, layout=None):
        self.session, self.root, self.assets_dir = session, root, assets_dir
        self.layout = layout or Layout()
        self.texture_paths = texture_paths  # texture asset key -> exported path
        self.paths = {}                     # material uid -> "Assets/..." ('' if it couldn't be written)
        self.used = set()
        self.repacks = {}                   # (recipe, (channel, texture key)...) -> guid of the repacked PNG
        self.written = self.failed = 0

    def texture_guid(self, asset):
        return asset_guid(asset.uid) if asset.key in self.texture_paths else None

    def repacked(self, folder, recipe, sources):
        """GUID of a PNG repacked from the game's maps for the Standard shader (see unity_materials.convert),
        written once next to the first material that needs it; None if a source can't be read."""
        key = (recipe,) + tuple((ch, asset.key) for ch, asset in sources)
        if key not in self.repacks:
            self.repacks[key] = None
            try:
                with self.session.lock:
                    images = [(ch, self.session.image(asset)) for ch, asset in sources]
                if recipe == "ao":
                    img = pbr.occlusion(*images[0])
                else:
                    img = pbr.unity_metal_gloss(*images[0], second=images[1] if len(images) > 1 else None)
                suffix = "Occlusion" if recipe == "ao" else "MetallicSmoothness"
                stem = safe_filename(f"{os.path.basename(sources[0][1].name)}_{suffix}")
                path, n = os.path.join(folder, stem + ".png"), 1
                while path.lower() in self.used:  # (a file from an earlier export is overwritten)
                    n += 1
                    path = os.path.join(folder, f"{stem}_{n}.png")
                self.used.add(path.lower())
                img.save(path)
                guid = asset_guid("repacked:" + "|".join(map(str, key)))
                write_meta(path, guid)
                self.repacks[key] = guid
            except Exception as e:
                log.warning("Could not repack %s for a material: %s", recipe, e)
        return self.repacks[key]

    def path_for(self, uid):
        if not uid:
            return ""
        if uid not in self.paths:
            self.paths[uid] = ""
            try:
                details = self.session.material_details(uid)
                if details is not None:
                    self.paths[uid] = self._write(uid, details)
                    self.written += 1
            except Exception as e:
                self.failed += 1
                log.warning("Could not export material %s: %s", uid, e)
        return self.paths[uid]

    def _write(self, uid, details):
        folder = os.path.join(self.assets_dir, *[safe_filename(p)[:80] for p in
                                                 self.layout.material_folder(uid).split("/") if p])
        base = safe_filename(details.get("name") or "Material") or "Material"
        name, i = base, 1
        while os.path.join(folder, name).lower() in self.used:
            i += 1
            name = f"{base}_{i}"
        self.used.add(os.path.join(folder, name).lower())
        path = os.path.join(folder, f"{name}.mat")
        os.makedirs(folder, exist_ok=True)

        def make_texture(recipe, sources):
            return self.repacked(folder, recipe, sources)

        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(mat_yaml(convert(details, self.texture_guid, make_texture)))
        write_meta(path, asset_guid(uid))
        write_folder_metas(self.assets_dir, folder)
        return unity_path(self.root, path)


def component_props(props, asset_paths, root, materials=None, prefab_objects=None):
    """unity_components.flatten() entries with references turned into what UniViewBuilder.cs loads:
    "n" (object n of this prefab/scene, s = "GameObject"/"Transform"/component class), "m" (the mesh of
    the model file s) or "a" (the asset file s; for an object of another prefab also c = its class and
    q = its child path in that prefab). References to things not in the project are dropped."""
    out = []
    for e in props:
        if e["t"] != "ref":
            out.append(e)
        elif "node" in e:
            out.append({"p": e["p"], "t": "n", "n": e["node"], "s": e["cls"]})
        elif e.get("kind") == "sprite":
            found = (prefab_objects or {}).get(e["asset"])  # sprite uid -> (texture file, sprite name)
            if found:
                out.append({"p": e["p"], "t": "a", "s": found[0], "c": "Sprite", "q": found[1]})
        elif e.get("kind") == "gameobject":
            found = (prefab_objects or {}).get(e["asset"])
            if found:
                out.append({"p": e["p"], "t": "a", "s": found[0], "c": e.get("cls") or "GameObject", "q": found[1]})
        elif e.get("kind") == "material":
            path = materials.path_for(e["asset"]) if materials is not None else ""
            if path:
                out.append({"p": e["p"], "t": "a", "s": path})
        else:
            path = asset_paths.get(e.get("asset"))
            if path:
                out.append({"p": e["p"], "t": "m" if e.get("kind") == "mesh" else "a", "s": unity_path(root, path)})
    return out


SHARE_MIN_PROPS = 8  # components with fewer values keep them inline


def prefab_description(nodes, target, model_paths, root, builtin_meshes=None, kind="prefab", materials=None,
                       prefab_objects=None):
    """The JSON UniViewBuilder.cs reads: nodes with their model GLB/OBJ (or built-in mesh) instead of mesh uids.
    Components with the same values (a scene full of copies of one particle effect) share one prop list in
    "shared" - written out each time, big maps made JSON files Unity can't read (hundreds of MB)."""
    out = []
    shared, shared_index = [], {}

    def comp_entry(c):
        props = component_props(c["props"], model_paths, root, materials, prefab_objects)
        entry = {"type": c["type"], "script": c.get("script", ""), "props": [], "shared": -1}
        if len(props) < SHARE_MIN_PROPS:
            entry["props"] = props
            return entry
        key = json.dumps(props, sort_keys=True, separators=(",", ":"))
        if key not in shared_index:
            shared_index[key] = len(shared)
            shared.append({"props": props})
        entry["shared"] = shared_index[key]
        return entry

    for n in nodes:
        model = model_paths.get(n.get("mesh")) or model_paths.get(n.get("terrain"))
        if model and not model.lower().endswith((".glb", ".obj")):
            model = None
        builtin = (builtin_meshes or {}).get(n.get("mesh"), "") if not model else ""
        out.append({"name": n["name"], "parent": n["parent"], "active": n["active"],
                    "rendererEnabled": n.get("renderer_enabled", True),
                    "pos": [float(v) for v in n["pos"]], "rot": [float(v) for v in n["rot"]],
                    "scale": [float(v) for v in n["scale"]],
                    "model": unity_path(root, model) if model else "", "builtin": builtin,
                    "skinned": bool(n.get("skinned")),
                    "materials": [materials.path_for(u) for u in n.get("materials") or ()]
                    if materials is not None and (model or builtin) else [],
                    # The game's renderer has no materials, so the game never draws it (collision/helper meshes).
                    "noMaterials": "materials" in n and not n["materials"] and bool(model or builtin),
                    "components": [comp_entry(c) for c in n.get("components") or ()],
                    "layer": int(n.get("layer", 0)), "tag": n.get("tag", "")})
        rect = n.get("rect")
        if rect:  # a UI object: RectTransform layout
            out[-1]["rect"] = {"present": True, "anchorMin": [float(v) for v in rect["anchor_min"]],
                               "anchorMax": [float(v) for v in rect["anchor_max"]],
                               "anchoredPosition": [float(v) for v in rect["pos"]],
                               "sizeDelta": [float(v) for v in rect["size"]], "pivot": [float(v) for v in rect["pivot"]]}
        light = n.get("light")
        if light:
            out[-1]["light"] = {"present": True, "type": light["type"], "color": [float(c) for c in light["color"]],
                                "intensity": light["intensity"], "range": light["range"],
                                "spotAngle": light["spot_angle"], "enabled": light.get("enabled", True)}
    return {"version": 1, "kind": kind, "target": unity_path(root, target), "nodes": out, "shared": shared}


def is_scene(asset):
    return asset.kind == "scene" and isinstance(asset.key, tuple) and asset.key[:1] == ("scene",)


def plan_scenes(assets, assets_dir, layout=None):
    """[(scene asset, target .unity path)]: where the build's scene list says, else Assets/Scenes/."""
    layout = layout or Layout(assets)
    out, used = [], set()
    for asset in assets:
        if not is_scene(asset):
            continue
        folder, stem, _how = layout.place(asset, KIND_FOLDERS["scene"])
        out.append((asset, layout_target(assets_dir, folder, stem or "Scene", "unity", used)))
    return out


def visible_nodes(nodes):
    """Per node: active in the hierarchy (it and all its parents active)."""
    out = []
    for n in nodes:
        parent = n["parent"]
        out.append(bool(n["active"]) and (parent < 0 or out[parent]))
    return out


def write_static_batches(session, nodes, folder, texture_paths):
    """One GLB per static-batch combined mesh used in a scene, each submesh with the material of the renderer
    that owns it; submeshes of hidden objects are left out. Returns [(file written, material uid per submesh)]."""
    by_uid = {a.uid: a for a in session.assets}
    visible = visible_nodes(nodes)
    owners = {}  # combined mesh uid -> [(first, count, materials)]
    for n, vis in zip(nodes, visible):
        batch = n.get("batch")
        if batch and vis and n.get("renderer_enabled", True):
            owners.setdefault(batch["mesh"], []).append((batch["first"], batch["count"], batch["materials"],
                                                         batch.get("material_uids") or []))
    written = []
    default = Material("Default")
    for uid, owned in owners.items():
        asset = by_uid.get(uid)
        if asset is None:
            continue
        with session.lock:
            md = session.mesh(asset)
        materials, index, keep, slots, uids = [], {}, [], [], []
        for j, tris in enumerate(md.submeshes):
            slot = md.material_slots[j] if j < len(md.material_slots) else j
            owner = next((o for o in owned if o[0] <= slot < o[0] + o[1]), None)
            if owner is None:
                continue
            mats, k = owner[2], slot - owner[0]
            mat = mats[k] if k < len(mats) and mats[k] is not None else default
            uids.append(owner[3][k] if k < len(owner[3]) else None)
            if id(mat) not in index:
                index[id(mat)] = len(materials)
                materials.append(mat)
            keep.append(tris)
            slots.append(index[id(mat)])
        if not keep:
            continue
        md.submeshes, md.material_slots = keep, slots
        base = os.path.join(folder, safe_filename(asset.name) or "Combined Mesh")
        path, n = base + ".glb", 1
        while path in (w for w, _u in written):
            n += 1
            path = f"{base}_{n}.glb"
        os.makedirs(folder, exist_ok=True)

        def image_uri(tex_asset):
            target = texture_paths.get(tex_asset.key)
            return os.path.relpath(target, folder).replace(os.sep, "/") if target else None

        write_glb(session, md, materials, path, image_uri=image_uri)
        written.append((path, uids))
    return written


def write_settings_description(session, root, assets_dir, scene_targets):
    """Assets/UniView/Build/ProjectSettings.json: the game's tags, layers, physics, input, time, audio, quality and
    NavMesh settings, and its scenes in build order. Returns True if written."""
    if not hasattr(session, "project_settings"):
        return False
    try:
        settings = session.project_settings()
    except Exception as e:
        log.warning("Could not read the project settings: %s", e)
        return False
    by_level = {}
    for asset, target in scene_targets:
        m = re.match(r"level(\d+)$", asset.source or "", re.I)
        if m:
            by_level[int(m.group(1))] = unity_path(root, target)
    desc = {"version": 1, "kind": "settings", "target": "ProjectSettings/TagManager.asset",
            "managers": settings["managers"], "scenes": [by_level[i] for i in sorted(by_level)],
            "product": settings.get("product", ""), "company": settings.get("company", ""), "nodes": []}
    path = os.path.join(assets_dir, *BUILD_DIR, "ProjectSettings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(desc, f, separators=(",", ":"))
    write_meta(path, asset_guid("uniview:settings"))
    write_folder_metas(assets_dir, os.path.dirname(path))
    log.info("Project settings: %d manager(s), %d tag(s), %d scene(s) in build order",
             len(settings["managers"]), len(settings.get("tags", [])), len(desc["scenes"]))
    return True


def write_scene_descriptions(session, root, assets_dir, model_paths, texture_paths, cancelled=None, materials=None,
                             prefab_objects=None, layout=None):
    """One JSON per scene (+ its static-batch GLBs); returns (files written, failed)."""
    if not hasattr(session, "hierarchy"):
        return 0, 0
    written = failed = 0
    build_dir = os.path.join(assets_dir, *BUILD_DIR)
    builtin_meshes = {a.uid: a.name for a in session.assets
                      if a.kind in MODEL_KINDS and a.source in BUILTIN_SOURCES and a.name in BUILTIN_MESHES}
    for asset, target in plan_scenes(session.assets, assets_dir, layout):
        if cancelled is not None and cancelled():
            break
        try:
            nodes = session.hierarchy(asset)
            if not nodes:
                continue
            desc = prefab_description(nodes, target, model_paths, root, builtin_meshes, kind="scene",
                                      materials=materials, prefab_objects=prefab_objects)
            batches = write_static_batches(session, nodes, target[:-len(".unity")] + "_StaticBatches", texture_paths)
            for glb, _uids in batches:
                write_meta(glb, asset_guid(f"{asset.uid}:batch:{os.path.basename(glb)}"))
                write_folder_metas(assets_dir, os.path.dirname(glb))
            desc["batches"] = [{"model": unity_path(root, glb),
                                "materials": [materials.path_for(u) for u in uids] if materials is not None else []}
                               for glb, uids in batches]
            path = os.path.join(build_dir, os.path.relpath(target, assets_dir) + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{asset.uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            written += 1 + len(batches)
        except Exception as e:
            failed += 1
            log.warning("Could not describe scene '%s': %s", asset.name, e)
    if written:
        write_builder(root, assets_dir)
    return written, failed


def write_builder(root, assets_dir):
    path = os.path.join(assets_dir, *BUILDER_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(BUILDER_CS)
    write_meta(path, asset_guid("uniview:builder"))
    write_folder_metas(assets_dir, os.path.dirname(path))


def prefab_object_map(session, root, assets_dir, layout=None):
    """{"go:<file>:<id>": ("Assets/...prefab", child path)} for every exported prefab's GameObjects."""
    out = {}
    if not hasattr(session, "object_paths"):
        return out
    for asset, target in plan_prefabs(session.assets, assets_dir, layout):
        try:
            for go, path in session.object_paths(asset).items():
                out.setdefault(go, (unity_path(root, target), path))
        except Exception as e:
            log.debug("Objects of '%s': %s", asset.name, e)
    return out


def plan_data(assets, assets_dir, layout=None):
    """[(data asset, target .asset path)]: the game's ScriptableObjects (item stats, loot tables...)."""
    layout = layout or Layout(assets)
    out, used = [], set()
    for asset in assets:
        if asset.kind != "data" or asset.source in BUILTIN_SOURCES:
            continue
        if getattr(getattr(asset.ref, "type", None), "name", "") != "MonoBehaviour":
            continue
        folder, stem, _how = layout.place(asset, KIND_FOLDERS["data"])
        out.append((asset, layout_target(assets_dir, folder, stem or "Data", "asset", used)))
    return out


def write_sprites_description(session, root, assets_dir, model_paths):
    """Assets/UniView/Build/Sprites.json: which exported textures are sprites / sprite sheets, and where each sprite
    is. Returns {sprite uid: (texture file, sprite name)} for links to sprites."""
    if not hasattr(session, "sprite_sheets"):
        return {}
    links, sheets = {}, []
    for sheet in session.sprite_sheets():
        path = model_paths.get(sheet["texture"].uid)
        if not path or not path.lower().endswith(".png"):
            continue
        used, sprites = set(), []
        for sp in sheet["sprites"]:
            name, i = sp["name"], 1
            while name.lower() in used:
                i += 1
                name = f"{sp['name']}_{i}"
            used.add(name.lower())
            sprites.append({"name": name, "rect": sp["rect"], "pivot": sp["pivot"], "border": sp["border"]})
            links[sp["uid"]] = (unity_path(root, path), name)
        sheets.append({"texture": unity_path(root, path), "ppu": sheet["sprites"][0]["ppu"], "sprites": sprites})
    if sheets:
        desc = {"version": 1, "kind": "sprites", "target": "", "sheets": sheets, "nodes": []}
        out = os.path.join(assets_dir, *BUILD_DIR, "Sprites.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(desc, f, separators=(",", ":"))
        write_meta(out, asset_guid("uniview:sprites"))
        write_folder_metas(assets_dir, os.path.dirname(out))
        log.info("Sprites: %d in %d texture(s)", len(links), len(sheets))
    return links


def plan_clips(assets, assets_dir, layout=None):
    """[(animation clip asset, target .anim path)]."""
    layout = layout or Layout(assets)
    out, used = [], set()
    for asset in assets:
        if asset.kind != "animation" or asset.source in BUILTIN_SOURCES:
            continue
        if getattr(getattr(asset.ref, "type", None), "name", "") != "AnimationClip":
            continue
        folder, stem, _how = layout.place(asset, KIND_FOLDERS["animation"])
        out.append((asset, layout_target(assets_dir, folder, stem or "Clip", "anim", used)))
    return out


def write_clip_descriptions(session, root, assets_dir, clip_plan, sprite_links, cancelled=None):
    """One JSON per animation clip (UniViewBuilder.cs makes the .anim); returns (written, failed)."""
    if not hasattr(session, "clip_export"):
        return 0, 0
    written = failed = 0
    build_dir = os.path.join(assets_dir, *BUILD_DIR)
    for asset, target in clip_plan:
        if cancelled is not None and cancelled():
            break
        try:
            clip = session.clip_export(asset)
            curves = [{"path": c["path"], "type": c["type"], "prop": c["prop"], "script": bool(c.get("script")),
                       "keys": [float(x) for t, v in c["keys"] for x in (t, v)]} for c in clip["curves"]]
            objects = []
            for c in clip["object_curves"]:
                keys = [(t, sprite_links.get(uid)) for t, uid in c["keys"] if sprite_links.get(uid)]
                if keys:
                    objects.append({"path": c["path"], "type": c["type"], "prop": c["prop"],
                                    "times": [float(t) for t, _l in keys], "textures": [link[0] for _t, link in keys],
                                    "sprites": [link[1] for _t, link in keys]})
            desc = {"version": 1, "kind": "clip", "target": unity_path(root, target), "nodes": [],
                    "clip": {"length": clip["length"], "rate": float(clip["sample_rate"] or 30), "loop": clip["loop"],
                             "legacy": clip["legacy"], "curves": curves, "objectCurves": objects,
                             "events": [{"time": float(e.get("time") or 0), "function": e.get("function") or "",
                                         "data": str(e.get("data") or ""), "floatValue": float(e.get("float") or 0),
                                         "intValue": int(e.get("int") or 0)} for e in clip["events"]]}}
            path = os.path.join(build_dir, os.path.relpath(target, assets_dir) + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{asset.uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not describe animation '%s': %s", asset.name, e)
    return written, failed


def plan_controllers(session, assets_dir, layout=None):
    """[(uid, target .controller path)] for the game's AnimatorControllers."""
    if not hasattr(session, "controllers"):
        return []
    layout = layout or Layout(session.assets)
    out, used = [], set()
    for uid, name, source in session.controllers():
        if source in BUILTIN_SOURCES:
            continue
        item = SimpleNamespace(uid=uid, name=name or "Controller", kind="controller", path="", source=source)
        folder, stem, _how = layout.place(item, KIND_FOLDERS["controller"])
        out.append((uid, layout_target(assets_dir, folder, stem or "Controller", "controller", used)))
    return out


def controller_json(ctrl, clip_path):
    """unity_controller.decode_controller() output in the shape UniViewBuilder.cs reads (camelCase, clip paths)."""
    def transition(t):
        return {"dest": t["dest"], "duration": t["duration"], "offset": t["offset"], "exitTime": t["exit_time"],
                "hasExitTime": t["has_exit_time"], "fixedDuration": t["fixed_duration"],
                "interruption": t["interruption"], "ordered": t["ordered"], "toSelf": t["to_self"],
                "conditions": t["conditions"]}

    def state(s):
        motion = s["motion"] or {}
        out = {"name": s["name"], "tag": s["tag"], "speed": s["speed"], "cycleOffset": s["cycle_offset"],
               "mirror": s["mirror"], "ikOnFeet": s["ik_on_feet"], "writeDefaults": s["write_defaults"],
               "speedParam": s["speed_param"], "clip": clip_path(motion.get("clip")), "treeNodes": [],
               "transitions": [transition(t) for t in s["transitions"]]}
        if "tree" in motion:
            out["treeNodes"] = [{"type": n["type"], "param": n["param"], "paramY": n["param_y"],
                                 "clip": clip_path(n["clip"]), "children": n["children"],
                                 "thresholds": n["thresholds"], "positions": n["positions"]}
                                for n in motion["tree"]["nodes"]]
        return out

    return {"parameters": [{"name": p["name"], "type": p["type"], "defaultValue": p["default"]} for p in ctrl["parameters"]],
            "layers": [{"name": layer["name"], "weight": layer["weight"], "blending": layer["blending"], "ik": layer["ik"],
                        "defaultState": layer["default_state"], "states": [state(s) for s in layer["states"]],
                        "anyTransitions": [transition(t) for t in layer["any_transitions"]]}
                       for layer in ctrl["layers"]]}


def write_controller_descriptions(session, root, assets_dir, controller_plan, model_paths, cancelled=None):
    """One JSON per AnimatorController (UniViewBuilder.cs makes the .controller); returns (written, failed)."""
    written = failed = 0
    build_dir = os.path.join(assets_dir, *BUILD_DIR)

    def clip_path(uid):
        path = model_paths.get(uid) if uid else None
        return unity_path(root, path) if path else ""

    for uid, target in controller_plan:
        if cancelled is not None and cancelled():
            break
        try:
            desc = {"version": 1, "kind": "controller", "target": unity_path(root, target), "nodes": [],
                    "controller": controller_json(session.controller_export(uid), clip_path)}
            path = os.path.join(build_dir, os.path.relpath(target, assets_dir) + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not describe animator controller %s: %s", uid, e)
    return written, failed


def write_data_descriptions(session, root, assets_dir, data_plan, model_paths, materials=None, prefab_objects=None,
                            cancelled=None):
    """One JSON per data asset (built by UniViewBuilder.cs once the game's scripts compile); returns (written, failed)."""
    if not hasattr(session, "data_asset"):
        return 0, 0
    written = failed = 0
    build_dir = os.path.join(assets_dir, *BUILD_DIR)
    for asset, target in data_plan:
        if cancelled is not None and cancelled():
            break
        try:
            found = session.data_asset(asset)
            if found is None:
                continue
            script, props = found
            desc = {"version": 1, "kind": "data", "target": unity_path(root, target), "script": script,
                    "props": component_props(props, model_paths, root, materials, prefab_objects), "nodes": []}
            path = os.path.join(build_dir, os.path.relpath(target, assets_dir) + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{asset.uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not describe data asset '%s': %s", asset.name, e)
    return written, failed


def write_prefab_descriptions(session, root, assets_dir, model_paths, cancelled=None, materials=None,
                              prefab_objects=None, layout=None):
    """One JSON per prefab under Assets/UniView/Build/; returns (written, failed)."""
    if not hasattr(session, "hierarchy"):
        return 0, 0
    written = failed = 0
    build_dir = os.path.join(assets_dir, *BUILD_DIR)
    builtin_meshes = {a.uid: a.name for a in session.assets
                      if a.kind in MODEL_KINDS and a.source in BUILTIN_SOURCES and a.name in BUILTIN_MESHES}
    for asset, target in plan_prefabs(session.assets, assets_dir, layout):
        if cancelled is not None and cancelled():
            break
        try:
            nodes = session.hierarchy(asset)
            if not nodes:
                continue
            desc = prefab_description(nodes, target, model_paths, root, builtin_meshes, materials=materials,
                                      prefab_objects=prefab_objects)
            path = os.path.join(build_dir, os.path.relpath(target, assets_dir) + ".json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(desc, f, separators=(",", ":"))
            write_meta(path, asset_guid(f"{asset.uid}:description"))
            write_folder_metas(assets_dir, os.path.dirname(path))
            written += 1
        except Exception as e:
            failed += 1
            log.warning("Could not describe prefab '%s': %s", asset.name, e)
    if written:
        write_builder(root, assets_dir)
    return written, failed


# --------------------------------------------------------------------------- exporting

def exportable(asset):
    return asset.kind in FILE_KINDS + MODEL_KINDS and asset.source not in BUILTIN_SOURCES


def plan(assets, assets_dir, model_format="glb", layout=None):
    """[(asset, target path)] for the files to write, with unique names per folder."""
    layout = layout or Layout(assets)
    out, used = [], set()
    for asset in assets:
        if not exportable(asset):
            continue
        ext = export_ext(asset, model_format)
        folder, stem, _how = layout.place(asset)
        if asset.kind in ("text", "file", "audio") and stem.lower().endswith("." + ext.lower()):
            stem = stem[: -len(ext) - 1]
        out.append((asset, layout_target(assets_dir, folder, stem, ext, used)))
    return out


def _write_model(session, asset, path, texture_paths, normal_keys):
    """GLB (textures referenced) or OBJ; returns the files written. Collects normal-map textures."""
    with session.lock:
        md = session.mesh(asset)
        materials = session_materials(session, asset)
        for mat in materials:
            maps = pbr.assign(mat.textures)
            normal_keys.update(maps[ch].asset.key for ch in (pbr.NORMAL, pbr.DETAIL_NORMAL) if ch in maps)
        if not path.lower().endswith(".glb"):
            return write_obj(session, md, materials, path)
        folder = os.path.dirname(path)

        def image_uri(tex_asset):
            target = texture_paths.get(tex_asset.key)
            return os.path.relpath(target, folder).replace("\\", "/") if target else None

        return write_glb(session, md, materials, path, rig=rig_for_export(session, asset, md), image_uri=image_uri)


def _float_texture(session, asset):
    if not hasattr(session, "float_texture"):
        return None
    with session.lock:
        return session.float_texture(asset)


def _skippable(session, asset):
    """Nothing to export (e.g. a texture the game only fills in while running)?"""
    try:
        with session.lock:
            stats = session.stats(asset)
    except Exception:
        return False
    return is_unreadable(stats) or (asset.kind in MODEL_KINDS and not stats.get("tris"))


def find_owners(session, layout, cancelled=None, progress=None):
    """Tell the layout which assets belong next to a model or prefab whose original folder is known:
    the textures of models, and what those prefabs use."""
    for asset in session.assets:
        if cancelled is not None and cancelled():
            return
        if asset.kind not in MODEL_KINDS or not exportable(asset):
            continue
        real = layout.real_place(asset)
        if real is None:
            continue
        try:
            with session.lock:
                materials = session_materials(session, asset)
            for mat in materials:
                for tex in mat.textures:
                    layout.use(tex.asset.uid, real[0], model=True)
        except Exception as e:
            log.debug("Textures of '%s': %s", asset.name, e)
    prefabs = [(a, layout.real_place(a)) for a in session.assets
               if is_prefab(a) and a.source not in BUILTIN_SOURCES]
    collect_usage(session, layout, [(a, real[0]) for a, real in prefabs if real is not None], cancelled, progress)


def export_unity_project(session, root, version="", progress=None, cancelled=None, editor_exe=None,
                         scripts=True, notes=None, bundle_packages=True):
    """Write the project; returns (files written, failed, skipped). progress(done, total, text) is called
    now and then; cancelled() -> True stops early. editor_exe: the Unity.exe the project is for (its
    module list goes into the package manifest). scripts: also decompile the game's code (Mono games, needs
    ilspycmd). notes: a list that gets messages for the user (e.g. why there are no scripts).
    bundle_packages: ship the game's Unity packages and their dependencies with the project (.tgz files in
    LocalPackages/, from Unity's registry) instead of leaving them for Unity to download."""
    assets_dir = os.path.join(root, "Assets")
    os.makedirs(assets_dir, exist_ok=True)
    write_project_settings(root, version)
    gltf = uses_gltf(version)
    game_dir = getattr(session, "path", "") or ""
    game_dir = game_dir if os.path.isdir(game_dir) else os.path.dirname(game_dir)
    bundle_packages = bundle_packages and gltf and bool(editor_exe)
    wanted = packages_for(game_assemblies(game_dir) + list(getattr(session, "script_assemblies", lambda: [])()),
                          recommended_versions(editor_exe), others=bundle_packages) if game_dir else {}
    resolved = {}
    if bundle_packages and wanted:
        from uniview.unity_registry import Registry, bundle, resolve
        if progress is not None:
            progress(0, 0, "Unity packages")
        registry = Registry()
        resolved = resolve(wanted, version, builtin_packages(editor_exe), registry)
        refs, missing = bundle(root, resolved, registry, cancelled, progress)
        log.info("Unity packages shipped in LocalPackages/: %s", ", ".join(f"{n} {resolved[n]}" for n in sorted(refs)))
        if missing and notes is not None:
            notes.append("Unity downloads these packages on first open (couldn't get them): " + ", ".join(missing))
        resolved = {n: refs.get(n, v) for n, v in resolved.items()}
    # The editor's built-in packages keep their versions; everything else is what the resolve picked.
    packages = {**{n: v for n, v in wanted.items() if v}, **resolved}
    if packages:
        log.info("Unity packages the game uses: %s", ", ".join(f"{k} {v}" for k, v in sorted(packages.items())))
    if gltf and not write_manifest(root, editor_exe, gltfast_version(version), packages, version):
        log.warning("No Unity editor to read the package list from: models are saved as OBJ")
        gltf = False
    layout = Layout(session.assets)
    find_owners(session, layout, cancelled, progress)
    jobs = plan(session.assets, assets_dir, "glb" if gltf else "obj", layout)
    texture_paths = {a.key: p for a, p in jobs if a.kind == "texture"}
    # Models first: they tell which textures are normal maps (their .meta says so).
    jobs.sort(key=lambda job: job[0].kind not in MODEL_KINDS)
    normal_keys = set()
    log.info("Exporting %d asset(s) as a Unity %s project to %s (models as %s)", len(jobs),
             version or "(unknown version)", root, "GLB" if gltf else "OBJ")
    written = failed = skipped = 0
    for n, (asset, path) in enumerate(jobs, 1):
        if cancelled is not None and cancelled():
            log.info("Unity project export cancelled after %d file(s)", written)
            break
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = None
            if asset.kind in MODEL_KINDS:
                files = _write_model(session, asset, path, texture_paths, normal_keys)
            elif asset.kind == "texture" and (floats := _float_texture(session, asset)) is not None:
                pixels, data = floats  # data, not colours: .exr keeps the values (a PNG would clamp them)
                path = os.path.splitext(path)[0] + ".exr"
                write_exr(path, pixels, data["float32"])
                texture_paths[asset.key] = path
                files = [path]
            elif asset.kind == "texture" and asset.key in normal_keys and path.lower().endswith(".png"):
                with session.lock:  # DXT5nm / BC5 -> the RGB normal map Unity expects from a PNG
                    pbr.unpack_normal(session.image(asset)).save(path)
                files = [path]
            else:
                files = write_asset(session, asset, path)
            for out in files:
                guid = asset_guid(asset.uid if out == path or len(files) == 1 else f"{asset.uid}:{os.path.basename(out)}")
                write_meta(out, guid, normal_map=asset.key in normal_keys, data=data)
                write_folder_metas(assets_dir, os.path.dirname(out))
                written += 1
        except Exception as e:
            if _skippable(session, asset):
                skipped += 1
                log.debug("Skipped %s '%s': %s", asset.kind, asset.name, e)
            else:
                failed += 1
                log.warning("Could not export %s '%s': %s", asset.kind, asset.name, e)
        if progress is not None and (n % 10 == 0 or n == len(jobs)):
            progress(n, len(jobs), asset.name)
    if not (cancelled is not None and cancelled()):
        if progress is not None:
            progress(len(jobs), len(jobs), "prefabs")
        model_paths = {asset.uid: path for asset, path in jobs if os.path.isfile(path)}  # models, sounds, textures...
        library = (MaterialLibrary(session, root, assets_dir, texture_paths, layout)
                   if hasattr(session, "material_details") else None)
        data_plan = plan_data(session.assets, assets_dir, layout)
        model_paths.update({a.uid: p for a, p in data_plan})  # links to data assets
        clip_plan = plan_clips(session.assets, assets_dir, layout)
        model_paths.update({a.uid: p for a, p in clip_plan})  # links to animation clips
        controller_plan = plan_controllers(session, assets_dir, layout)
        model_paths.update(dict(controller_plan))  # links to animator controllers
        prefab_objects = prefab_object_map(session, root, assets_dir, layout)
        sprite_links = write_sprites_description(session, root, assets_dir, model_paths)
        prefab_objects.update(sprite_links)  # links to sprites
        prefabs, prefab_failed = write_prefab_descriptions(session, root, assets_dir, model_paths, cancelled, library,
                                                           prefab_objects, layout)
        if progress is not None:
            progress(len(jobs), len(jobs), "scenes")
        scenes, scene_failed = write_scene_descriptions(session, root, assets_dir, model_paths, texture_paths,
                                                        cancelled, library, prefab_objects, layout)
        data, data_failed = write_data_descriptions(session, root, assets_dir, data_plan, model_paths, library,
                                                    prefab_objects, cancelled)
        clips, clip_failed = write_clip_descriptions(session, root, assets_dir, clip_plan, sprite_links, cancelled)
        ctrls, ctrl_failed = write_controller_descriptions(session, root, assets_dir, controller_plan, model_paths,
                                                           cancelled)
        written += data + clips + ctrls
        failed += data_failed + clip_failed + ctrl_failed
        if (prefabs or scenes) and write_settings_description(session, root, assets_dir,
                                                                plan_scenes(session.assets, assets_dir, layout)):
            written += 1
        written += prefabs + scenes + (library.written if library else 0)
        if (scripts and hasattr(session, "script_assemblies") and (prefabs or scenes)
                and not (cancelled is not None and cancelled())):
            from uniview.unity_scripts import export_scripts
            try:
                code_files, libraries, note = export_scripts(session, assets_dir, progress, cancelled, version,
                                                              set(packages) if gltf else None)
                written += code_files + libraries
                if note:
                    log.info(note)
                    if notes is not None:
                        notes.append(note)
            except Exception as e:
                failed += 1
                log.warning("Could not export the game's scripts: %s", e)
        failed += prefab_failed + scene_failed + (library.failed if library else 0)
        log.info("Unity project: %d prefab and %d scene file(s) for UniViewBuilder.cs", prefabs, scenes)
    report = layout.report()
    if report:
        log.info(report)
        if notes is not None:
            notes.insert(0, report)
    log.info("Unity project: %d file(s) written, %d failed, %d skipped", written, failed, skipped)
    return written, failed, skipped
