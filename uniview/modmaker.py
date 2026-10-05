"""Mod Maker: replace a game's assets with your own files and write modded copies of the game files.

A mod is saved per game in <app>/mods/<game>/:
  mod.json   the replacements (which asset, which file) and which game files are installed
  files/     copies of the replacement files (moving or deleting the originals doesn't break the mod)
  backup/    the game's original files, kept while the mod is installed
  build/     the modded game files of the last install (staging)

Unity only for now: textures, sprites (pasted into their sprite sheet), TextAssets, data assets
(MonoBehaviour fields from a JSON file), fonts, AudioClips (16-bit PCM FSB5 appended to the .resource file)
and meshes (from OBJ/glTF, in the game mesh's own vertex layout). Each changed game file is rebuilt from the original
with UnityPy: the object is rewritten, everything else in the file stays as it was.
"""

import hashlib
import json
import math
import os
import shutil
import struct
import time

from uniview.constants import APP_DIR, log
from uniview.util import norm_path, read_json, safe_filename, write_json

MODS_DIR = os.path.join(APP_DIR, "mods")
REPLACEABLE = ("texture", "sprite", "text", "data", "font", "audio", "model")
IMAGE_FILES = "Images (*.png *.jpg *.jpeg *.tga *.bmp *.webp *.dds)"
FILE_FILTERS = {
    "texture": IMAGE_FILES,
    "sprite": IMAGE_FILES,
    "text": "Text files (*.txt *.json *.xml *.csv *.yaml *.bytes);;All files (*)",
    "data": "JSON (*.json)",
    "font": "Fonts (*.ttf *.otf)",
    "audio": "Sounds (*.wav *.ogg *.mp3 *.flac *.aif *.aiff);;All files (*)",
    "model": "3D models (*.glb *.gltf *.obj)",
}
RESOURCE_ALIGN = 32  # Unity starts each clip's data in a .resource file on a 32-byte boundary


class ModError(Exception):
    """Something the user can fix (shown as is)."""


def can_mod(session):
    return session is not None and getattr(session.plugin, "id", "") == "unity"


def can_replace(session, asset):
    return can_mod(session) and asset.kind in REPLACEABLE and asset.ref is not None and hasattr(asset.ref, "path_id")


# --------------------------------------------------------------------------- where an asset lives

def _outer_file(session, assets_file):
    """Path on disk of the game file that holds `assets_file` (a bundle for objects inside AssetBundles)."""
    from UnityPy.files import File
    top = assets_file
    while isinstance(getattr(top, "parent", None), File):
        top = top.parent
    for path, f in session.env.files.items():
        if f is top:
            if not os.path.isfile(path):
                raise ModError(f"'{os.path.basename(path)}' is split into several files (.split0, .split1 ...), "
                               "which Mod Maker can't write yet.")
            return path
    raise ModError("Couldn't find which game file holds this asset.")


def _rel(game_dir, path):
    root = game_dir if os.path.isdir(game_dir) else os.path.dirname(game_dir)
    rel = os.path.relpath(path, root)
    return os.path.basename(path) if rel.startswith("..") else rel.replace("\\", "/")


def _sprite_target(reader):
    """(texture ObjectReader, (x, y, w, h) from the bottom-left in texture pixels) of a sprite."""
    sprite = reader.read()
    data = sprite.m_RD
    atlas = sprite.m_SpriteAtlas.deref_parse_as_object() if sprite.m_SpriteAtlas else None
    if atlas is not None:
        data = next((v for k, v in atlas.m_RenderDataMap if k == sprite.m_RenderDataKey), data)
    settings = int(getattr(data, "settingsRaw", 0) or 0)
    if settings & 1 and (settings >> 2) & 0xF:
        raise ModError("This sprite is stored rotated/flipped in its sprite sheet, which Mod Maker can't paste "
                       "into yet. Replace the whole sprite sheet texture instead.")
    tex = data.texture.deref()
    r = data.textureRect
    return tex, (float(r.x), float(r.y), float(r.width), float(r.height))


def target_of(session, asset):
    """{"file", "inner", "path_id"[, "rect"]}: what an edit of `asset` rewrites. Sprites rewrite their texture."""
    reader = asset.ref
    rect = None
    if asset.kind == "sprite":
        reader, rect = _sprite_target(reader)
    target = {"file": _rel(session.path, _outer_file(session, reader.assets_file)),
              "inner": reader.assets_file.name, "path_id": int(reader.path_id)}
    if rect is not None:
        target["rect"] = list(rect)
        tex = reader.read()
        target["texture_size"] = [int(tex.m_Width), int(tex.m_Height)]
    return target


# --------------------------------------------------------------------------- the mod project

def project_dir(game_path, root=None):
    name = os.path.basename(os.path.normpath(game_path)) or "game"
    digest = hashlib.sha1(norm_path(game_path).encode("utf-8")).hexdigest()[:8]
    return os.path.join(root or MODS_DIR, f"{safe_filename(name)}-{digest}")


def _stat(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


class ModProject:
    """The replacements made for one game, and what of them is installed."""

    def __init__(self, game_path, root=None):
        self.game_path = game_path
        self.dir = project_dir(game_path, root)
        self.file = os.path.join(self.dir, "mod.json")
        data = read_json(self.file, "mod") or {}
        self.name = data.get("name") or f"{os.path.basename(os.path.normpath(game_path))} mod"
        self.edits = data.get("edits") or []          # [{uid, kind, name, target, file, original_name, added}]
        self.installed = data.get("installed") or {}  # game file (rel) -> [size, mtime_ns] we wrote

    @property
    def game_dir(self):
        return self.game_path if os.path.isdir(self.game_path) else os.path.dirname(self.game_path)

    @property
    def files_dir(self):
        return os.path.join(self.dir, "files")

    @property
    def backup_dir(self):
        return os.path.join(self.dir, "backup")

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        write_json(self.file, {"name": self.name, "game": self.game_path, "edits": self.edits,
                               "installed": self.installed})

    def edit_for(self, uid):
        return next((e for e in self.edits if e["uid"] == uid), None)

    def add(self, session, asset, src, options=None):
        """Replace `asset` with the file `src` (checked first; raises ModError if it can't be used)."""
        if not can_replace(session, asset):
            raise ModError(f"Mod Maker can't replace {asset.kind} assets yet.")
        options = dict(options or {})
        check_replacement(session, asset, src, options)
        target = target_of(session, asset)
        self.remove(asset.uid)
        os.makedirs(self.files_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        stored = f"{safe_filename(asset.name)[:60]}-{hashlib.sha1(asset.uid.encode()).hexdigest()[:8]}{ext}"
        if ext == ".gltf":  # a folder with the .gltf and the .bin files it points to
            folder = os.path.join(self.files_dir, os.path.splitext(stored)[0])
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder)
            for name in [os.path.basename(src)] + _gltf_files(src):
                os.makedirs(os.path.dirname(os.path.join(folder, name)) or folder, exist_ok=True)
                shutil.copyfile(os.path.join(os.path.dirname(src), name), os.path.join(folder, name))
            stored = os.path.splitext(stored)[0] + "/" + os.path.basename(src)
        else:
            shutil.copyfile(src, os.path.join(self.files_dir, stored))
        self.edits.append({"uid": asset.uid, "kind": asset.kind, "name": asset.name, "target": target,
                           "file": stored, "original_name": os.path.basename(src), "options": options,
                           "added": time.strftime("%Y-%m-%d %H:%M")})
        self.save()
        log.info("Mod: '%s' (%s) will be replaced with %s", asset.name, asset.kind, src)
        return options.get("notes") or []

    def remove(self, uid):
        edit = self.edit_for(uid)
        if edit is None:
            return
        self.edits.remove(edit)
        if not any(e["file"] == edit["file"] for e in self.edits):
            path = os.path.join(self.files_dir, edit["file"])
            try:
                if "/" in edit["file"]:  # a .gltf with its .bin files
                    shutil.rmtree(os.path.dirname(path))
                else:
                    os.remove(path)
            except OSError:
                pass
        self.save()

    def changed_files(self):
        """Game files (rel) the edits rewrite, sorted."""
        return sorted({e["target"]["file"] for e in self.edits})

    def original(self, rel):
        """Path of the unmodded version of a game file: the backup while our copy is installed, else the game's."""
        game = os.path.join(self.game_dir, rel)
        backup = os.path.join(self.backup_dir, rel)
        if rel in self.installed and os.path.isfile(backup):
            try:
                if os.path.isfile(game) and _stat(game) == self.installed[rel]:
                    return backup
            except OSError:
                pass
            # The game file changed since we installed (the game was updated): that's the new original.
            log.info("Mod: %s changed since the mod was installed - using the game's new file", rel)
            self.installed.pop(rel, None)
            os.remove(backup)
            self.save()
        return game


# --------------------------------------------------------------------------- checks when adding

def check_replacement(session, asset, src, options):
    """Raise ModError if `src` can't replace `asset`; fills `options` with what the build needs."""
    if not os.path.isfile(src):
        raise ModError(f"File not found:\n{src}")
    if asset.kind in ("texture", "sprite"):
        from PIL import Image
        try:
            with Image.open(src) as img:
                options["size"] = [img.width, img.height]
        except Exception as e:
            raise ModError(f"Couldn't open that image: {e}") from e
    elif asset.kind == "font":
        with open(src, "rb") as f:
            magic = f.read(4)
        if magic not in (b"\0\1\0\0", b"OTTO", b"true", b"ttcf"):
            raise ModError("That isn't a TrueType/OpenType font (.ttf / .otf).")
    elif asset.kind == "audio":
        with session.lock:
            tree = asset.ref.read_typetree()
        if not (tree.get("m_Resource") or {}).get("m_Source"):
            raise ModError("This AudioClip keeps its sound inside the asset (Unity 4 or older), which Mod Maker "
                           "can't write yet.")
        from uniview import fsb
        try:
            pcm, channels, rate = fsb.decode(src)
        except Exception as e:
            raise ModError(f"Couldn't read that sound file: {e}") from e
        if not pcm:
            raise ModError("That sound file has no sound in it.")
        options["seconds"] = round(len(pcm) / (2 * channels * rate), 2)
    elif asset.kind == "model":
        from uniview import meshimport
        try:
            model = meshimport.load(src)
        except Exception as e:
            raise ModError(f"Couldn't read that model: {e}") from e
        options["vertices"], options["triangles"] = len(model.points), model.triangle_count
        with session.lock:  # a trial run on the game's mesh: problems and notes show up now, not at install
            options["notes"] = _apply_model(asset.ref, model, save=False)
            skinned = bool(asset.ref.read_typetree().get("m_BoneNameHashes"))
        if skinned and src.lower().endswith(".obj"):
            options["notes"].insert(0, "This is a skinned (rigged) mesh. Blender writes OBJ files in the current pose, "
                                       "which may not line up with the skeleton - GLB is safer for characters.")
    elif asset.kind == "data":
        try:
            with open(src, encoding="utf-8-sig") as f:
                tree = json.load(f)
        except Exception as e:
            raise ModError(f"That isn't a valid JSON file: {e}") from e
        if not isinstance(tree, dict):
            raise ModError("The JSON file must hold an object ({ ... }) like the one UniView saves for this asset.")
        with session.lock:
            if _layout(session, asset) is None:
                raise ModError("This script's fields can't be fully decoded, so it can't be rewritten safely.")


def _layout(session, asset):
    """Field layout (TypeTreeNode) that reads a data asset completely, or None."""
    return session._scripts().layout(asset.ref)


# --------------------------------------------------------------------------- building modded files

def merge_tree(original, new):
    """`original` (typetree values as read) with the values of `new` (JSON as UniView saves it) put in,
    keeping each field's type. Fields `new` doesn't have keep their value; ones `original` doesn't have
    are ignored (the file's layout can't grow fields)."""
    if isinstance(original, dict):
        if not isinstance(new, dict):
            return original
        return {k: merge_tree(v, new[k]) if k in new else v for k, v in original.items()}
    if isinstance(original, list):
        if not isinstance(new, list):
            return original
        template = original[0] if original else None
        out = []
        for i, item in enumerate(new):
            base = original[i] if i < len(original) else template
            out.append(merge_tree(base, item) if base is not None else item)
        return out
    if isinstance(original, (bytes, bytearray, memoryview)):
        if isinstance(new, str):
            try:
                return bytes.fromhex(new)  # short byte arrays are saved as hex; long ones as "<N bytes>"
            except ValueError:
                pass
        return original
    if isinstance(original, bool):
        return bool(new) if isinstance(new, (bool, int)) else original
    if isinstance(original, int):
        try:
            return int(new)
        except (TypeError, ValueError):
            return original
    if isinstance(original, float):
        if new == "NaN":
            return math.nan
        try:
            return float(new)
        except (TypeError, ValueError):
            return original
    if isinstance(original, str):
        return new if isinstance(new, str) else original
    return original


def _find_object(env, inner, path_id):
    stack = list(env.files.values())
    while stack:
        f = stack.pop()
        objects = getattr(f, "objects", None)
        if objects is not None and getattr(f, "name", "") == inner:
            obj = objects.get(path_id)
            if obj is not None:
                return obj
        stack += list((getattr(f, "files", None) or {}).values())
    raise ModError(f"Object {path_id} wasn't found in {inner} (the game may have been updated - "
                   "replace the asset again).")


def _texture_image(edit, files_dir, tex):
    from PIL import Image
    img = Image.open(os.path.join(files_dir, edit["file"])).convert("RGBA")
    if edit["options"].get("keep_size"):
        return img
    if (img.width, img.height) != (tex.m_Width, tex.m_Height):
        img = img.resize((tex.m_Width, tex.m_Height), Image.Resampling.LANCZOS)
    return img


def _apply_texture(obj, edits, files_dir):
    """Write the texture edit and/or sprite pastes of one Texture2D."""
    from PIL import Image

    from engines.unity import asset_image
    tex = obj.parse_as_object()
    whole = next((e for e in edits if e["kind"] == "texture"), None)
    if whole is not None:
        img = _texture_image(whole, files_dir, tex)
    else:
        img = asset_image(tex).convert("RGBA")
    for edit in edits:
        if edit["kind"] != "sprite":
            continue
        x, y, w, h = edit["target"]["rect"]
        sw, sh = edit["target"].get("texture_size") or [img.width, img.height]
        sx, sy = img.width / sw, img.height / sh  # the sheet was replaced with a bigger/smaller one
        box_w, box_h = max(1, round(w * sx)), max(1, round(h * sy))
        piece = Image.open(os.path.join(files_dir, edit["file"])).convert("RGBA")
        piece = piece.resize((box_w, box_h), Image.Resampling.LANCZOS)
        left, top = round(x * sx), img.height - round(y * sy) - box_h
        img.paste(piece, (left, top))
    mips = int(getattr(tex, "m_MipCount", 1) or 1)
    try:
        tex.set_image(img, mipmap_count=mips)
    except Exception as e:  # a format UnityPy can't encode: store it uncompressed
        log.info("Mod: re-encoding %s as %s failed (%s) - writing RGBA32", tex.m_Name, tex.m_TextureFormat, e)
        tex.set_image(img, target_format=4, mipmap_count=mips)  # TextureFormat.RGBA32
    tex.save()


def _apply(obj, edit, files_dir, session):
    path = os.path.join(files_dir, edit["file"])
    kind = edit["kind"]
    if kind == "text":
        with open(path, "rb") as f:
            data = f.read()
        tree = obj.read_typetree()
        tree["m_Script"] = data.decode("utf-8", "surrogateescape")
        obj.save_typetree(tree)
    elif kind == "font":
        with open(path, "rb") as f:
            data = f.read()
        tree = obj.read_typetree()
        tree["m_FontData"] = data if isinstance(tree.get("m_FontData"), (bytes, bytearray)) else list(data)
        obj.save_typetree(tree)
    elif kind == "data":
        asset = next((a for a in session.assets if a.uid == edit["uid"]), None)
        if asset is None:
            raise ModError(f"'{edit['name']}' isn't in the loaded game any more.")
        with session.lock:
            node = _layout(session, asset)
        if node is None:
            raise ModError(f"'{edit['name']}': the script's fields can't be fully decoded.")
        with open(path, encoding="utf-8-sig") as f:
            new = json.load(f)
        tree = merge_tree(obj.read_typetree(nodes=node), new)
        obj.save_typetree(tree, nodes=node)
        return node
    else:
        raise ModError(f"Can't write {kind} assets.")


def _verify(data, name, layouts):
    """Load a written file back and read each changed object (catches broken writes before installing).
    layouts: (inner file, path_id) -> field layout to read it with (None: the built-in one)."""
    import UnityPy
    env = UnityPy.Environment()
    env.load_file(data, name=name)  # named like the game file: a plain .assets file's objects carry its name
    for (inner, path_id), node in layouts.items():
        obj = _find_object(env, inner, path_id)
        try:
            obj.read_typetree(nodes=node)
        except Exception as e:
            raise ModError(f"The modded {name} didn't read back correctly ({e}).") from e


def _is_bundle(f):
    from UnityPy.files import BundleFile, WebFile
    return isinstance(f, (BundleFile, WebFile))


class _Resources:
    """Sound data appended to .resource files while one game file is rebuilt: a separate .resource file next
    to a .assets file, or the .resource entry inside an AssetBundle."""

    def __init__(self, project, rel, top):
        self.project, self.rel, self.top = project, rel, top
        self.files = {}    # rel of a separate .resource file -> bytearray
        self.entries = {}  # bundle entry name -> bytearray

    def append(self, source, data):
        """Add data to the resource file a clip names (m_Source); returns its offset there."""
        name = source.replace("\\", "/").rsplit("/", 1)[-1]
        if _is_bundle(self.top):
            entry = self.top.files.get(name)
            if entry is None:
                raise ModError(f"{self.rel} has no '{name}' inside it for the sound data.")
            buf = self.entries.setdefault(name, bytearray(entry.bytes))
        else:
            res_rel = "/".join(filter(None, (os.path.dirname(self.rel), name)))
            if res_rel not in self.files:
                path = self.project.original(res_rel)
                if not os.path.isfile(path):
                    raise ModError(f"The sound file {res_rel} is missing.")
                with open(path, "rb") as f:
                    self.files[res_rel] = bytearray(f.read())
            buf = self.files[res_rel]
        buf += bytes(-len(buf) % RESOURCE_ALIGN)
        offset = len(buf)
        buf += data
        return offset

    def store_entries(self):
        from UnityPy.streams import EndianBinaryReader
        for name, buf in self.entries.items():
            old = self.top.files[name]
            new = EndianBinaryReader(bytes(buf))
            new.flags = getattr(old, "flags", 0)
            self.top.files[name] = new


def _gltf_files(path):
    """Relative paths of the external buffers a .gltf uses."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return [b["uri"] for b in doc.get("buffers", []) if b.get("uri") and not b["uri"].startswith("data:")]


def _apply_model(obj, model, save=True):
    """Put an ImportedMesh into a Mesh object. Returns notes for the user."""
    from UnityPy.helpers.ResourceReader import get_resource_data

    from uniview.unity_mesh_write import MeshWriteError, replace_mesh
    if obj.reader.endian != "<":
        raise ModError("Only PC (little-endian) game files can have meshes replaced.")
    tree = obj.read_typetree()
    stream = tree.get("m_StreamData") or {}
    if stream.get("path"):
        vertex_data = bytes(get_resource_data(stream["path"], obj.assets_file, stream["offset"], stream["size"]))
    else:
        vertex_data = bytes(tree["m_VertexData"].get("m_DataSize") or b"")
    try:
        notes = replace_mesh(tree, tuple(obj.version)[:2], model, vertex_data)
    except MeshWriteError as e:
        raise ModError(str(e)) from e
    if save:
        obj.save_typetree(tree)
    return notes


def _apply_audio(obj, edit, files_dir, resources):
    from uniview import fsb
    pcm, channels, rate = fsb.decode(os.path.join(files_dir, edit["file"]))
    bank = fsb.fsb5_pcm16(pcm, channels, rate)
    tree = obj.read_typetree()
    res = tree["m_Resource"]
    res["m_Offset"] = resources.append(res["m_Source"], bank)
    res["m_Size"] = len(bank)
    tree.update(m_Channels=channels, m_Frequency=rate, m_BitsPerSample=16, m_SubsoundIndex=0,
                m_Length=len(pcm) / (2 * channels * rate), m_CompressionFormat=0)  # 0 = PCM
    obj.save_typetree(tree)


def build_file(project, session, rel, out_dir):
    """Write the modded copy of one game file (plus the .resource file its new sounds went into) under
    out_dir. Returns the rel paths written."""
    import UnityPy
    edits = [e for e in project.edits if e["target"]["file"] == rel]
    src = project.original(rel)
    if not os.path.isfile(src):
        raise ModError(f"The game file {rel} is missing.")
    game_file = os.path.join(project.game_dir, rel)
    env = UnityPy.Environment(path=os.path.dirname(game_file))  # .resS/.resource next to the game's file
    with open(src, "rb") as stream:
        top = env.load_file(stream, name=game_file)
        if top is None:
            raise ModError(f"UnityPy couldn't read {rel}.")
        by_object, layouts = {}, {}
        resources = _Resources(project, rel, top)
        for edit in edits:
            t = edit["target"]
            by_object.setdefault((t["inner"], t["path_id"]), []).append(edit)
        for (inner, path_id), group in by_object.items():
            obj = _find_object(env, inner, path_id)
            try:
                if any(e["kind"] in ("texture", "sprite") for e in group):
                    _apply_texture(obj, group, project.files_dir)
                    layouts[inner, path_id] = None
                elif group[-1]["kind"] == "audio":
                    _apply_audio(obj, group[-1], project.files_dir, resources)
                    layouts[inner, path_id] = None
                elif group[-1]["kind"] == "model":
                    from uniview import meshimport
                    for note in _apply_model(obj, meshimport.load(os.path.join(project.files_dir, group[-1]["file"]))):
                        log.info("Mod: '%s': %s", group[-1]["name"], note)
                    layouts[inner, path_id] = None
                else:
                    layouts[inner, path_id] = _apply(obj, group[-1], project.files_dir, session)
            except ModError:
                raise
            except struct.error as e:
                raise ModError(f"'{group[0]['name']}': a value doesn't fit its field (e.g. a number too big "
                               f"for a byte field, or a negative number in an unsigned one): {e}") from e
            except Exception as e:
                log.exception("Mod: writing '%s' failed", group[0]["name"])
                raise ModError(f"Couldn't write '{group[0]['name']}': {e}") from e
        resources.store_entries()
        if _is_bundle(top):
            try:
                data = top.save(packer="original")
            except Exception as e:
                log.info("Mod: re-packing %s with its own compression failed (%s) - using LZ4", rel, e)
                data = top.save(packer="lz4")
        else:
            data = top.save()
    _verify(data, os.path.basename(rel), layouts)
    outputs = {rel: data, **resources.files}
    for out_rel, out_data in outputs.items():
        out_path = os.path.join(out_dir, out_rel)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(out_data)
        log.info("Mod: wrote %s (%.1f MB)", out_rel, len(out_data) / 1e6)
    return list(outputs)


def build(project, session, out_dir, progress=None, cancelled=None):
    """Write every changed game file under out_dir (same folders as in the game). Returns the rel paths."""
    if not project.edits:
        raise ModError("The mod has no replacements yet: right-click an asset → Replace with file...")
    files = project.changed_files()
    done = []
    for n, rel in enumerate(files):
        if cancelled is not None and cancelled():
            break
        if progress is not None:
            progress(n, len(files), rel)
        for written in build_file(project, session, rel, out_dir):
            if written in done:
                raise ModError(f"{written} would be written twice (two game files share it) - "
                               "Mod Maker can't combine those yet.")
            done.append(written)
    done += patch_catalogs(project, done, out_dir)
    if progress is not None:
        progress(len(files), len(files), "")
    return done


def patch_catalogs(project, written, out_dir):
    """Switch off the Addressables CRC check of the modded bundles in `written`: writes the patched catalog(s)
    under out_dir and returns their rel paths."""
    from uniview import addressables
    by_aa = {}
    for rel in written:
        aa = addressables.aa_folder(rel)
        if aa is not None:
            by_aa.setdefault(aa, []).append(rel)
    out = []
    for aa, bundles in by_aa.items():
        for name in addressables.catalog_files(os.path.join(project.game_dir, aa)):
            rel = f"{aa}/{name}"
            with open(project.original(rel), "rb") as f:
                text = f.read().decode("utf-8")
            try:
                new, patched = addressables.patch_catalog(text, aa, bundles)
            except Exception as e:
                log.warning("Mod: couldn't patch the Addressables catalog %s: %s", rel, e)
                continue
            if patched:
                path = os.path.join(out_dir, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(new.encode("utf-8"))
                out.append(rel)
                log.info("Mod: %s - CRC check switched off for %d modded bundle(s)", rel, len(patched))
    return out


def warnings_for(project):
    """Things to tell the user about the files this mod changes."""
    from uniview import addressables
    notes = []
    for aa in sorted({a for a in map(addressables.aa_folder, project.changed_files()) if a}):
        folder = os.path.join(project.game_dir, aa)
        if not addressables.catalog_files(folder):
            notes.append(f"{aa} has no catalog.json (a binary catalog or one inside a bundle): if the game checks "
                         "the CRC of its bundles, it will refuse the modded ones.")
        try:
            with open(os.path.join(folder, "settings.json"), encoding="utf-8") as f:
                online = "http" in json.dumps(json.load(f).get("m_CatalogLocations"))
        except (OSError, ValueError):
            online = False
        if online:
            notes.append("This game can download an updated Addressables catalog online; if it does, that catalog "
                         "can switch the CRC check of the modded bundles back on.")
    return notes


# --------------------------------------------------------------------------- export / install / restore

README = """{name}
{line}
Made with UniView's Mod Maker on {date}.

INSTALL: copy everything in this folder (except this file) into the game folder:
    {game}
and let it replace the files. Back up the originals first (or use Steam's "Verify integrity of game
files" to undo the mod). A game update replaces these files again.

CHANGES
{changes}
"""


def export(project, session, out_dir, progress=None, cancelled=None):
    """A shareable mod folder: the modded game files + README. Returns the rel paths written."""
    written = build(project, session, out_dir, progress, cancelled)
    changes = "\n".join(f"  - {e['kind']:8} {e['name']}  ({e['target']['file']})  <- {e['original_name']}"
                        for e in project.edits)
    with open(os.path.join(out_dir, "README - UniView mod.txt"), "w", encoding="utf-8") as f:
        f.write(README.format(name=project.name, line="=" * len(project.name), date=time.strftime("%Y-%m-%d"),
                              game=project.game_dir, changes=changes))
    return written


def stage(project, session, progress=None, cancelled=None):
    """Build the modded files into the project's build folder (do this while the game is still loaded:
    data assets need its script layouts). Returns the rel paths staged."""
    staging = os.path.join(project.dir, "build")
    shutil.rmtree(staging, ignore_errors=True)
    return build(project, session, staging, progress, cancelled)


def install_staged(project, staged):
    """Copy the staged files into the game (the game must be closed in UniView: its files are read lazily).
    Originals go to the backup folder first; installed files the mod no longer changes are restored."""
    staging = os.path.join(project.dir, "build")
    for rel in staged:
        game = os.path.join(project.game_dir, rel)
        backup = os.path.join(project.backup_dir, rel)
        if rel not in project.installed:
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(game, backup)
    for rel in staged:
        game = os.path.join(project.game_dir, rel)
        try:
            shutil.copyfile(os.path.join(staging, rel), game)
        except PermissionError as e:
            raise ModError(f"Couldn't write {rel} - is the game running?\n{e}") from e
        project.installed[rel] = _stat(game)
        project.save()
        log.info("Mod: installed %s", rel)
    for rel in [r for r in project.installed if r not in staged]:
        restore_file(project, rel)
    shutil.rmtree(staging, ignore_errors=True)


def restore_file(project, rel):
    game = os.path.join(project.game_dir, rel)
    backup = os.path.join(project.backup_dir, rel)
    expected = project.installed.pop(rel, None)
    try:
        if os.path.isfile(backup):
            if os.path.isfile(game) and expected is not None and _stat(game) != expected:
                log.info("Mod: %s was updated by the game since install - keeping the game's file", rel)
            else:
                shutil.copyfile(backup, game)
                log.info("Mod: restored %s", rel)
            os.remove(backup)
    finally:
        project.save()


def restore(project):
    """Put the game's original files back."""
    for rel in list(project.installed):
        restore_file(project, rel)
