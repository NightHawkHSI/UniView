"""Unity scenes and prefabs as one 3D view: every renderer's mesh placed where the game puts it."""

import logging
import os
import re
import time

import numpy as np

from .sdk import ALBEDO, NORMAL, OTHER, Material, MeshData, TextureRef
from .unity_skin import trs

log = logging.getLogger("viewer.unity")

MAX_TRIANGLES = 6_000_000
TERRAIN_GRID = 257  # heightmap vertices per side for terrains inside scenes
FLIP = np.diag([-1.0, 1.0, 1.0, 1.0])  # Unity is left-handed; UniView meshes are x-flipped
SCENE_FILE = re.compile(r"^(level\d+|BuildPlayer-.*)$", re.I)


def is_scene_file(assets_file):
    return bool(SCENE_FILE.match(assets_file.name or ""))


def _ptr_key(ptr):
    try:
        if not ptr.path_id:
            return None
        reader = ptr.deref()
        return (id(reader.assets_file), reader.path_id), reader
    except Exception:
        return None


def _components(go):
    out = []
    for c in getattr(go, "m_Component", None) or getattr(go, "m_Components", None) or []:
        ptr = getattr(c, "component", None)
        if ptr is None and isinstance(c, (tuple, list)):
            ptr = c[-1]
        if ptr is None:
            ptr = c
        try:
            reader = ptr.deref()
            out.append((reader.type.name, reader))
        except Exception:
            continue
    return out


class SceneBuilder:
    """Collects placed meshes from a set of root transforms."""

    def __init__(self, session):
        self.session = session
        self.meshes = {}      # mesh key -> MeshData (or None)
        self.materials = {}   # material key -> Material
        self.points, self.uvs, self.subs, self.slots = [], [], [], []
        self.material_list, self.material_index = [], {}
        self.count = 0
        self.triangles = 0
        self.objects = self.renderers = self.skipped = 0
        self.lod_skip = set()
        self.terrains = 0

    def _mesh(self, key, reader):
        if key not in self.meshes:
            from .unity import mesh_to_meshdata
            try:
                self.meshes[key] = mesh_to_meshdata(reader.read())
            except Exception as e:
                log.debug("Scene mesh %s: %s", reader.peek_name(), e)
                self.meshes[key] = None
        return self.meshes[key]

    def _material(self, ptr):
        found = _ptr_key(ptr)
        if found is None:
            return None
        key, reader = found
        if key not in self.material_index:
            from .unity import ALBEDO_PROPS, NORMAL_PROPS, read_material
            try:
                mat = read_material(reader.read())
            except Exception:
                return None
            refs = []
            for prop, tex_name, tex_reader in mat["textures"]:
                role = ALBEDO if prop in ALBEDO_PROPS else NORMAL if prop in NORMAL_PROPS else OTHER
                refs.append(TextureRef(prop, tex_name, self.session._asset_for(tex_reader, "Texture2D", tex_name), role))
            self.material_index[key] = len(self.material_list)
            self.material_list.append(Material(mat["name"], refs, color=mat["color"], properties=mat["properties"]))
        return self.material_index[key]

    def _add(self, md, matrix, materials, submesh_filter=None):
        """Place a mesh: matrix is Unity-space local-to-world; materials are indices into material_list."""
        m = FLIP @ matrix @ FLIP
        rot, pos = m[:3, :3], m[:3, 3]
        mirrored = np.linalg.det(rot) < 0
        picked = []  # (triangles, material slot)
        for j, tris in enumerate(md.submeshes):
            slot = md.material_slots[j] if j < len(md.material_slots) else j
            if submesh_filter is not None:
                if not submesh_filter[0] <= slot < submesh_filter[0] + submesh_filter[1]:
                    continue
                slot -= submesh_filter[0]
            picked.append((tris, slot))
        n_tris = sum(len(t) for t, _s in picked)
        if not n_tris:
            return True
        if self.triangles + n_tris > MAX_TRIANGLES:
            return False
        points = md.points
        uv = next(iter(md.uvs.values()), None)
        if submesh_filter is not None:
            # A static batch holds many objects' vertices: keep only this renderer's.
            used = np.unique(np.concatenate([t for t, _s in picked]))
            remap = np.zeros(len(points), np.int64)
            remap[used] = np.arange(len(used))
            points = points[used]
            uv = uv[used] if uv is not None else None
            picked = [(remap[t], s) for t, s in picked]
        pts = (points.astype(np.float64) @ rot.T + pos).astype(np.float32)
        for tris, slot in picked:
            self.subs.append((tris[:, ::-1] if mirrored else tris) + self.count)
            mat = materials[min(slot, len(materials) - 1)] if materials else None
            self.slots.append(mat if mat is not None else self._default_material())
        self.triangles += n_tris
        self.points.append(pts)
        self.uvs.append(uv if uv is not None else np.zeros((len(pts), 2), np.float32))
        self.count += len(pts)
        return True

    def _terrain(self, reader, matrix):
        """A Terrain component: its heightmap mesh with the blended layers, at the object's position."""
        try:
            terrain = reader.read()
            if not getattr(terrain, "m_Enabled", True):
                return
            found = _ptr_key(terrain.m_TerrainData)
            if found is None:
                return
            md, material, _info, _res = self.session.terrain(found[1], grid=TERRAIN_GRID)
        except Exception as e:
            log.debug("Scene terrain: %s", e)
            return
        key = ("terrain",) + found[0]
        if key not in self.material_index:
            self.material_index[key] = len(self.material_list)
            self.material_list.append(material)
        # Terrains only move (Unity ignores their rotation and scale).
        move = np.eye(4)
        move[:3, 3] = matrix[:3, 3]
        if self._add(md, move, [self.material_index[key]]):
            self.renderers += 1
            self.terrains += 1

    def _default_material(self):
        if None not in self.material_index:
            self.material_index[None] = len(self.material_list)
            self.material_list.append(Material("(no material)", []))
        return self.material_index[None]

    def visit(self, transform_reader, parent_matrix, parent_active=True, depth=0):
        """Walk a transform and its children (depth first)."""
        if depth > 200 or self.triangles >= MAX_TRIANGLES:
            return
        try:
            t = transform_reader.read()
            go = t.m_GameObject.deref().read()
        except Exception:
            return
        p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
        matrix = parent_matrix @ trs((p.x, p.y, p.z), (r.x, r.y, r.z, r.w), (s.x, s.y, s.z))
        active = parent_active and bool(getattr(go, "m_IsActive", True))
        self.objects += 1
        if active:
            comps = _components(go)
            names = {n for n, _r in comps}
            for name, reader in comps:
                if name == "LODGroup":
                    try:
                        for level, lod in enumerate(reader.read().m_LODs):
                            if level == 0:
                                continue
                            for lr in lod.renderers:
                                found = _ptr_key(lr.renderer)
                                if found:
                                    self.lod_skip.add(found[0])
                    except Exception:
                        pass
            for name, reader in comps:
                if name == "Terrain":
                    self._terrain(reader, matrix)
                    continue
                if name not in ("MeshRenderer", "SkinnedMeshRenderer"):
                    continue
                if (id(reader.assets_file), reader.path_id) in self.lod_skip:
                    self.skipped += 1
                    continue
                try:
                    renderer = reader.read()
                except Exception:
                    continue
                if not getattr(renderer, "m_Enabled", True):
                    continue
                mesh_ptr = None
                if name == "SkinnedMeshRenderer":
                    mesh_ptr = renderer.m_Mesh
                elif "MeshFilter" in names:
                    mf = next(r for n, r in comps if n == "MeshFilter")
                    try:
                        mesh_ptr = mf.read().m_Mesh
                    except Exception:
                        mesh_ptr = None
                found = _ptr_key(mesh_ptr) if mesh_ptr is not None else None
                if found is None:
                    continue
                md = self._mesh(*found)
                if md is None:
                    continue
                materials = [self._material(ptr) for ptr in (renderer.m_Materials or [])]
                batch = getattr(renderer, "m_StaticBatchInfo", None)
                first = getattr(batch, "firstSubMesh", 0) if batch is not None else 0
                count = getattr(batch, "subMeshCount", 0) if batch is not None else 0
                if count:
                    # Static batching: the combined mesh is already in world space.
                    self._add(md, np.eye(4), materials, (first, count))
                else:
                    self._add(md, matrix, materials)
                self.renderers += 1
        for child in t.m_Children or []:
            try:
                self.visit(child.deref(), matrix, active, depth + 1)
            except Exception:
                continue

    def result(self, name):
        if not self.subs:
            raise ValueError("Nothing visible here (no active mesh renderers).")
        md = MeshData(np.concatenate(self.points), self.subs, uvs={"UV0": np.concatenate(self.uvs)},
                      material_slots=self.slots, name=name)
        return md, self.material_list


def build(session, roots, name):
    """roots: Transform ObjectReaders to start from. Returns (MeshData, materials, info rows)."""
    started = time.time()
    builder = SceneBuilder(session)
    for root in roots:
        builder.visit(root, np.eye(4))
        if builder.triangles >= MAX_TRIANGLES:
            break
    md, materials = builder.result(name)
    info = [("Objects", f"{builder.objects:,}"), ("Mesh renderers", f"{builder.renderers:,}"),
            ("Distinct meshes", f"{sum(1 for m in builder.meshes.values() if m is not None):,}")]
    if builder.terrains:
        info.append(("Terrains", f"{builder.terrains:,}"))
    if builder.skipped:
        info.append(("Skipped", f"{builder.skipped:,} lower-detail LOD renderers"))
    if builder.triangles >= MAX_TRIANGLES:
        info.append(("Note", f"stopped at {MAX_TRIANGLES:,} triangles"))
    log.info("Built '%s': %d objects, %d renderers, %s tris in %.1fs", name, builder.objects, builder.renderers,
             f"{builder.triangles:,}", time.time() - started)
    return md, materials, info


def scene_name(assets_file, scene_paths):
    m = re.match(r"level(\d+)$", assets_file.name or "", re.I)
    if m and int(m.group(1)) < len(scene_paths):
        return os.path.splitext(os.path.basename(scene_paths[int(m.group(1))]))[0]
    if (assets_file.name or "").lower().startswith("buildplayer-"):
        return assets_file.name[len("BuildPlayer-"):].split(".")[0]
    return assets_file.name
