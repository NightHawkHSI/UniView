"""Where exported assets go in a Unity project: the game's own folders where the build kept them,
otherwise a guess. No Qt here.

Builds keep original paths only in a few places: AssetBundle / Addressables entries (usually
lowercase), the Resources index (relative to a Resources folder) and the scene list (full paths,
original capitals). Everything else only has a name, so it goes next to the one prefab that uses it,
or into a folder per type.
"""

import os

from uniview.util import safe_filename

# Folders for assets the build kept no path for, by asset kind (and for the files the exporter makes).
KIND_FOLDERS = {"texture": "Textures", "audio": "Audio", "model": "Models", "text": "TextAssets", "font": "Fonts",
                "video": "Video", "data": "Data", "animation": "Animations", "controller": "Animators",
                "prefab": "Prefabs", "scene": "Scenes", "material": "Materials"}
# Container files that hold several assets (a model's meshes and textures): the assets get a folder named after it.
MULTI_ASSET_EXTS = (".fbx", ".obj", ".blend", ".dae", ".3ds", ".max", ".ma", ".mb", ".asset", ".prefab", ".unity",
                    ".spriteatlas", ".psd", ".psb")

REAL, NEXT_TO, BY_TYPE = "real", "next_to", "by_type"


def original_path(path):
    """A build's path for an asset -> path relative to Assets/ ('Art/Gun.png'), or '' when it isn't
    one (package content, empty). Resources index paths ('prefabs/player') are relative to a Resources
    folder; the one they came from is unknown, so they go in Assets/Resources/."""
    p = (path or "").replace("\\", "/").strip("/")
    low = p.lower()
    if not p or low.startswith(("packages/", "library/")):
        return ""
    if low.startswith("assets/"):
        return p[len("assets/"):]
    if low == "assets":
        return ""
    if low.startswith("resources/"):
        return p  # already marked as a Resources index path by the engine
    return "Resources/" + p


class CaseMap:
    """Original capitals of folder names, learned from the paths a build kept with them (scene list,
    Addressables). Bundle and Resources paths are lowercase; those folders get their capitals back when
    any other path has the same folder."""

    def __init__(self, paths=()):
        self._names = {}  # lowercase path prefix -> cased last segment
        for p in paths:
            self.add(p)

    def add(self, path):
        parts = original_path(path).split("/")
        for i, part in enumerate(parts[:-1]):
            if part != part.lower():
                self._names.setdefault("/".join(parts[:i + 1]).lower(), part)

    def fix(self, folder):
        """Folder path (relative to Assets/, '/'-separated) with the capitals found for it."""
        parts = [p for p in folder.split("/") if p]
        return "/".join(self._names.get("/".join(parts[:i + 1]).lower(), part) for i, part in enumerate(parts))


def _stem(name):
    return os.path.basename((name or "").replace("\\", "/"))


class Layout:
    """Folder (relative to Assets/, '/'-separated) and file stem for each exported asset, and how
    each folder was found (REAL, NEXT_TO, BY_TYPE) for the report."""

    def __init__(self, assets=(), extra_paths=()):
        self.case = CaseMap([a.path for a in assets if getattr(a, "path", "")] + list(extra_paths))
        self.owners = {}  # asset uid -> {folders of the prefabs that use it}
        self.model_owners = {}  # asset uid -> {folders of the model files that use it}
        self.counts = {REAL: 0, NEXT_TO: 0, BY_TYPE: 0}
        self._placed = {}  # uid -> place() result (each asset is counted once, however often it's planned)

    def real_place(self, asset):
        """(folder, stem) from the path the build kept, or None."""
        rel = original_path(getattr(asset, "path", ""))
        if not rel:
            return None
        folder, _, file = rel.rpartition("/")
        stem, ext = os.path.splitext(file)
        name = _stem(asset.name)
        if name.lower().startswith("prefab: ") or name.lower().startswith("scene: "):
            name = name.split(": ", 1)[1]
        name = _stem(name)
        if stem.lower() != name.lower() and name and ext.lower() in MULTI_ASSET_EXTS:
            # one of several assets inside a model or asset file: a folder named after the file
            folder = f"{folder}/{stem}" if folder else stem
            stem = name
        elif stem.lower() == name.lower() and name:
            stem = name  # the asset's own name has the capitals
        return self.case.fix(folder), stem

    def use(self, uid, folder, model=False):
        """Record that a prefab in `folder` uses asset `uid`. model=True: a model file uses it (its
        texture): that wins over prefabs, which usually sit elsewhere than their art."""
        if uid and folder is not None:
            (self.model_owners if model else self.owners).setdefault(uid, set()).add(folder)

    def _owners(self, uid):
        return self.model_owners.get(uid) or self.owners.get(uid) or set()

    def place(self, asset, kind_folder=None):
        """(folder, stem, how). kind_folder: folder for the BY_TYPE case (default from the asset kind)."""
        uid = getattr(asset, "uid", None)
        if uid is not None and uid in self._placed:
            return self._placed[uid]
        real = self.real_place(asset)
        if real is not None:
            how, (folder, stem) = REAL, real
        else:
            stem = _stem(asset.name.split(": ", 1)[-1] if asset.kind == "scene" else asset.name)
            owners = self._owners(asset.uid)
            if len(owners) == 1:
                how, folder = NEXT_TO, next(iter(owners))
            else:
                how, folder = BY_TYPE, kind_folder or KIND_FOLDERS.get(asset.kind, asset.kind.title())
        self.counts[how] += 1
        if uid is not None:
            self._placed[uid] = (folder, stem, how)
        return folder, stem, how

    def material_folder(self, uid):
        """Folder for a material .mat: next to the one prefab/model that uses it, else Materials."""
        owners = self._owners(uid)
        if len(owners) == 1:
            folder = next(iter(owners))
            return f"{folder}/Materials" if folder else "Materials"
        return "Materials"

    def report(self):
        """One line for the user, or '' when nothing was placed."""
        real, near, typed = self.counts[REAL], self.counts[NEXT_TO], self.counts[BY_TYPE]
        total = real + near + typed
        if not total:
            return ""
        parts = [f"{real:,} of {total:,} assets are in the game's original folders"]
        if near:
            parts.append(f"{near:,} are next to the one prefab or model that uses them")
        if typed:
            parts.append(f"{typed:,} are sorted into folders by type (the build didn't keep where they were)")
        return "Folders: " + ", ".join(parts) + "."


def target(assets_dir, folder, stem, ext, used):
    """Unique path Assets/<folder>/<stem>.<ext>; `used` is a set shared by one plan."""
    parts = [safe_filename(p)[:80] for p in folder.split("/") if p]
    folder_path = os.path.normpath(os.path.join(assets_dir, *parts)) if parts else assets_dir
    base = safe_filename(stem) or "Asset"
    name, i = base, 1
    while (folder_path.lower(), name.lower(), ext.lower()) in used:
        i += 1
        name = f"{base}_{i}"
    used.add((folder_path.lower(), name.lower(), ext.lower()))
    return os.path.join(folder_path, f"{name}.{ext}")


def _walk_assets(value, out):
    """Asset uids in flattened component values ({"asset": uid, ...} anywhere inside)."""
    if isinstance(value, dict):
        uid = value.get("asset")
        if isinstance(uid, str):
            out.add(uid)
        for v in value.values():
            _walk_assets(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_assets(v, out)


def prefab_uses(nodes, material_textures=None):
    """Asset uids a prefab hierarchy uses: meshes, materials (and their textures), component assets."""
    used = set()
    for node in nodes or ():
        for key in ("mesh",):
            if isinstance(node.get(key), str):
                used.add(node[key])
        batch = node.get("batch") or {}
        if isinstance(batch.get("mesh"), str):
            used.add(batch["mesh"])
        for mat in list(node.get("materials") or ()) + list(batch.get("material_uids") or ()):
            if isinstance(mat, str):
                used.add(mat)
                if material_textures is not None:
                    used.update(material_textures(mat))
        _walk_assets(node.get("components"), used)
    return used


def collect_usage(session, layout, prefabs, cancelled=None, progress=None):
    """Fill layout.owners from the prefabs whose folder is known (only those tell where their assets
    belong). prefabs: [(prefab asset, folder)]. Materials' textures count as used by the prefab."""
    if not hasattr(session, "hierarchy"):
        return
    textures = {}

    def material_textures(uid):
        if uid not in textures:
            textures[uid] = []
            try:
                details = session.material_details(uid) if hasattr(session, "material_details") else None
                textures[uid] = [t[1].uid for t in (details or {}).get("textures", ()) if t[1] is not None]
            except Exception:
                pass
        return textures[uid]

    for n, (asset, folder) in enumerate(prefabs, 1):
        if cancelled is not None and cancelled():
            return
        if progress is not None and (n % 5 == 0 or n == len(prefabs)):
            progress(n, len(prefabs), "Working out folders")
        try:
            for uid in prefab_uses(session.hierarchy(asset), material_textures):
                layout.use(uid, folder)
        except Exception:
            continue
