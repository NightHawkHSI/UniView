"""Loose files & archives: a fallback engine for any game folder.

Lists images (png/jpg/tga/dds/bmp...), sounds, text files and simple models (.obj, DirectX .x)
lying loose in the folder or inside zip-style archives (.zip, .gro, .pk3, .pk4, zip-based .pak).
It only wins when no real engine plugin recognizes a game, so games without a dedicated plugin
can still be browsed (e.g. Project Zomboid, Serious Sam 2).
"""

import logging
import os
import re
import zipfile

import numpy as np

from .sdk import (
    ALBEDO, Asset, EnginePlugin, GameSession, Material, MeshData, TextureRef, kind_for_extension,
    pil_image_from_bytes,
)

log = logging.getLogger("viewer.generic")

ARCHIVE_EXTS = (".zip", ".gro", ".pk3", ".pk4", ".pak")
MODEL_EXTS = {"obj", "x"}
SKIP_DIRS = {"__pycache__", ".git", "bin", "binaries", "redist", "_commonredist", "directx", "vcredist"}
SKIP_EXTS = {"exe", "dll", "pdb", "so", "dylib", "lib", "sys", "msi", "cab", "ini_", "log"}


def _is_zip(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


# --------------------------------------------------------------------------- models

def parse_obj(text):
    points, uvs, face_uvs, groups, material, mtls = [], [], {}, {}, "", []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            points.append([float(x) for x in parts[1:4]])
        elif parts[0] == "vt" and len(parts) >= 3:
            uvs.append([float(x) for x in parts[1:3]])
        elif parts[0] == "usemtl":
            material = parts[1] if len(parts) > 1 else ""
        elif parts[0] == "mtllib" and len(parts) > 1:
            mtls.append(" ".join(parts[1:]))
        elif parts[0] == "f":
            corners = [p.split("/") for p in parts[1:]]
            ids = [int(c[0]) - 1 if int(c[0]) > 0 else len(points) + int(c[0]) for c in corners]
            for c, vid in zip(corners, ids):
                if len(c) > 1 and c[1]:
                    face_uvs[vid] = int(c[1]) - 1 if int(c[1]) > 0 else len(uvs) + int(c[1])
            for i in range(1, len(ids) - 1):
                groups.setdefault(material, []).append([ids[0], ids[i], ids[i + 1]])
    uv0 = None
    if uvs and face_uvs:
        uv0 = np.zeros((len(points), 2), np.float32)
        for vid, tid in face_uvs.items():
            if 0 <= vid < len(points) and 0 <= tid < len(uvs):
                uv0[vid] = uvs[tid]
    names = list(groups)
    return (np.array(points, np.float32), [np.array(groups[n]) for n in names], uv0, names, mtls)


_NUM = re.compile(r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


def parse_x(text):
    """DirectX .x (text): every Mesh block's vertices, faces, UVs and texture names."""
    points_all, uvs_all, subs, textures = [], [], [], []
    base = 0
    for m in re.finditer(r"\bMesh\b[^{]*\{", text):
        nums = _NUM.finditer(text, m.end())

        def take(n, cast=float):
            return [cast(next(nums).group()) for _ in range(n)]

        try:
            n_verts = int(next(nums).group())
            verts = np.array(take(n_verts * 3), np.float32).reshape(-1, 3)
            n_faces = int(next(nums).group())
            faces = []
            for _ in range(n_faces):
                k = int(next(nums).group())
                idx = take(k, int)
                faces += [[idx[0], idx[i], idx[i + 1]] for i in range(1, k - 1)]
        except (StopIteration, ValueError):
            continue
        end = text.find("\nMesh ", m.end())
        block = text[m.end(): end if end > 0 else len(text)]
        uv = np.zeros((n_verts, 2), np.float32)
        tc = re.search(r"MeshTextureCoords[^{]*\{", block)
        if tc:
            tnums = _NUM.findall(block, tc.end())
            try:
                count = int(tnums[0])
                if count == n_verts:
                    uv = np.array([float(x) for x in tnums[1:1 + count * 2]], np.float32).reshape(-1, 2)
            except (IndexError, ValueError):
                pass
        tex = re.search(r'TextureFilename[^{]*\{\s*"([^"]+)"', block)
        textures.append(tex.group(1) if tex else "")
        points_all.append(verts)
        uvs_all.append(uv)
        subs.append(np.array(faces, np.int64).reshape(-1, 3) + base)
        base += n_verts
    if not points_all:
        raise ValueError("No mesh in this .x file (it may only hold a skeleton or an animation).")
    return np.concatenate(points_all), subs, np.concatenate(uvs_all), textures


# --------------------------------------------------------------------------- session

class GenericSession(GameSession):
    def __init__(self, plugin, path):
        super().__init__(plugin, path)
        self.archives = {}
        self.by_name = {}   # lowercase file name -> [texture Asset] (to find a model's textures)

    def close(self):
        for z in self.archives.values():
            z.close()

    def raw(self, asset):
        kind, where, member = asset.ref
        if kind == "zip":
            return self.archives[where].read(member)
        with open(where, "rb") as f:
            return f.read()

    def image(self, asset):
        return pil_image_from_bytes(self.raw(asset))

    def text(self, asset):
        data = self.raw(asset)
        for enc in ("utf-8", "utf-16"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("latin-1")

    def stats(self, asset):
        stats = {"size": asset.size, "info": "", "sort": asset.size or 0}
        if asset.kind == "texture":
            import io
            from PIL import Image
            with Image.open(io.BytesIO(self.raw(asset))) as img:
                w, h = img.size
            stats.update(w=w, h=h, info=f"{w}×{h}", sort=w * h)
        elif asset.kind == "model":
            md = self.mesh(asset)
            stats.update(tris=md.triangle_count, verts=len(md.points), info=f"{md.triangle_count:,} tris",
                         sort=md.triangle_count)
        return stats

    def _texture_named(self, name, near):
        base = os.path.basename(name.replace("\\", "/")).lower()
        candidates = self.by_name.get(base) or self.by_name.get(os.path.splitext(base)[0] + ".png") or []
        if not candidates:
            return None
        near = near.lower()
        return max(candidates, key=lambda a: len(os.path.commonprefix([a.path.lower(), near])))

    def mesh(self, asset):
        text = self.text(asset)
        if asset.ext == "x":
            points, subs, uv, _textures = parse_x(text)
            points = points * np.array([1, 1, -1], np.float32)  # .x is left-handed
            subs = [s[:, ::-1] for s in subs]
            uv = uv.copy()
            uv[:, 1] = 1.0 - uv[:, 1]
            return MeshData(points, subs, uvs={"UV0": uv}, name=asset.name)
        points, subs, uv, _names, _mtls = parse_obj(text)
        return MeshData(points, subs, uvs={"UV0": uv} if uv is not None else None, name=asset.name)

    def materials(self, asset):
        text = self.text(asset)
        out = []
        if asset.ext == "x":
            _p, _s, _uv, textures = parse_x(text)
            for name in textures:
                tex = self._texture_named(name, asset.path) if name else None
                out.append(Material(os.path.basename(name) or "(no texture)",
                                    [TextureRef("texture", tex.name, tex, ALBEDO)] if tex else []))
            return out
        _p, _s, _uv, names, _mtls = parse_obj(text)
        for name in names:
            tex = self._texture_named(name, asset.path) if name else None
            out.append(Material(name or "(default)", [TextureRef("texture", tex.name, tex, ALBEDO)] if tex else []))
        return out


class GenericPlugin(EnginePlugin):
    id = "generic"
    name = "Loose files & archives"
    version = "1.0"
    author = "UniView"
    description = ("Fallback for any game: images (png/tga/dds...), sounds, text files and .obj/.x models "
                   "lying loose in the folder or inside zip-style archives (.zip/.gro/.pk3).")

    def detect(self, path):
        if os.path.isfile(path):
            return 3 if path.lower().endswith(ARCHIVE_EXTS) and _is_zip(path) else 0
        return 3 if os.path.isdir(path) else 0

    def game_info(self, path):
        return {"engine_version": "", "detail": "loose files"}

    def label(self, info):
        return "Loose files"

    def count_files(self, path):
        return sum(len(files) for _root, _dirs, files in os.walk(path)) if os.path.isdir(path) else 1

    def open(self, path, progress):
        session = GenericSession(self, path)
        root = path if os.path.isdir(path) else os.path.dirname(path)

        def add(rel, ref, size, source):
            ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
            if ext in SKIP_EXTS:
                return
            kind = "model" if ext in MODEL_EXTS else kind_for_extension(ext)
            name = rel.rsplit(".", 1)[0] if kind in ("model", "texture") else rel
            key = f"{source}|{rel}".lower()
            asset = Asset(kind, name, key, uid=key, size=size, path=rel, source=source, ref=ref, ext=ext)
            session.assets.append(asset)
            if kind == "texture":
                session.by_name.setdefault(rel.rsplit("/", 1)[-1].lower(), []).append(asset)

        files = [path] if os.path.isfile(path) else []
        if not files:
            for dirpath, dirs, names in os.walk(path):
                dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
                files += [os.path.join(dirpath, n) for n in names]
        for n, full in enumerate(files):
            if n % 2000 == 0:
                progress(f"Listing files ... {n:,}/{len(files):,}", n, len(files))
            rel = os.path.relpath(full, root).replace("\\", "/")
            if full.lower().endswith(ARCHIVE_EXTS) and _is_zip(full):
                try:
                    z = zipfile.ZipFile(full)
                except (zipfile.BadZipFile, OSError) as e:
                    log.warning("Couldn't open %s: %s", rel, e)
                    continue
                session.archives[full] = z
                for info in z.infolist():
                    if not info.is_dir():
                        add(info.filename, ("zip", full, info.filename), info.file_size, os.path.basename(full))
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            add(rel, ("file", full, None), size, "")
        session.file_count = len(files)
        if not session.assets:
            raise FileNotFoundError(f"No files UniView can show were found in:\n{path}")
        return session


PLUGIN = GenericPlugin()
