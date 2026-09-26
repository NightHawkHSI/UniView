"""Unity engine plugin (UnityPy): meshes, Texture2D, Sprites and TextAssets from any Unity game."""

import logging
import os
import re
import threading
import time
from contextlib import contextmanager

import numpy as np

from .sdk import (
    ALBEDO, NORMAL, OTHER, Asset, EnginePlugin, GameSession, Material, MeshData, TextureRef,
)

log = logging.getLogger("viewer.unity")

TYPE_KINDS = {"Mesh": "model", "Texture2D": "texture", "Sprite": "sprite", "TextAsset": "text", "AudioClip": "audio",
              "AnimationClip": "animation"}
ALBEDO_PROPS = ("_MainTex", "_BaseMap", "_BaseColorMap", "_Albedo", "_BaseColorTexture")
NORMAL_PROPS = ("_BumpMap", "_NormalMap")

BUNDLE_MAGICS = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
RESOURCE_EXTS = (".ress", ".resource")
SKIP_EXTS = (".exe", ".dll", ".so", ".pdb", ".mdb", ".txt", ".log", ".json", ".xml",
             ".config", ".ini", ".cfg", ".png", ".jpg", ".bat", ".sys", ".manifest",
             ".vpk", ".pak", ".utoc", ".ucas", ".sig")
DATA_MARKERS = ("globalgamemanagers", "mainData", "data.unity3d", "level0")
SNIFF_EXTS = ("", ".assets", ".unity3d", ".bundle", ".ab", ".asset", ".assetbundle")  # detect() on loose folders
UNITY_VERSION_RE = re.compile(rb"\d{1,4}\.\d+\.\d+[a-zA-Z]?\d*")


# --------------------------------------------------------------------------- finding files

def looks_like_unity_file(path):
    """Sniff a file's header to decide whether UnityPy can load it."""
    lower = path.lower()
    if lower.endswith(RESOURCE_EXTS) or ".split" in os.path.basename(lower):
        return True  # streamed texture/mesh data referenced by .assets files
    if lower.endswith(SKIP_EXTS):
        return False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(48)
    except OSError:
        return False
    if head.startswith(BUNDLE_MAGICS):
        return True
    # Serialized file (.assets, level0, globalgamemanagers...): no magic, so check
    # that the big-endian header's version and recorded file size are sane.
    if len(head) < 48:
        return False
    version = int.from_bytes(head[8:12], "big")
    if not 5 <= version <= 50:
        return False
    if version < 22:
        return int.from_bytes(head[4:8], "big") == size
    return int.from_bytes(head[24:32], "big") == size


def find_unity_files(root, limit=None):
    if os.path.isfile(root):
        return [root] if looks_like_unity_file(root) else []
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if looks_like_unity_file(path):
                found.append(path)
                if limit and len(found) >= limit:
                    return found
    return sorted(found)


def unity_data_dir(game_dir):
    """Return the game's <Name>_Data folder if this looks like a Unity game, else None."""
    try:
        entries = os.listdir(game_dir)
    except OSError:
        return None
    for name in entries:
        path = os.path.join(game_dir, name)
        if name.endswith("_Data") and os.path.isdir(path):
            if any(os.path.exists(os.path.join(path, f)) for f in DATA_MARKERS):
                return path
    return None


def read_unity_version(path):
    """Unity editor version stored in a serialized file's or bundle's header (e.g. '2019.4.40f1')."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if head.startswith(BUNDLE_MAGICS):
        # magic\0, uint32 format, player version\0, engine version\0
        strings = head[head.index(b"\0") + 5:].split(b"\0")
        candidates = strings[1:2] + strings[:1]
    else:
        if len(head) < 48:
            return None
        version = int.from_bytes(head[8:12], "big")
        if version < 9:
            return None  # older files keep the version at the end of the file
        candidates = [head[48 if version >= 22 else 20:].split(b"\0")[0]]
    for text in candidates:
        if UNITY_VERSION_RE.fullmatch(text) and not text.startswith(b"0.0"):
            return text.decode()
    return None


def detect_unity_info(game_dir):
    """{'engine_version': '2019.4.40f1' or '', 'detail': 'IL2CPP' / 'Mono' / ''} without loading the game."""
    data = unity_data_dir(game_dir) if os.path.isdir(game_dir) else None
    version = None
    for name in DATA_MARKERS if data else ():
        version = read_unity_version(os.path.join(data, name))
        if version:
            break
    if not version:
        # Loose asset folder or a single file: try the first few Unity files found.
        for path in find_unity_files(game_dir, limit=50):
            version = read_unity_version(path)
            if version:
                break
    backend = ""
    if os.path.isdir(game_dir) and (os.path.isfile(os.path.join(game_dir, "GameAssembly.dll")) or (
            data and os.path.isdir(os.path.join(data, "il2cpp_data")))):
        backend = "IL2CPP"
    elif data and os.path.isdir(os.path.join(data, "Managed")):
        backend = "Mono"
    return {"engine_version": version or "", "detail": backend}


def version_key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def unity_branch(version):
    """'2019.4.40f1' -> 'Unity 2019.4', '6000.0.58f2' -> 'Unity 6.0'."""
    nums = version_key(version)
    if len(nums) < 2:
        return "Unity (unknown version)"
    major = nums[0] // 1000 if nums[0] >= 6000 else nums[0]
    return f"Unity {major}.{nums[1]}"


# --------------------------------------------------------------------------- mesh conversion

SURFACE_TOPOLOGIES = (0, 1, 2)  # Triangles, TriangleStrip, Quads (not Lines/LineStrip/Points)


@contextmanager
def surface_submeshes(mesh):
    """Temporarily hide line/point submeshes, which UnityPy can't turn into triangles."""
    original = mesh.m_SubMeshes
    surfaces = [sm for sm in original or [] if int(sm.topology) in SURFACE_TOPOLOGIES]
    if original and not surfaces:
        raise ValueError("Mesh only has lines/points (no surfaces to show).")
    mesh.m_SubMeshes = surfaces
    try:
        yield len(original or []) - len(surfaces)
    finally:
        mesh.m_SubMeshes = original


def triangle_count(submeshes):
    tris = 0
    for sm in submeshes or []:
        topology, count = int(sm.topology), sm.indexCount
        if topology == 0:
            tris += count // 3
        elif topology == 1:
            tris += max(count - 2, 0)
        elif topology == 2:
            tris += count // 2
    return tris


def mesh_to_meshdata(mesh):
    """UnityPy Mesh -> MeshData (x flipped: Unity is left-handed, so winding flips too)."""
    from UnityPy.helpers.MeshHelper import MeshHandler
    handler = MeshHandler(mesh)
    handler.process()
    if not handler.m_VertexCount or not handler.m_Vertices:
        raise ValueError("Mesh has no vertices (it may be stripped or read/write disabled).")
    points = np.asarray(handler.m_Vertices, dtype=np.float32)[:, :3].copy()
    points[:, 0] *= -1
    count = len(points)
    keep = [i for i, sm in enumerate(mesh.m_SubMeshes or []) if int(sm.topology) in SURFACE_TOPOLOGIES]
    with surface_submeshes(mesh) as skipped:
        triangles = handler.get_triangles()
    submeshes, slots = [], []
    for j, sub in enumerate(triangles):
        tris = [t for t in sub if len(t) == 3]
        if tris:
            submeshes.append(np.asarray(tris, dtype=np.int64)[:, ::-1])
            slots.append(keep[j] if j < len(keep) else j)
    if not submeshes:
        raise ValueError("Mesh has no triangles.")
    normals = None
    if handler.m_Normals and len(handler.m_Normals) == count:
        normals = np.asarray(handler.m_Normals, dtype=np.float32)[:, :3].copy()
        normals[:, 0] *= -1
    uvs = {}
    for i in range(8):
        uv = getattr(handler, f"m_UV{i}", None)
        if uv and len(uv) == count:
            uvs[f"UV{i}"] = np.asarray(uv, dtype=np.float32)[:, :2]
    colors = None
    if handler.m_Colors and len(handler.m_Colors) == count:
        colors = np.asarray(handler.m_Colors, dtype=np.float32)[:, :4]
        if colors.max() > 1.0:
            colors = colors / 255.0
    return MeshData(points, submeshes, normals=normals, uvs=uvs, colors=colors, material_slots=slots,
                    name=mesh.m_Name or "", skipped=skipped)


def asset_image(asset):
    """PIL image of a Texture2D/Sprite, with a clear error for textures that have no pixels.

    Some textures (e.g. "Font Texture") are generated while the game runs, so the files only
    hold an empty 0x0 placeholder; UnityPy would otherwise fail with a confusing file error.
    """
    width = getattr(asset, "m_Width", None)
    stream = getattr(asset, "m_StreamData", None)
    if width is not None and (width == 0 or asset.m_Height == 0 or (
            not getattr(asset, "image_data", None) and (stream is None or not stream.path))):
        raise ValueError("Empty texture - it's created while the game runs (e.g. a font), "
                         "so there are no pixels saved in the game files.")
    return asset.image


def obj_key(assets_file, path_id):
    return (id(assets_file), path_id)


# --------------------------------------------------------------------------- material index

class TextureFinder:
    """Finds what uses a mesh and which materials/textures it has (renderer -> material).

    Big games have a million+ MeshFilters/MeshRenderers, so the index only reads the first
    pointers of each component (its GameObject, and a MeshFilter's mesh) straight from the
    file bytes; materials are read on demand. The index is built in the background right after
    a game loads, a chunk at a time under the session lock, and whoever needs it first finishes it.
    """

    CHUNK = 4000      # objects handled per lock hold (keeps the UI responsive)
    MAX_USERS = 40    # GameObject names resolved per model

    def __init__(self, env, lock):
        self.env = env
        self.lock = lock
        self._steps = None
        self._indexed = False   # mesh -> users index done
        self._finished = False  # texture -> models index done too (or gave up)
        self._mesh_users = {}   # mesh key -> [(assets file, GameObject path id)]
        self._renderers = {}    # GameObject key -> MeshRenderer ObjectReader
        self._skinned = {}      # mesh key -> [SkinnedMeshRenderer ObjectReader]
        self._files = {}        # (id(assets file), file id) -> target assets file or None
        self._tex_users = {}    # texture key -> {mesh keys}

    # ---- building
    def start_background(self):
        def work():
            while not self._finished:
                with self.lock:
                    self._step()

        threading.Thread(target=work, daemon=True, name="mesh-index").start()

    def stop(self):
        """Give up on the background indexing (the game is being unloaded)."""
        self._finished = True

    @property
    def indexed(self):
        return self._indexed or self._finished

    def _run_until(self, done):
        with self.lock:
            while not done() and not self._finished:
                self._step()

    def _step(self):
        """Advance the index by one chunk. Callers hold the lock."""
        if self._finished:
            return
        if self._steps is None:
            self._steps = self._build_steps()
        try:
            next(self._steps)
        except StopIteration:
            self._indexed = self._finished = True
        except Exception:
            log.exception("Indexing which models use which materials failed")
            self._indexed = self._finished = True

    def _build_steps(self):
        log.info("Indexing which objects use which meshes/materials...")
        started = time.time()
        for n, obj in enumerate(self.env.objects, 1):
            if n % self.CHUNK == 0:
                yield
            tname = obj.type.name
            try:
                if tname == "MeshFilter":
                    ptrs = self._read_pptrs(obj, 2)
                    if ptrs and ptrs[1][1]:
                        go, mesh = ptrs
                        self._mesh_users.setdefault(obj_key(*mesh), []).append(go)
                elif tname == "MeshRenderer":
                    ptrs = self._read_pptrs(obj, 1)
                    if ptrs:
                        self._renderers[obj_key(*ptrs[0])] = obj
                elif tname == "SkinnedMeshRenderer":
                    mesh_ptr = obj.read().m_Mesh
                    if mesh_ptr.path_id:
                        mesh = mesh_ptr.deref()
                        self._skinned.setdefault(obj_key(mesh.assets_file, mesh.path_id), []).append(obj)
            except Exception:
                continue
        self._indexed = True
        log.info("Indexed %d meshes with renderers in %.1fs",
                 len(set(self._mesh_users) | set(self._skinned)), time.time() - started)
        yield

        # Second pass for the texture view's "used by": one renderer's materials per model.
        started = time.time()
        material_textures = {}
        for n, mesh_key in enumerate(set(self._mesh_users) | set(self._skinned), 1):
            if n % 500 == 0:
                yield
            for ptr in self._material_ptrs(mesh_key, first_only=True):
                try:
                    mat_key = (id(ptr.assetsfile), ptr.path_id)
                except Exception:
                    continue
                if mat_key not in material_textures:
                    material_textures[mat_key] = self._texture_keys(ptr)
                for tex_key in material_textures[mat_key]:
                    self._tex_users.setdefault(tex_key, set()).add(mesh_key)
        log.info("Indexed which models use which textures (%d textures) in %.1fs",
                 len(self._tex_users), time.time() - started)

    def _read_pptrs(self, obj, count):
        """Read the first `count` PPtrs of a component without parsing it: [(assets file, path id)]."""
        assets_file = obj.assets_file
        wide = assets_file.header.version >= 14
        reader = obj.reader
        obj.reset()
        out = []
        for _ in range(count):
            file_id = reader.read_int()
            path_id = reader.read_long() if wide else reader.read_int()
            target = self._file(assets_file, file_id)
            if target is None:
                return None
            out.append((target, path_id))
        return out

    def _file(self, assets_file, file_id):
        """Assets file a PPtr's file id points to (same lookup as UnityPy's PPtr.deref)."""
        key = (id(assets_file), file_id)
        if key not in self._files:
            target = assets_file if file_id == 0 else None
            if 0 < file_id <= len(assets_file.externals):
                name = assets_file.externals[file_id - 1].path
                name = (name[9:] if name.startswith("archive:/") else name).rsplit("/", 1)[-1].lower()
                container = assets_file.parent
                if container is not None:
                    target = next((f for k, f in container.files.items() if k.lower() == name), None)
                if target is None:
                    try:
                        target = assets_file.environment.find_file(name)
                    except Exception:
                        target = None
            self._files[key] = target
        return self._files[key]

    # ---- lookups
    def _material_ptrs(self, mesh_key, first_only=False):
        for assets_file, go_id in self._mesh_users.get(mesh_key, []):
            renderer = self._renderers.get(obj_key(assets_file, go_id))
            if renderer is not None:
                try:
                    return list(renderer.read().m_Materials)
                except Exception:
                    if first_only:
                        break
        for renderer in self._skinned.get(mesh_key, []):
            try:
                return list(renderer.read().m_Materials)
            except Exception:
                continue
        return []

    @staticmethod
    def _object_name(reader):
        try:
            return reader.peek_name() or reader.read().m_Name
        except Exception:
            return "?"

    def info(self, mesh_obj):
        """Return (gameobject names, materials, number of users).

        names: up to MAX_USERS GameObjects using the mesh.
        materials: [{"name": str, "textures": [(property, texture name, ObjectReader)]}],
        one per submesh slot, with albedo textures first.
        """
        self._run_until(lambda: self._indexed)
        with self.lock:
            key = obj_key(mesh_obj.assets_file, mesh_obj.path_id)
            gos = self._mesh_users.get(key, [])
            skinned = self._skinned.get(key, [])
            names = []
            for assets_file, go_id in gos[:self.MAX_USERS]:
                reader = assets_file.objects.get(go_id)
                if reader is not None:
                    names.append(self._object_name(reader))
            for renderer in skinned[:max(0, self.MAX_USERS - len(names))]:
                try:
                    names.append(self._object_name(renderer.read().m_GameObject.deref()))
                except Exception:
                    pass
            materials = []
            for ptr in self._material_ptrs(key):
                try:
                    mat = ptr.deref_parse_as_object()
                    tex_envs = mat.m_SavedProperties.m_TexEnvs
                except Exception:
                    continue
                textures = []
                for prop, tex_env in tex_envs:
                    prop = getattr(prop, "name", prop)
                    tptr = tex_env.m_Texture
                    if tptr.path_id == 0:
                        continue
                    try:
                        reader = tptr.deref()
                        if reader.type.name != "Texture2D":
                            continue
                        textures.append((prop, reader.peek_name() or prop, reader))
                    except Exception:
                        continue
                textures.sort(key=lambda t: t[0] not in ALBEDO_PROPS)
                materials.append({"name": mat.m_Name, "textures": textures})
            return names, materials, len(gos) + len(skinned)

    def texture_users(self, tex_obj):
        """Keys of the meshes whose materials use this texture."""
        self._run_until(lambda: False)
        return self._tex_users.get(obj_key(tex_obj.assets_file, tex_obj.path_id), set())

    @staticmethod
    def _texture_keys(mat_ptr):
        try:
            tex_envs = mat_ptr.deref_parse_as_object().m_SavedProperties.m_TexEnvs
        except Exception:
            return ()
        keys = []
        for _prop, tex_env in tex_envs:
            ptr = tex_env.m_Texture
            if ptr.path_id:
                try:
                    reader = ptr.deref()
                    keys.append(obj_key(reader.assets_file, reader.path_id))
                except Exception:
                    pass
        return keys


# --------------------------------------------------------------------------- session

class UnitySession(GameSession):
    def __init__(self, plugin, path, env, file_count):
        super().__init__(plugin, path)
        self.env = env
        self.file_count = file_count
        self.finder = TextureFinder(env, self.lock)
        self.by_key = {}
        self._paths = None
        self._transforms = None
        self._hierarchy_done = False
        self._skins = {}

    def _asset_for(self, obj, type_name="Texture2D", name=None):
        """Asset for an ObjectReader (the listed one if we have it)."""
        key = obj_key(obj.assets_file, obj.path_id)
        asset = self.by_key.get(key)
        if asset is None:
            asset = Asset(TYPE_KINDS.get(type_name, "file"), name or obj.peek_name() or type_name, key,
                          uid=f"{obj.assets_file.name}:{obj.path_id}", size=obj.byte_size,
                          path=getattr(obj, "container", None) or "", source=obj.assets_file.name, ref=obj)
        return asset

    def start_background(self):
        self.finder.start_background()

    def close(self):
        self.finder.stop()

    def image(self, asset):
        return asset_image(asset.ref.read())

    def mesh(self, asset):
        return mesh_to_meshdata(asset.ref.read())

    def raw(self, asset):
        if asset.kind == "animation":
            import json
            return json.dumps(self._clip(asset), indent=1).encode("utf-8")
        script = asset.ref.read().m_Script
        return script.encode("utf-8", "surrogateescape") if isinstance(script, str) else bytes(script)

    def text(self, asset):
        if asset.kind == "animation":
            from .unity_anim import summary_text
            return summary_text(self._clip(asset))
        script = asset.ref.read().m_Script
        return script.decode("utf-8", "replace") if isinstance(script, bytes) else script

    def _path_names(self):
        """CRC32 path hash -> transform path, from every Avatar's table (animations name bones by hash)."""
        if self._paths is None:
            self._paths = {}
            for obj in self.env.objects:
                if obj.type.name != "Avatar":
                    continue
                try:
                    tos = obj.read_typetree().get("m_TOS") or []
                except Exception:
                    continue
                for entry in tos:
                    if isinstance(entry, (list, tuple)) and len(entry) == 2:
                        self._paths[int(entry[0]) & 0xFFFFFFFF] = entry[1]
        return self._paths

    def transforms(self):
        """Every Transform: key -> {"name", "parent" (key or None), "pos", "rot" (x,y,z,w), "scale"}.

        Read once (can take a while in big games). Used to name animated bones and to pose skeletons.
        """
        if self._transforms is None:
            started = time.time()
            nodes = {}
            with self.lock:
                for obj in self.env.objects:
                    if obj.type.name not in ("Transform", "RectTransform"):
                        continue
                    try:
                        t = obj.read()
                        go = t.m_GameObject.deref()
                        father = t.m_Father
                        parent = None
                        if father.path_id:
                            f = father.deref()
                            parent = obj_key(f.assets_file, f.path_id)
                        p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
                        nodes[obj_key(obj.assets_file, obj.path_id)] = {
                            "name": go.peek_name() or "", "parent": parent,
                            "go": obj_key(go.assets_file, go.path_id),
                            "pos": (p.x, p.y, p.z), "rot": (r.x, r.y, r.z, r.w), "scale": (s.x, s.y, s.z)}
                    except Exception:
                        continue
            self._transforms = nodes
            log.info("Read %d transforms in %.1fs", len(nodes), time.time() - started)
        return self._transforms

    def _hierarchy_paths(self):
        """CRC32 -> path for every transform path relative to each of its ancestors (as animations use them)."""
        from .unity_anim import path_hash
        out = {}
        nodes = self.transforms()
        for key in nodes:
            chain, k = [], key
            while k is not None and k in nodes and len(chain) < 64:
                chain.append(nodes[k]["name"])
                k = nodes[k]["parent"]
            chain.reverse()  # root ... this transform
            for start in range(1, len(chain)):
                path = "/".join(chain[start:])
                out.setdefault(path_hash(path), path)
        return out

    def _skin_info(self, smr_reader):
        """(bone transform keys, mesh transform key) of a SkinnedMeshRenderer (cached)."""
        key = obj_key(smr_reader.assets_file, smr_reader.path_id)
        if key not in self._skins:
            smr = smr_reader.read()
            bones = []
            for ptr in smr.m_Bones:
                try:
                    b = ptr.deref()
                    bones.append(obj_key(b.assets_file, b.path_id))
                except Exception:
                    bones.append(None)
            go = smr.m_GameObject.deref()
            go_key = obj_key(go.assets_file, go.path_id)
            mesh_node = next((k for k, n in self.transforms().items() if n.get("go") == go_key), None)
            self._skins[key] = (bones, mesh_node)
        return self._skins[key]

    def _bone_suffixes(self, bone_keys):
        nodes = self.transforms()
        out = set()
        for k in bone_keys:
            chain, j = [], k
            while j is not None and j in nodes and len(chain) < 64:
                chain.append(nodes[j]["name"])
                j = nodes[j]["parent"]
            chain.reverse()
            for i in range(len(chain)):
                out.add("/".join(chain[i:]))
        return out

    def animation_targets(self, asset):
        clip = self._clip(asset)
        wanted = {c["path"] for c in clip["curves"]
                  if c["property"] in ("position", "rotation", "euler", "scale")}
        if not wanted:
            return []
        self.finder._run_until(lambda: self.finder.indexed)
        scored = []
        with self.lock:
            skinned = dict(self.finder._skinned)
        for mesh_key, smrs in skinned.items():
            model = self.by_key.get(mesh_key)
            if model is None:
                continue
            try:
                with self.lock:
                    bones, _node = self._skin_info(smrs[0])
            except Exception:
                continue
            score = len(wanted & self._bone_suffixes([b for b in bones if b is not None]))
            if score:
                scored.append((score, model))
        scored.sort(key=lambda s: (-s[0], s[1].name.lower()))
        return [m for _s, m in scored[:50]]

    def animate(self, model, clip_asset):
        from UnityPy.helpers.MeshHelper import MeshHandler
        from .unity_skin import Animator
        self.finder._run_until(lambda: self.finder.indexed)
        smrs = self.finder._skinned.get(model.key)
        if not smrs:
            raise ValueError("This model isn't skinned (no SkinnedMeshRenderer uses it).")
        with self.lock:
            bones, mesh_node = self._skin_info(smrs[0])
            mesh = model.ref.read()
            handler = MeshHandler(mesh)
            handler.process()
        if not handler.m_BoneWeights or not handler.m_BoneIndices:
            raise ValueError("This model has no bone weights.")
        bind = getattr(handler, "m_BindPose", None) or mesh.m_BindPose
        bind_poses = []
        for m in bind:
            if hasattr(m, "e00"):
                bind_poses.append([[getattr(m, f"e{r}{c}") for c in range(4)] for r in range(4)])
            else:
                bind_poses.append(list(m) if len(m) == 4 else [m[i * 4:(i + 1) * 4] for i in range(4)])
        bind_poses = np.asarray(bind_poses, float).reshape(-1, 4, 4)
        n = len(bone_keys := bones)
        if len(bind_poses) < n:
            bind_poses = np.concatenate([bind_poses, np.tile(np.eye(4), (n - len(bind_poses), 1, 1))])
        animator = Animator(handler.m_Vertices, handler.m_BoneIndices, handler.m_BoneWeights, bind_poses[:n],
                            bone_keys, self.transforms(), mesh_node, self._clip(clip_asset))
        if not animator.matched:
            raise ValueError("None of this animation's bones are in this model's skeleton.")
        return animator

    def _clip(self, asset):
        from .unity_anim import decode_clip
        with self.lock:
            tree = asset.ref.read_typetree()
        names = self._path_names()
        hashes = [b.get("path", 0) for b in (tree.get("m_ClipBindingConstant") or {}).get("genericBindings") or []]
        if any(h and h not in names for h in hashes) and not self._hierarchy_done:
            # Not in any Avatar's table: name them from the scene/prefab transform hierarchy.
            self._hierarchy_done = True
            for h, path in self._hierarchy_paths().items():
                names.setdefault(h, path)
        return decode_clip(tree, names)

    def audio(self, asset):
        clip = asset.ref.read()
        samples = clip.samples  # UnityPy converts through FMOD to WAV
        if not samples:
            raise ValueError("This AudioClip has no sound data in the game files.")
        _name, data = next(iter(samples.items()))
        return bytes(data), "wav"

    def stats(self, asset):
        obj = asset.ref
        stats = {"size": obj.byte_size}
        if asset.kind == "animation":
            tree = obj.read_typetree()
            muscle = tree.get("m_MuscleClip") or {}
            length = float(muscle.get("m_StopTime", 0) or 0) - float(muscle.get("m_StartTime", 0) or 0)
            stats.update(info=f"{length:.2f} s", sort=length)
            return stats
        if asset.kind == "audio":
            clip = obj.read()
            length = float(getattr(clip, "m_Length", 0) or 0)
            size = getattr(getattr(clip, "m_Resource", None), "m_Size", 0) or 0
            stats.update(size=obj.byte_size + size, info=f"{int(length // 60)}:{length % 60:04.1f}", sort=length)
            return stats
        if asset.kind == "text":
            stats.update(info="", sort=obj.byte_size)
            return stats
        data = obj.read()
        if asset.kind == "model":
            tris = triangle_count(data.m_SubMeshes)
            vertex_data = getattr(data, "m_VertexData", None)
            verts = vertex_data.m_VertexCount if vertex_data is not None else 0
            stats.update(tris=tris, verts=verts, info=f"{tris:,} tris", sort=tris)
        elif asset.kind == "texture":
            w, h = data.m_Width, data.m_Height
            stream = getattr(data, "m_StreamData", None)
            if stream is not None and stream.size:
                stats["size"] += stream.size  # pixel data lives in the .resS file
            stats.update(w=w, h=h, info=f"{w}×{h}", sort=w * h)
        elif asset.kind == "sprite":
            w, h = int(data.m_Rect.width), int(data.m_Rect.height)
            stats.update(w=w, h=h, info=f"{w}×{h}", sort=w * h)
        return stats

    def materials_ready(self):
        return self.finder.indexed

    def _info(self, asset):
        return self.finder.info(asset.ref)

    def materials(self, asset):
        _names, mats, _n = self._info(asset)
        out = []
        for mat in mats:
            refs = []
            for prop, tex_name, reader in mat["textures"]:
                role = ALBEDO if prop in ALBEDO_PROPS else NORMAL if prop in NORMAL_PROPS else OTHER
                refs.append(TextureRef(prop, tex_name, self._asset_for(reader, "Texture2D", tex_name), role))
            out.append(Material(mat["name"], refs))
        return out

    def describe(self, asset):
        rows = [("File", asset.source)]
        if asset.path:
            rows.append(("Path", asset.path))
        if asset.kind == "model" and self.finder.indexed:
            try:
                users, _mats, n_users = self._info(asset)
            except Exception:
                users, n_users = [], 0
            if users:
                names = sorted(set(users))
                shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 or n_users > len(users) else "")
                rows.append((f"Used by {n_users:,} object(s)", shown))
        return rows

    def related(self, asset):
        if asset.kind == "sprite":
            # A sprite is a piece of a texture (sprite sheet): link to it.
            links = []
            try:
                reader = asset.ref.read().m_RD.texture.deref()
                links.append(self._asset_for(reader, "Texture2D"))
            except Exception:
                pass
            return "Sprite sheet", links, "Couldn't find the texture this sprite comes from."
        if asset.kind == "texture":
            try:
                keys = self.finder.texture_users(asset.ref)
            except Exception:
                log.exception("Finding models that use %s failed", asset.name)
                keys = set()
            links = sorted((self.by_key[k] for k in keys if k in self.by_key), key=lambda a: a.name.lower())
            return (f"Used by {len(links)} model(s)", links,
                    "No models found that use this texture (UI images and sprites often aren't on models).")
        return "", [], ""


class UnityPlugin(EnginePlugin):
    id = "unity"
    name = "Unity"
    version = "1.2"
    author = "UniView"
    description = "Unity games (.assets, level files, AssetBundles, .resS) via UnityPy."

    def detect(self, path):
        if os.path.isfile(path):
            return 90 if looks_like_unity_file(path) else 0
        if unity_data_dir(path):
            return 100
        # A loose folder of asset files / bundles: sniff a few likely-looking files, shallowly
        # (this runs for every game on the Projects page, so it must stay cheap on big folders).
        listed = sniffed = 0
        root_depth = path.rstrip("\\/").count(os.sep)
        for dirpath, dirs, files in os.walk(path):
            if dirpath.count(os.sep) - root_depth >= 3:
                dirs[:] = []
            for name in files:
                listed += 1
                if listed > 3000 or sniffed > 40:
                    return 0
                if os.path.splitext(name)[1].lower() not in SNIFF_EXTS:
                    continue
                sniffed += 1
                if looks_like_unity_file(os.path.join(dirpath, name)):
                    return 40
        return 0

    def game_info(self, path):
        return detect_unity_info(path)

    def count_files(self, path):
        return len(find_unity_files(path))

    def short_version(self, info):
        return unity_branch(info.get("engine_version"))

    def open(self, path, progress):
        import UnityPy
        started = time.time()
        progress(f"Scanning {path} for Unity files ...")
        log.info("Scanning %s for Unity files", path)
        files = find_unity_files(path)
        if not files:
            raise FileNotFoundError(f"No Unity asset files found in:\n{path}")
        log.info("Found %d Unity files in %.1fs", len(files), time.time() - started)
        root = path if os.path.isdir(path) else os.path.dirname(path)
        env = UnityPy.Environment(path=root)
        failed = 0
        for n, fpath in enumerate(files, 1):
            rel = os.path.relpath(fpath, root)
            progress(f"Loading file {n}/{len(files)}: {rel}", n - 1, len(files))
            log.info("Loading %d/%d  %s  (%.1f MB)", n, len(files), rel, os.path.getsize(fpath) / 1e6)
            try:
                env.load_files([fpath])
            except Exception as e:
                failed += 1  # one broken/encrypted file shouldn't stop the rest
                log.warning("Could not load %s: %s: %s", rel, type(e).__name__, e)
        progress("Indexing objects ...")
        session = UnitySession(self, path, env, len(files))
        for i, obj in enumerate(env.objects):
            type_name = obj.type.name
            kind = TYPE_KINDS.get(type_name)
            if kind is None:
                continue
            try:
                name = obj.peek_name()
            except Exception:
                name = None
            container = getattr(obj, "container", None)
            if not name:
                # Unnamed (common for combined/prefab meshes): name it after where it lives.
                stem = os.path.splitext(os.path.basename(container))[0] if container else type_name
                name = f"{stem} #{obj.path_id % 100000:05d}"
            key = obj_key(obj.assets_file, obj.path_id)
            asset = Asset(kind, name, key, uid=f"{obj.assets_file.name}:{obj.path_id}", size=obj.byte_size,
                          path=container or "", source=obj.assets_file.name, ref=obj,
                          ext={"text": "txt", "audio": "wav", "animation": "json"}.get(kind, ""))
            session.assets.append(asset)
            session.by_key[key] = asset
            if i % 2000 == 0:
                progress(f"Indexing objects ... {i}")
        if failed:
            session.warnings.append(f"{failed} file(s) could not be loaded (see the console).")
        version = next((v for v in (getattr(f, "unity_version", "") for f in getattr(env, "assets", []))
                        if v and UNITY_VERSION_RE.fullmatch(v.encode()) and not v.startswith("0.0")), "")
        session.engine_version = version
        log.info("Unity load done in %.1fs (%d file(s) failed)", time.time() - started, failed)
        return session


PLUGIN = UnityPlugin()
