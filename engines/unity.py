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
              "AnimationClip": "animation",
              "Font": "font", "VideoClip": "video", "MonoBehaviour": "data"}
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


# --------------------------------------------------------------------------- materials

COLOR_PROPS = ("_Color", "_BaseColor", "_MainColor", "_TintColor", "_Albedo", "_BaseColorFactor")


def _prop_name(prop):
    return getattr(prop, "name", prop) if not isinstance(prop, str) else prop


def read_material(mat):
    """A parsed Material -> {"name", "textures": [(property, texture name, ObjectReader)], "color", "properties"}."""
    saved = mat.m_SavedProperties
    textures = []
    for prop, tex_env in saved.m_TexEnvs:
        prop = _prop_name(prop)
        tptr = tex_env.m_Texture
        if not tptr.path_id:
            continue
        try:
            reader = tptr.deref()
            if reader.type.name != "Texture2D":
                continue
            textures.append((prop, reader.peek_name() or prop, reader))
        except Exception:
            continue
    textures.sort(key=lambda t: t[0] not in ALBEDO_PROPS)
    colors, properties = {}, []
    for prop, c in getattr(saved, "m_Colors", None) or []:
        prop = _prop_name(prop)
        try:
            rgba = (float(c.r), float(c.g), float(c.b), float(c.a))
        except AttributeError:
            continue
        colors[prop] = rgba
        properties.append((prop, "#%02x%02x%02x  alpha %.2f" % tuple(
            [int(max(0, min(1, v)) * 255) for v in rgba[:3]] + [rgba[3]])))
    for prop, value in getattr(saved, "m_Floats", None) or []:
        try:
            properties.append((_prop_name(prop), f"{float(value):.3g}"))
        except (TypeError, ValueError):
            continue
    color = next((colors[p] for p in COLOR_PROPS if p in colors), None)
    shader = ""
    try:
        shader = mat.m_Shader.deref().peek_name() or ""
    except Exception:
        pass
    if shader:
        properties.insert(0, ("Shader", shader))
    return {"name": mat.m_Name, "textures": textures, "color": color, "properties": properties}


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
                    materials.append(read_material(ptr.deref_parse_as_object()))
                except Exception:
                    continue
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



# --------------------------------------------------------------------------- scripts (MonoBehaviour data)

UNDER_READ = re.compile(r"Expected to read (\d+) bytes, but only read (\d+) bytes")


def _aligned_nodes(gen):
    """get_nodes_up for a TypeTreeGenerator, with MonoBehaviour's m_Enabled marked as 4-byte aligned.

    The IL2CPP (AssetStudio) backend leaves the flag off, so every field after it was read 3 bytes
    early. The C field reader copies nodes when first used, so they're rebuilt rather than patched.
    """
    from UnityPy.helpers.TypeTreeNode import TypeTreeNode
    original = gen.get_nodes_up
    cache = {}

    def get_nodes_up(assembly, fullname):
        key = (assembly, fullname)
        if key not in cache:
            root = original(assembly, fullname)
            flat = []

            def walk(node):
                flag = node.m_MetaFlag or 0
                if node.m_Level == 1 and node.m_Name == "m_Enabled" and node.m_Type == "UInt8":
                    flag |= 0x4000
                flat.append(TypeTreeNode(node.m_Level, node.m_Type, node.m_Name, 0, 0, m_MetaFlag=flag))
                for child in node.m_Children or []:
                    walk(child)

            walk(root)
            cache[key] = TypeTreeNode.from_list(flat)
        return cache[key]
    return get_nodes_up


class ScriptReader:
    """Reads MonoBehaviour fields. Cooked games don't store script field layouts, so they're generated
    from the game's code (Managed/*.dll for Mono, GameAssembly.dll + metadata for IL2CPP) with
    TypeTreeGeneratorAPI, trying a second backend when the first fails for a class."""

    def __init__(self, env, game_dir):
        self.env = env
        self.game_dir = game_dir
        self.generators = None
        self.error = ""
        self._embedded = None  # script class -> field layout stored in some file (usually an AssetBundle)
        self._ref_types = []   # [SerializeReference] type layouts stored in those files

    def _setup(self):
        if self.generators is not None:
            return
        self.generators = []
        try:
            from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
        except ImportError:
            self.error = "Install TypeTreeGeneratorAPI to read script fields (py -m pip install TypeTreeGeneratorAPI)."
            return
        version = next((f.unity_version for f in getattr(self.env, "assets", []) if getattr(f, "unity_version", "")), "")
        il2cpp = os.path.isfile(os.path.join(self.game_dir, "GameAssembly.dll"))
        for backend in (("AssetStudio", "AssetsTools") if il2cpp else ("AssetsTools", "AssetStudio")):
            try:
                gen = TypeTreeGenerator(version, backend)
                gen.load_local_game(self.game_dir)
                gen.get_nodes_up = _aligned_nodes(gen)
                self.generators.append(gen)
            except Exception as e:
                self.error = f"Couldn't load the game's code: {e}"
        if self.generators:
            log.info("Script field layouts from the game's %s code (%s)", "IL2CPP" if il2cpp else "Mono",
                     ", ".join(type(g).__name__ for g in self.generators))

    def _embedded_layouts(self):
        """Field layouts that AssetBundles store with their objects, by script class. The same class in a
        file without layouts (resources.assets, sharedassets) can borrow them: they come from the build
        itself and include [SerializeReference] fields the code-based generators leave out."""
        if self._embedded is None:
            self._embedded = {}
            seen, files = set(), set()
            for obj in self.env.objects:
                if id(obj.assets_file) not in files:
                    files.add(id(obj.assets_file))
                    self._ref_types += [r for r in getattr(obj.assets_file, "ref_types", None) or () if r.node is not None]
                st = obj.serialized_type
                if obj.type.name != "MonoBehaviour" or st is None or st.node is None:
                    continue
                key = (id(obj.assets_file), id(st))  # one script per type entry of a file
                if key in seen:
                    continue
                seen.add(key)
                cls = script_class(obj)
                if cls:
                    self._embedded.setdefault(cls, st.node)
            if self._embedded:
                log.info("%d script layout(s) found stored with bundle objects", len(self._embedded))
        return self._embedded

    def read(self, obj):
        """(dict of fields, note)"""
        st = obj.serialized_type
        if st is not None and st.node is not None:
            try:
                return obj.read_typetree(), ""  # stored with the object (AssetBundles): no need for the game's code
            except Exception as e:
                log.debug("Stored layout of '%s' didn't fit: %s", script_class(obj), e)
        else:
            node = self._embedded_layouts().get(script_class(obj))
            if node is not None:
                f = obj.assets_file
                own_refs = f.ref_types
                # Layouts of the objects its [SerializeReference] fields hold (this file lists their names only).
                f.ref_types = self._ref_types + [r for r in own_refs or () if r.node is not None]
                try:
                    return obj.read_typetree(nodes=node), ""
                except Exception as e:  # a different version of the class: generate the layout instead
                    log.debug("Bundle layout of '%s' didn't fit: %s", script_class(obj), e)
                finally:
                    f.ref_types = own_refs
        self._setup()
        previous = self.env.typetree_generator
        best = None  # (bytes read, generator) of the layout that got furthest without overrunning
        try:
            for gen in self.generators:
                self.env.typetree_generator = gen
                try:
                    return obj.read_typetree(), ""
                except Exception as e:
                    m = UNDER_READ.search(str(e))
                    if m and (best is None or int(m.group(2)) > best[0]):
                        best = (int(m.group(2)), gen)
            if best is not None:
                # The generated layout ends before the data does: usually the registry of
                # [SerializeReference] objects Unity appends at the end, which the generators leave out.
                read, gen = best
                self.env.typetree_generator = gen
                try:
                    tree = obj.read_typetree(check_read=False)
                except Exception:
                    tree = None
                if tree is not None:
                    tail = bytes(obj.get_raw_data())[read:]
                    if len(tail) == 8 and tail[4:] == bytes(4) and tail[:4] in (b"\1\0\0\0", b"\2\0\0\0"):
                        tree["references"] = {"version": tail[0], "RefIds": []}
                        return tree, ""
                    _add_strings(tree, tail)
                    return tree, (f"The last {len(tail):,} bytes of this object couldn't be decoded (objects "
                                  "stored by [SerializeReference], or fields the reader doesn't know); "
                                  "every field above them is shown.")
        finally:
            self.env.typetree_generator = previous
        # Only the fields every MonoBehaviour has.
        try:
            tree = obj.read_typetree(check_read=False)
        except Exception:
            tree = {}
        try:
            _add_strings(tree, bytes(obj.get_raw_data()), skip=(tree.get("m_Name"),))
        except Exception:
            pass
        note = self.error or "This script's own fields couldn't be decoded (its class uses features the field " \
                             "reader doesn't support yet); showing the common fields only."
        return tree, note


def find_strings(data, min_len=2):
    """Text stored the way Unity serializes strings (int32 length, UTF-8, aligned to 4) in undecoded bytes."""
    found, pos, end = [], 0, len(data) - 4
    while pos <= end:
        n = int.from_bytes(data[pos:pos + 4], "little")
        if min_len <= n <= 4096 and pos + 4 + n <= len(data):
            chunk = data[pos + 4:pos + 4 + n]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text.isprintable() and any(c.isalnum() for c in text):
                found.append(text)
                pos += (4 + n + 3) & ~3
                continue
        pos += 4
    return found


def _add_strings(tree, data, skip=()):
    """Add the text found in bytes the reader couldn't decode, so names/values can still be searched."""
    strings = [t for t in find_strings(data) if t not in skip]
    if strings:
        tree["(text in undecoded data)"] = strings


def _path_closeness(a, b):
    """How many leading folders two asset paths share (+1 when they're the same file)."""
    if not a or not b:
        return 0
    pa, pb = a.lower().split("/"), b.lower().split("/")
    n = 0
    while n < min(len(pa), len(pb)) - 1 and pa[n] == pb[n]:
        n += 1
    return n + (a.lower() == b.lower())


def script_class(obj):
    """'Namespace.ClassName' of a MonoBehaviour's script, or ''."""
    try:
        script = obj.read(check_read=False).m_Script.read()
        ns, cls = getattr(script, "m_Namespace", ""), getattr(script, "m_ClassName", "")
        return f"{ns}.{cls}" if ns else cls
    except Exception:
        return ""


def json_safe(value):
    """typetree values -> JSON-friendly (bytes shortened)."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return f"<{len(data)} bytes>" if len(data) > 64 else data.hex()
    if isinstance(value, float) and value != value:
        return "NaN"
    return value


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
        self._scenes = {}
        self._script_reader = None
        self._sheets = None
        self._skins = {}
        self._terrains = {}
        self._rigs = None        # humanoid Avatars (unity_humanoid.HumanRig)
        self._model_rigs = {}    # mesh key -> the HumanRig that fits its skeleton, or None
        self._humanoid = {}      # (clip key, rig) -> clip converted to bone curves

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

    def options_changed(self):
        self._scenes.clear()

    # ------------------------------------------------------------------ terrains

    @staticmethod
    def _is_terrain(asset):
        return isinstance(asset.ref, dict) and asset.ref.get("type") in ("terrain", "terrain_tex")

    def terrain(self, obj, grid=None):
        """(MeshData, Material, info rows, heightmap resolution) of a TerrainData reader, cached."""
        from . import unity_terrain
        key = (obj_key(obj.assets_file, obj.path_id), grid)
        if key not in self._terrains:
            with self.lock:
                tree = obj.read_typetree()
            if grid:
                old, unity_terrain.MAX_GRID = unity_terrain.MAX_GRID, grid
            try:
                md, size = unity_terrain.terrain_mesh(tree)
            finally:
                if grid:
                    unity_terrain.MAX_GRID = old
            name = tree.get("m_Name") or "Terrain"
            md.name = name
            tex = Asset("texture", f"{name} (blended terrain layers)", ("terrain_tex",) + key[0],
                        uid=f"terrain_tex:{obj.assets_file.name}:{obj.path_id}", source=obj.assets_file.name,
                        ref={"type": "terrain_tex", "obj": obj, "tree": tree, "size": size})
            hm = tree["m_Heightmap"]
            splat = tree.get("m_SplatDatabase") or {}
            layers = len(splat.get("m_TerrainLayers") or splat.get("m_Splats") or [])
            res = hm.get("m_Resolution") or hm.get("m_Width")
            info = [("Heightmap", f"{res}\u00d7{res}"),
                    ("Size", f"{size[0]:g} \u00d7 {size[1]:g} m, {hm['m_Scale']['y']:g} m high"),
                    ("Terrain layers", str(layers))]
            if splat.get("m_AlphaTextures") and layers:
                material = Material(f"{name} layers", [TextureRef("_Splat", tex.name, tex, ALBEDO)])
            else:  # never painted (or drawn by a custom material): plain ground color
                material = Material(f"{name} layers", [], color=(0.42, 0.5, 0.3, 1.0))
            self._terrains[key] = (md, material, info, res)
        return self._terrains[key]

    def _terrain_image(self, ref):
        from .unity_terrain import terrain_texture
        if "image" not in ref:
            with self.lock:
                ref["image"] = terrain_texture(self, ref["obj"], ref["tree"], ref["size"])
        if ref["image"] is None:
            raise ValueError("This terrain has no painted layers.")
        return ref["image"]

    def image(self, asset):
        if self._is_terrain(asset):
            return self._terrain_image(asset.ref)
        if asset.kind == "font":
            from .sdk import font_preview
            return font_preview(self.raw(asset), asset.name)
        return asset_image(asset.ref.read())

    def mesh(self, asset):
        if self._is_terrain(asset):
            md = self.terrain(asset.ref["obj"])[0]
            asset.ref["resolution"] = self.terrain(asset.ref["obj"])[3]
            return md
        if asset.kind == "scene":
            return self._scene(asset)[0]
        return mesh_to_meshdata(asset.ref.read())

    def _scene_roots(self, asset):
        """Root Transform readers of a scene or prefab asset."""
        ref = asset.ref
        if ref["type"] == "scene":
            roots = []
            for t in ref["transforms"]:
                try:
                    if not t.read().m_Father.path_id:
                        roots.append(t)
                except Exception:
                    continue
            return roots
        if ref["type"] == "root":
            return [ref["transform"]]
        # A prefab bundle entry: find its root GameObject's transform.
        gos = ref["gameobjects"]
        base = os.path.splitext(os.path.basename(asset.path))[0].lower()
        go = next((g for g in gos if (g.peek_name() or "").lower() == base), gos[0])
        from .unity_scene import _components
        transform = next((r for n, r in _components(go.read()) if n in ("Transform", "RectTransform")), None)
        if transform is None:
            raise ValueError("This prefab has no transform.")
        for _ in range(200):
            father = transform.read().m_Father
            if not father.path_id:
                break
            transform = father.deref()
        return [transform]

    def hierarchy(self, asset):
        """GameObject tree of a scene/prefab asset as a flat list (parents before children):
        [{"name", "parent" (index or -1), "active", "pos" (x,y,z), "rot" (x,y,z,w), "scale",
          "mesh" (uid of the Mesh asset or None), "skinned" (bool), "renderer_enabled"}], in Unity's own space."""
        from .unity_scene import _components
        nodes = []

        def visit(transform_reader, parent, depth):
            if depth > 200:
                return
            try:
                t = transform_reader.read()
                go = t.m_GameObject.deref().read()
            except Exception:
                return
            p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
            node = {"name": getattr(go, "m_Name", "") or "GameObject", "parent": parent,
                    "active": bool(getattr(go, "m_IsActive", True)),
                    "pos": [p.x, p.y, p.z], "rot": [r.x, r.y, r.z, r.w], "scale": [s.x, s.y, s.z],
                    "mesh": None, "skinned": False, "renderer_enabled": True}
            comps = _components(go)
            mesh_ptr = None
            for name, reader in comps:
                try:
                    if name == "SkinnedMeshRenderer":
                        renderer = reader.read()
                        mesh_ptr, node["skinned"] = renderer.m_Mesh, True
                        node["renderer_enabled"] = bool(getattr(renderer, "m_Enabled", True))
                    elif name == "MeshFilter" and mesh_ptr is None:
                        mesh_ptr = reader.read().m_Mesh
                    elif name == "MeshRenderer":
                        node["renderer_enabled"] = bool(getattr(reader.read(), "m_Enabled", True))
                except Exception:
                    continue
            if mesh_ptr is not None and getattr(mesh_ptr, "path_id", 0):
                try:
                    node["mesh"] = self._asset_for(mesh_ptr.deref(), "Mesh").uid
                except Exception:
                    pass
            index = len(nodes)
            nodes.append(node)
            for child in t.m_Children or []:
                try:
                    visit(child.deref(), index, depth + 1)
                except Exception:
                    continue

        with self.lock:
            for root in self._scene_roots(asset):
                visit(root, -1, 0)
        return nodes

    def _scene(self, asset):
        """(MeshData, materials, info rows) of a scene/prefab, cached (the last few)."""
        from .unity_scene import build
        if asset.key not in self._scenes:
            with self.lock:
                result = build(self, self._scene_roots(asset), asset.name.split(": ", 1)[-1])
            self._scenes[asset.key] = result
            while len(self._scenes) > 3:
                self._scenes.pop(next(iter(self._scenes)))
        return self._scenes[asset.key]

    def raw(self, asset):
        if asset.kind == "font":
            with self.lock:
                return bytes(asset.ref.read().m_FontData)
        if asset.kind == "video":
            return self.video(asset)[0]
        if asset.kind == "data":
            import json
            tree, _note = self._data(asset)
            return json.dumps(tree, indent=2, ensure_ascii=False).encode("utf-8")
        if asset.kind == "animation":
            import json
            return json.dumps(self._clip(asset), indent=1).encode("utf-8")
        script = asset.ref.read().m_Script
        return script.encode("utf-8", "surrogateescape") if isinstance(script, str) else bytes(script)

    def text(self, asset):
        if asset.kind == "data":
            import json
            tree, note = self._data(asset)
            with self.lock:
                tree = self._label_refs(asset.ref, tree)
            header = f"{asset.name}   ({script_class(asset.ref) or 'script'})\n"
            if note:
                header += f"\nNote: {note}\n"
            return header + "\n" + json.dumps(tree, indent=2, ensure_ascii=False)
        if asset.kind == "animation":
            from .unity_anim import summary_text
            return summary_text(self._clip(asset))
        script = asset.ref.read().m_Script
        return script.decode("utf-8", "replace") if isinstance(script, bytes) else script

    def _label_refs(self, obj, tree):
        """Copy of a data tree where object references also say what they point to."""
        names = {}

        def label(file_id, path_id):
            key = (file_id, path_id)
            if key not in names:
                names[key] = None
                try:
                    target = self.finder._file(obj.assets_file, file_id)
                    reader = target.objects.get(path_id) if target is not None else None
                    if reader is not None:
                        name = reader.peek_name() if reader.type.name != "MonoBehaviour" else (
                            reader.peek_name() or script_class(reader))
                        names[key] = f"{reader.type.name} '{name}'" if name else reader.type.name
                except Exception:
                    pass
            return names[key]

        def walk(value, depth=0):
            if depth > 60:
                return value
            if isinstance(value, dict):
                if set(value) == {"m_FileID", "m_PathID"} and value["m_PathID"]:
                    target = label(value["m_FileID"], value["m_PathID"])
                    return {**value, "points_to": target} if target else value
                return {k: walk(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, depth + 1) for v in value]
            return value
        return walk(tree)

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

    # ---- humanoid (muscle) clips
    MIN_HUMAN_BONES = 10  # skeleton bones an Avatar must share with a model to drive it

    def _human_rigs(self):
        """Every humanoid Avatar in the game (read once)."""
        if self._rigs is None:
            from .unity_humanoid import HumanRig
            rigs = []
            with self.lock:
                for obj in self.env.objects:
                    if obj.type.name != "Avatar":
                        continue
                    try:
                        rig = HumanRig(obj.read_typetree())
                    except Exception:
                        continue
                    if rig.valid:
                        rig.source_path = getattr(obj, "container", None) or ""
                        rigs.append(rig)
            self._rigs = rigs
            log.info("%d humanoid Avatar(s)", len(rigs))
        return self._rigs

    def _rig_for(self, mesh_key, bone_keys):
        """The humanoid Avatar whose skeleton fits these bones best (cached per model), or None."""
        if mesh_key not in self._model_rigs:
            suffixes = self._bone_suffixes([b for b in bone_keys if b is not None])
            model = self.by_key.get(mesh_key)
            best, best_score = None, (0,)
            for rig in self._human_rigs():
                shared = rig.human_paths & suffixes
                # Most bones in common; then the Avatar from the model's own file; then the fullest
                # paths (a rig whose paths are just 'Hips/Spine' fits every skeleton equally).
                score = (len(shared), _path_closeness(getattr(rig, "source_path", ""), model.path if model else ""),
                         sum(p.count("/") for p in shared))
                if score > best_score:
                    best, best_score = rig, score
            self._model_rigs[mesh_key] = best if best_score[0] >= self.MIN_HUMAN_BONES else None
        return self._model_rigs[mesh_key]

    def _skinned_models(self):
        """[(model Asset, bone keys)] of every skinned mesh."""
        self.finder._run_until(lambda: self.finder.indexed)
        with self.lock:
            skinned = dict(self.finder._skinned)
        out = []
        for mesh_key, smrs in skinned.items():
            model = self.by_key.get(mesh_key)
            if model is None:
                continue
            try:
                with self.lock:
                    bones, _node = self._skin_info(smrs[0])
            except Exception:
                continue
            out.append((model, bones))
        return out

    def animation_targets(self, asset):
        from .unity_humanoid import is_humanoid
        clip = self._clip(asset)
        if is_humanoid(clip):
            # Humanoid clips play on any humanoid character: list the models that have an Avatar.
            # The clip's own character first (same folder), then the most complete skeletons.
            models = [(m, len(bones)) for m, bones in self._skinned_models() if self._rig_for(m.key, bones) is not None]
            models.sort(key=lambda mb: (-_path_closeness(asset.path, mb[0].path), -mb[1], mb[0].name.lower()))
            return [m for m, _n in models[:200]]
        wanted = {c["path"] for c in clip["curves"]
                  if c["property"] in ("position", "rotation", "euler", "scale")}
        if not wanted:
            return []
        scored = []
        for model, bones in self._skinned_models():
            score = len(wanted & self._bone_suffixes([b for b in bones if b is not None]))
            if score:
                scored.append((score, _path_closeness(asset.path, model.path), model))
        # Creatures often share bone names: among equal matches, models from the clip's own file/folder first.
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].name.lower()))
        return [m for _s, _c, m in scored[:50]]

    def _skin_setup(self, model):
        """(MeshHandler, bone transform keys, mesh transform key, bind poses (B, 4, 4)) of a skinned model."""
        from UnityPy.helpers.MeshHelper import MeshHandler
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
        n = len(bones)
        if len(bind_poses) < n:
            bind_poses = np.concatenate([bind_poses, np.tile(np.eye(4), (n - len(bind_poses), 1, 1))])
        return handler, bones, mesh_node, bind_poses[:n]

    def animate(self, model, clip_asset):
        from .unity_skin import Animator
        handler, bones, mesh_node, bind_poses = self._skin_setup(model)
        animator = Animator(handler.m_Vertices, handler.m_BoneIndices, handler.m_BoneWeights, bind_poses,
                            bones, self.transforms(), mesh_node, self.clip_for(model, clip_asset, bones))
        if not animator.matched:
            raise ValueError("None of this animation's bones are in this model's skeleton.")
        return animator

    def skeleton(self, model, clip=None):
        from .unity_skin import export_animation, export_rig
        handler, bones, mesh_node, bind_poses = self._skin_setup(model)
        animator = self.animate(model, clip) if clip is not None else None
        rig, joint_of = export_rig(self.transforms(), bones, bind_poses, handler.m_BoneIndices,
                                   handler.m_BoneWeights, mesh_node, animator.root_node if animator else None)
        if len(rig["joints_0"]) != handler.m_VertexCount:
            raise ValueError("The bone weights don't match the mesh's vertices.")
        return rig, (export_animation(animator, joint_of, clip.name) if animator else None)

    def clip_for(self, model, clip_asset, bones=None):
        """Decoded clip with bone curves for `model`: humanoid clips are converted for its Avatar."""
        from .unity_humanoid import is_humanoid, place_hips, to_generic
        clip = self._clip(clip_asset)
        if not is_humanoid(clip):
            return clip
        if bones is None:
            smrs = self.finder._skinned.get(model.key)
            if not smrs:
                raise ValueError("This model isn't skinned (no SkinnedMeshRenderer uses it).")
            with self.lock:
                bones, _node = self._skin_info(smrs[0])
        rig = self._rig_for(model.key, bones)
        if rig is None:
            raise ValueError("This is a humanoid animation, and this model has no humanoid Avatar to play it with.")
        key = (clip_asset.key, id(rig))
        if key not in self._humanoid:
            self._humanoid[key] = to_generic(clip, rig)
            while len(self._humanoid) > 8:
                self._humanoid.pop(next(iter(self._humanoid)))
        clip = self._humanoid[key]
        parent = self._hips_parent(bones, clip["hips"]["path"])
        return place_hips(clip, parent) if parent is not None else clip

    def _hips_parent(self, bone_keys, hips_path):
        """Matrix of the model's hips parent relative to the animated root (the object `hips_path`,
        e.g. 'Armature/Hips', is relative to), from the model's own transforms; None if not found."""
        from .unity_skin import trs
        nodes = self.transforms()
        parts = hips_path.split("/")
        for key in bone_keys:
            chain, k = [], key
            while k is not None and k in nodes and len(chain) < len(parts):
                chain.append(k)
                k = nodes[k]["parent"]
            if len(chain) < len(parts) or [nodes[c]["name"] for c in reversed(chain)] != parts:
                continue
            m = np.eye(4)
            for c in reversed(chain[1:]):  # root's child ... the hips' parent
                n = nodes[c]
                m = m @ trs(n["pos"], n["rot"], n["scale"])
            return m
        return None

    # ---- sprites: sheets and flipbook animations
    def _sprite_sheets(self):
        """texture key -> [(sprite Asset, (x, y, w, h))] (y from the bottom), built on first use."""
        if self._sheets is None:
            sheets = {}
            started = time.time()
            with self.lock:
                for a in self.assets:
                    if a.kind != "sprite":
                        continue
                    try:
                        tree = a.ref.read_typetree()
                        rd = tree["m_RD"]
                        tex = rd["texture"]
                        if not tex.get("m_PathID"):
                            continue
                        target = self.finder._file(a.ref.assets_file, tex.get("m_FileID", 0))
                        if target is None:
                            continue
                        r = rd.get("textureRect") or tree["m_Rect"]
                        sheets.setdefault(obj_key(target, tex["m_PathID"]), []).append(
                            (a, (r["x"], r["y"], r["width"], r["height"])))
                    except Exception:
                        continue
            self._sheets = sheets
            log.info("Indexed which textures %d sprites come from in %.1fs",
                     sum(len(v) for v in sheets.values()), time.time() - started)
        return self._sheets

    def sprite_rects(self, asset):
        if asset.kind != "texture":
            return []
        return [rect for _a, rect in self._sprite_sheets().get(asset.key, [])]

    def sprite_frames(self, asset):
        if asset.kind != "animation":
            return [], 0.0
        clip = self._clip(asset)
        length = clip.get("length") or 0.0
        with self.lock:
            obj = asset.ref.read()
            curves = [c for c in clip["curves"] if c.get("object_curve")]
            if curves:
                mapping = list(obj.m_ClipBindingConstant.pptrCurveMapping or [])
                for curve in sorted(curves, key=lambda c: c.get("class_id") != 212):
                    frames = []
                    for t, v in curve["keys"]:
                        i = int(round(v))
                        if not 0 <= i < len(mapping) or not mapping[i].path_id:
                            continue
                        try:
                            reader = mapping[i].deref()
                        except Exception:
                            continue
                        if reader.type.name in ("Sprite", "Texture2D"):
                            frames.append((t, self._asset_for(reader, reader.type.name)))
                    if frames:
                        return frames, max(length, frames[-1][0])
            for pc in getattr(obj, "m_PPtrCurves", None) or []:  # legacy clips
                if getattr(pc, "attribute", "") != "m_Sprite":
                    continue
                frames = []
                for key in pc.curve:
                    try:
                        reader = key.value.deref()
                        frames.append((key.time, self._asset_for(reader, reader.type.name)))
                    except Exception:
                        continue
                if frames:
                    return frames, max(length, frames[-1][0])
        return [], 0.0

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

    def _scripts(self):
        if self._script_reader is None:
            game_dir = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            if not os.path.isfile(os.path.join(game_dir, "GameAssembly.dll")) and not any(
                    n.endswith("_Data") for n in os.listdir(game_dir)):
                game_dir = os.path.dirname(game_dir)  # opened the _Data folder itself
            self._script_reader = ScriptReader(self.env, game_dir)
        return self._script_reader

    def _data(self, asset):
        with self.lock:
            tree, note = self._scripts().read(asset.ref)
        return json_safe(tree), note

    def video(self, asset):
        from UnityPy.helpers.ResourceReader import get_resource_data
        with self.lock:
            tree = asset.ref.read_typetree()
            res = tree.get("m_ExternalResources") or {}
            if not res.get("m_Size"):
                raise ValueError("This VideoClip has no video data in the game files.")
            data = get_resource_data(res["m_Source"], asset.ref.assets_file, res["m_Offset"], res["m_Size"])
        return bytes(data), asset.ext or "mp4"

    def audio(self, asset):
        clip = asset.ref.read()
        samples = clip.samples  # UnityPy converts through FMOD to WAV
        if not samples:
            raise ValueError("This AudioClip has no sound data in the game files.")
        _name, data = next(iter(samples.items()))
        return bytes(data), "wav"

    def stats(self, asset):
        if self._is_terrain(asset):
            obj = asset.ref["obj"]
            if asset.ref["type"] == "terrain_tex":
                return {"size": 0, "info": "blended", "sort": 0}
            res = int(asset.ref.get("resolution") or 0)
            return {"size": obj.byte_size, "info": f"terrain {res}\u00d7{res}" if res else "terrain",
                    "sort": res * res * 2}
        if asset.kind == "scene":
            n = len(asset.ref.get("transforms", [])) if asset.ref["type"] == "scene" else 0
            return {"size": asset.size, "info": f"{n:,} objects" if n else "prefab", "sort": n}
        obj = asset.ref
        stats = {"size": obj.byte_size}
        if asset.kind == "data":
            with self.lock:
                cls = script_class(obj)
            return {"size": obj.byte_size, "info": cls.rsplit(".", 1)[-1], "sort": obj.byte_size}
        if asset.kind in ("font", "video"):
            return {"size": obj.byte_size, "info": asset.ext.upper(), "sort": obj.byte_size}
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
        if self._is_terrain(asset):
            return [self.terrain(asset.ref["obj"])[1]]
        if asset.kind == "scene":
            return list(self._scene(asset)[1])
        _names, mats, _n = self._info(asset)
        out = []
        for mat in mats:
            refs = []
            for prop, tex_name, reader in mat["textures"]:
                role = ALBEDO if prop in ALBEDO_PROPS else NORMAL if prop in NORMAL_PROPS else OTHER
                refs.append(TextureRef(prop, tex_name, self._asset_for(reader, "Texture2D", tex_name), role))
            out.append(Material(mat["name"], refs, color=mat.get("color"), properties=mat.get("properties")))
        return out

    def describe(self, asset):
        if self._is_terrain(asset):
            rows = [("File", asset.source)]
            if asset.ref["type"] == "terrain":
                rows += self.terrain(asset.ref["obj"])[2]
            return rows
        if asset.kind == "scene":
            rows = [("File", asset.source)] + ([("Path", asset.path)] if asset.path else [])
            if asset.key in self._scenes:
                rows += self._scenes[asset.key][2]
            return rows
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
        if self._is_terrain(asset):
            return "", [], ""
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
            sprites = [sa for sa, _r in self._sprite_sheets().get(asset.key, [])]
            title = f"Used by {len(links)} model(s)"
            if sprites:
                title += f" · {len(sprites)} sprite(s) cut from this sheet"
                links = links + sorted(sprites, key=lambda a: a.name.lower())[:1000]
            return (title, links,
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

    @staticmethod
    def _ext(kind, obj):
        if kind == "font":
            try:
                data = obj.read().m_FontData
                return "otf" if bytes(data[:4]) == b"OTTO" else "ttf"
            except Exception:
                return "ttf"
        if kind == "video":
            try:
                path = obj.read_typetree().get("m_OriginalPath", "")
                return os.path.splitext(path)[1].lstrip(".").lower() or "mp4"
            except Exception:
                return "mp4"
        return {"text": "txt", "audio": "wav", "animation": "json", "data": "json"}.get(kind, "")

    MAX_PREFAB_SCAN = 200_000

    @staticmethod
    def _add_terrain(session, obj):
        try:
            name = obj.peek_name() or f"Terrain #{obj.path_id % 100000:05d}"
        except Exception:
            name = "Terrain"
        key = ("terrain",) + obj_key(obj.assets_file, obj.path_id)
        session.assets.append(Asset("model", f"Terrain: {name}", key, uid=f"terrain:{obj.assets_file.name}:{obj.path_id}",
                                    size=obj.byte_size, path=getattr(obj, "container", None) or "",
                                    source=obj.assets_file.name, ref={"type": "terrain", "obj": obj}))

    def _scenes_and_prefabs(self, env, session, transforms_by_file, prefab_containers, renderer_readers):
        """Add 'scene' assets: one per level file, one per prefab (bundle container or root object)."""
        from .unity_scene import is_scene_file, scene_name
        scene_paths = []
        for obj in env.objects:
            if obj.type.name == "BuildSettings":
                try:
                    tree = obj.read_typetree()
                    scene_paths = tree.get("scenes") or tree.get("m_Scenes") or []
                except Exception:
                    pass
                break
        added = 0
        for file_id, (assets_file, transforms) in transforms_by_file.items():
            if not is_scene_file(assets_file):
                continue
            name = scene_name(assets_file, scene_paths)
            key = ("scene", file_id)
            session.assets.append(Asset("scene", f"Scene: {name}", key, uid=f"scene:{assets_file.name}",
                                        size=sum(t.byte_size for t in transforms), path="", source=assets_file.name,
                                        ref={"type": "scene", "transforms": transforms}))
            added += 1
        # Only prefabs with something to see (skip audio/logic/UI prefabs): a MeshFilter or
        # SkinnedMeshRenderer inside the same bundle entry.
        visible = {getattr(r, "container", None) for r in renderer_readers}
        for container, gos in prefab_containers.items():
            if container not in visible:
                continue
            label = container[7:] if container.lower().startswith("assets/") else container
            label = label[:-7] if label.lower().endswith(".prefab") else label
            session.assets.append(Asset("scene", f"Prefab: {label}", ("prefab", container), uid=f"prefab:{container}",
                                        size=None, path=container, source=gos[0].assets_file.name,
                                        ref={"type": "prefab", "gameobjects": gos}))
            added += 1
        if not prefab_containers:
            added += self._classic_prefabs(session, transforms_by_file, renderer_readers)
        log.info("Found %d scene(s)/prefab(s)", added)

    def _classic_prefabs(self, session, transforms_by_file, renderer_readers):
        """Builds without bundles: root objects outside the scene files that have a mesh under them."""
        from .unity_scene import is_scene_file
        files = [(f, ts) for f, ts in transforms_by_file.values() if not is_scene_file(f)]
        if sum(len(ts) for _f, ts in files) > self.MAX_PREFAB_SCAN:
            log.info("Too many objects to look for prefabs without bundles - skipped")
            return 0
        father, go_of, node = {}, {}, {}
        for _f, ts in files:
            for t in ts:
                try:
                    tr = t.read()
                    k = obj_key(t.assets_file, t.path_id)
                    node[k] = t
                    fp = tr.m_Father
                    father[k] = obj_key(fp.deref().assets_file, fp.path_id) if fp.path_id else None
                    go = tr.m_GameObject
                    go_of[obj_key(go.deref().assets_file, go.path_id)] = k
                except Exception:
                    continue
        roots = set()
        for r in renderer_readers:
            if is_scene_file(r.assets_file):
                continue
            try:
                go = r.read().m_GameObject
                k = go_of.get(obj_key(go.deref().assets_file, go.path_id))
            except Exception:
                continue
            for _ in range(200):
                if k is None:
                    break
                if father.get(k) is None:
                    roots.add(k)
                    break
                k = father[k]
        added = 0
        for k in roots:
            t = node[k]
            try:
                name = t.read().m_GameObject.deref().peek_name() or "prefab"
            except Exception:
                name = "prefab"
            session.assets.append(Asset("scene", f"Prefab: {name}", ("root", k[1], id(t.assets_file)),
                                        uid=f"prefab:{t.assets_file.name}:{t.path_id}", size=None, path="",
                                        source=t.assets_file.name, ref={"type": "root", "transform": t}))
            added += 1
        return added

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
        transforms_by_file = {}   # id(assets file) -> (assets file, [Transform readers])
        prefab_containers = {}    # container path -> [GameObject readers]
        renderer_readers = []     # MeshFilter / SkinnedMeshRenderer (to find prefab roots in classic builds)
        for i, obj in enumerate(env.objects):
            type_name = obj.type.name
            if type_name in ("Transform", "RectTransform"):
                entry = transforms_by_file.setdefault(id(obj.assets_file), (obj.assets_file, []))
                entry[1].append(obj)
                continue
            if type_name == "GameObject":
                container = getattr(obj, "container", None)
                if container and container.lower().endswith(".prefab"):
                    prefab_containers.setdefault(container, []).append(obj)
                continue
            if type_name in ("MeshFilter", "SkinnedMeshRenderer"):
                renderer_readers.append(obj)
            if type_name == "TerrainData":
                self._add_terrain(session, obj)
                continue
            kind = TYPE_KINDS.get(type_name)
            if kind is None:
                continue
            try:
                name = obj.peek_name()
            except Exception:
                name = None
            if kind == "data" and not name:
                continue  # only named MonoBehaviours: data assets (ScriptableObjects), not components
            if kind == "font":
                try:
                    if not obj.read().m_FontData:
                        continue  # a reference to a built-in/OS font: nothing to show or export
                except Exception:
                    continue
            container = getattr(obj, "container", None)
            if not name:
                # Unnamed (common for combined/prefab meshes): name it after where it lives.
                stem = os.path.splitext(os.path.basename(container))[0] if container else type_name
                name = f"{stem} #{obj.path_id % 100000:05d}"
            key = obj_key(obj.assets_file, obj.path_id)
            asset = Asset(kind, name, key, uid=f"{obj.assets_file.name}:{obj.path_id}", size=obj.byte_size,
                          path=container or "", source=obj.assets_file.name, ref=obj,
                          ext=self._ext(kind, obj))
            session.assets.append(asset)
            session.by_key[key] = asset
            if i % 2000 == 0:
                progress(f"Indexing objects ... {i}")
        try:
            progress("Finding scenes and prefabs ...")
            self._scenes_and_prefabs(env, session, transforms_by_file, prefab_containers, renderer_readers)
        except Exception:
            log.exception("Listing scenes and prefabs failed")
        if failed:
            session.warnings.append(f"{failed} file(s) could not be loaded (see the console).")
        version = next((v for v in (getattr(f, "unity_version", "") for f in getattr(env, "assets", []))
                        if v and UNITY_VERSION_RE.fullmatch(v.encode()) and not v.startswith("0.0")), "")
        session.engine_version = version
        log.info("Unity load done in %.1fs (%d file(s) failed)", time.time() - started, failed)
        return session


PLUGIN = UnityPlugin()
