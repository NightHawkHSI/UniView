"""Unity scenes and prefabs as one 3D view: every renderer's mesh placed where the game puts it."""

import logging
import os
import re
import time

import numpy as np

from .sdk import ALBEDO, NORMAL, OTHER, Material, MeshData, TextureRef, view_option
from .unity_skin import trs

log = logging.getLogger("viewer.unity")

MAX_TRIANGLES = 6_000_000
TERRAIN_GRID = 257  # heightmap vertices per side for terrains inside scenes
FLIP = np.diag([-1.0, 1.0, 1.0, 1.0])  # Unity is left-handed; UniView meshes are x-flipped
SCENE_FILE = re.compile(r"^(level\d+|BuildPlayer-.*)$", re.I)


def is_scene_file(assets_file):
    return bool(SCENE_FILE.match(assets_file.name or ""))


def sprite_quad(rect, pivot, ppu, tex_rect, offset, tex_size, draw_mode=0, size=None, flip_x=False, flip_y=False):
    """A sprite as a quad in Unity space: (points (4, 3), uvs (4, 2), triangles (4, 3) - both sides).
    rect (w, h): the sprite's rect in pixels; pivot (0..1); ppu: pixels per unit; tex_rect (x, y, w, h): the
    pixels it uses in its texture (trimmed / packed), offset (x, y): where those sit inside rect; tex_size (w, h).
    draw_mode 1/2 (sliced/tiled) stretch the whole sprite to size (w, h) in units instead."""
    ppu = ppu or 100.0
    tw, th = tex_size
    tx, ty, tw_px, th_px = tex_rect
    if draw_mode and size is not None and size[0] > 0 and size[1] > 0:
        x0, y0 = -pivot[0] * size[0], -pivot[1] * size[1]
        x1, y1 = x0 + size[0], y0 + size[1]
    else:
        x0 = (offset[0] - pivot[0] * rect[0]) / ppu
        y0 = (offset[1] - pivot[1] * rect[1]) / ppu
        x1, y1 = x0 + tw_px / ppu, y0 + th_px / ppu
    if flip_x:
        x0, x1 = -x1, -x0
    if flip_y:
        y0, y1 = -y1, -y0
    u0, u1 = tx / tw, (tx + tw_px) / tw
    v0, v1 = ty / th, (ty + th_px) / th
    if flip_x:
        u0, u1 = u1, u0
    if flip_y:
        v0, v1 = v1, v0
    points = np.array([[x0, y0, 0], [x1, y0, 0], [x1, y1, 0], [x0, y1, 0]], np.float32)
    uvs = np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]], np.float32)
    tris = np.array([[0, 1, 2], [0, 2, 3], [0, 2, 1], [0, 3, 2]], np.int64)
    return points, uvs, tris


# ---------------------------------------------------------------------------- particle systems and lines
# No simulation: a particle system is shown as a few still "puffs" (three crossed quads each, so they show from
# any side) where its particles would be about halfway through their life.

MAX_PUFFS = 6


def curve_value(c, default=0.0):
    """Typical value of a MinMaxCurve dict: the constant, the middle of two constants, or scalar x curve."""
    if not isinstance(c, dict):
        return default
    state = int(c.get("minMaxState", 0) or 0)
    scalar = float(c.get("scalar", default))
    if state == 3:
        return (scalar + float(c.get("minScalar", scalar))) / 2
    if state in (1, 2):
        keys = (c.get("maxCurve") or {}).get("m_Curve") or []
        values = [float(k.get("value", 1.0)) for k in keys if isinstance(k, dict)]
        return scalar * (sum(values) / len(values) if values else 1.0)
    return scalar


def gradient_color(g):
    """Typical (r, g, b, a) of a MinMaxGradient dict: its color(s), else its gradient's first key."""
    def rgba(d):
        return tuple(float(d.get(k, 1.0)) for k in "rgba")
    if not isinstance(g, dict):
        return (1.0, 1.0, 1.0, 1.0)
    state = int(g.get("minMaxState", 0) or 0)
    if state == 0 and g.get("maxColor"):
        return rgba(g["maxColor"])
    if state == 2 and g.get("maxColor"):
        lo, hi = rgba(g.get("minColor") or g["maxColor"]), rgba(g["maxColor"])
        return tuple((a + b) / 2 for a, b in zip(lo, hi))
    key = (g.get("maxGradient") or {}).get("key0")
    return rgba(key) if key else (1.0, 1.0, 1.0, 1.0)


def euler_matrix(x, y, z):
    """Unity euler angles (degrees; applied z, then x, then y) as a 3x3 rotation."""
    x, y, z = np.radians([x, y, z])
    rx = np.array([[1, 0, 0], [0, np.cos(x), -np.sin(x)], [0, np.sin(x), np.cos(x)]])
    ry = np.array([[np.cos(y), 0, np.sin(y)], [0, 1, 0], [-np.sin(y), 0, np.cos(y)]])
    rz = np.array([[np.cos(z), -np.sin(z), 0], [np.sin(z), np.cos(z), 0], [0, 0, 1]])
    return ry @ rx @ rz


def _vec(d, default):
    d = d or {}
    return np.array([float(d.get(a, default)) for a in "xyz"])


def particle_puffs(ps, seed=0):
    """(centres (k, 3) in the emitter's space, size) of a ParticleSystem type tree's still puffs."""
    init = ps.get("InitialModule") or {}
    shape = ps.get("ShapeModule") or {}
    emission = ps.get("EmissionModule") or {}
    size = max(curve_value(init.get("startSize"), 1.0), 1e-3)
    life = max(curve_value(init.get("startLifetime"), 5.0), 0.0)
    speed = curve_value(init.get("startSpeed"), 5.0)
    rate = curve_value(emission.get("rateOverTime"), 10.0) if emission.get("enabled", True) else 0.0
    bursts = int(emission.get("m_BurstCount", 0) or 0)
    k = int(np.clip(round(rate * life + (3 if bursts else 0)), 1, MAX_PUFFS))
    travel = speed * life * 0.5
    rng = np.random.default_rng(seed & 0xFFFFFFFF)
    if shape.get("enabled", True):
        kind = int(shape.get("type", 4) or 0)
        r = shape.get("radius", 1.0)
        radius = float(r.get("value", 1.0) if isinstance(r, dict) else r or 0.0)
        scale, pos = _vec(shape.get("m_Scale"), 1.0), _vec(shape.get("m_Position"), 0.0)
        rot = euler_matrix(*_vec(shape.get("m_Rotation"), 0.0))
        angle = np.radians(float(shape.get("angle", 25.0) or 0.0))
    else:
        kind, radius, scale, pos, rot, angle = 4, 0.0, np.ones(3), np.zeros(3), np.eye(3), 0.0
    out = []
    for i in range(k):
        d = travel * (i + 0.5) / k if k > 1 else travel
        if kind in (4, 8, 9):  # cones: along +z, widening with the cone angle
            spread = radius + abs(d) * np.tan(angle)
            a, r = rng.uniform(0, 2 * np.pi), spread * np.sqrt(rng.uniform(0, 1)) * 0.7
            p = np.array([r * np.cos(a), r * np.sin(a), d])
        elif kind in (5, 15, 16):  # boxes: anywhere in the box, moving along +z
            p = rng.uniform(-0.5, 0.5, 3) + np.array([0.0, 0.0, d])
        else:  # spheres, hemispheres, circles, meshes...: outwards
            v = rng.normal(size=3)
            v /= np.linalg.norm(v) or 1.0
            if kind in (2, 3):
                v[2] = abs(v[2])
            p = v * (radius * rng.uniform(0, 1) + abs(d))
        out.append(rot @ (p * scale) + pos)
    return np.array(out, np.float64).reshape(-1, 3), size


def crossed_quads(centres, size, uv_rect=(0.0, 0.0, 1.0, 1.0)):
    """(points, uvs, triangles) of three crossed, double-sided quads per centre."""
    h = size / 2
    u0, v0, u1, v1 = uv_rect
    planes = ([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)],   # facing z
              [(0, -h, -h), (0, -h, h), (0, h, h), (0, h, -h)],   # facing x
              [(-h, 0, -h), (h, 0, -h), (h, 0, h), (-h, 0, h)])   # facing y
    quad_uv = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
    pts, uvs, tris = [], [], []
    for c in centres:
        for plane in planes:
            base = len(pts)
            pts += [np.add(c, p) for p in plane]
            uvs += quad_uv
            tris += [[base, base + 1, base + 2], [base, base + 2, base + 3],
                     [base, base + 2, base + 1], [base, base + 3, base + 2]]
    return (np.array(pts, np.float32).reshape(-1, 3), np.array(uvs, np.float32).reshape(-1, 2),
            np.array(tris, np.int64).reshape(-1, 3))


def ribbon(positions, width, loop=False):
    """(points, uvs, triangles) of a line as two crossed double-sided strips, or None under 2 points."""
    line = np.asarray(positions, np.float64).reshape(-1, 3)
    if loop and len(line) > 2:
        line = np.vstack([line, line[:1]])
    h = max(width, 1e-4) / 2
    pts, uvs, tris = [], [], []
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        d = b - a
        length = np.linalg.norm(d)
        if length < 1e-9:
            continue
        d /= length
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
        n1 = np.cross(d, up)
        n1 /= np.linalg.norm(n1)
        n2 = np.cross(d, n1)
        for n in (n1, n2):
            base = len(pts)
            pts += [a - n * h, b - n * h, b + n * h, a + n * h]
            uvs += [(0, 0), (1, 0), (1, 1), (0, 1)]
            tris += [[base, base + 1, base + 2], [base, base + 2, base + 3],
                     [base, base + 2, base + 1], [base, base + 3, base + 2]]
    if not pts:
        return None
    return np.array(pts, np.float32), np.array(uvs, np.float32), np.array(tris, np.int64)


class _Deref:
    """An ObjectReader dressed as the PPtr _ptr_key() expects."""

    def __init__(self, reader):
        self.reader, self.path_id = reader, reader.path_id

    def deref(self):
        return self.reader


# ---------------------------------------------------------------------------- gizmos
# Wire shapes for things that have no mesh: colliders, light ranges, camera views, sound sources. Each helper
# returns (points (N, 3), segments (M, 2)) in the object's own (Unity) space.

GIZMO_KINDS = ("collider", "trigger", "light", "camera", "audio")
MAX_GIZMO_SEGMENTS = 300_000


def box_lines(center, size):
    c, h = np.asarray(center, float), np.abs(np.asarray(size, float)) / 2
    corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float) * h + c
    segs = [(i, j) for i in range(8) for j in range(i + 1, 8) if bin(i ^ j).count("1") == 1]
    return corners, np.array(segs, np.int64)


def circle_lines(center, radius, axis=2, n=32):
    """A circle around one axis (0 x, 1 y, 2 z)."""
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    u, v = [k for k in range(3) if k != axis]
    pts = np.zeros((n, 3))
    pts[:, u], pts[:, v] = radius * np.cos(a), radius * np.sin(a)
    pts += np.asarray(center, float)
    return pts, np.array([(i, (i + 1) % n) for i in range(n)], np.int64)


def join(*parts):
    """Several (points, segments) as one."""
    pts, segs, base = [], [], 0
    for p, s in parts:
        pts.append(np.asarray(p, float).reshape(-1, 3))
        segs.append(np.asarray(s, np.int64).reshape(-1, 2) + base)
        base += len(pts[-1])
    if not pts:
        return np.zeros((0, 3)), np.zeros((0, 2), np.int64)
    return np.concatenate(pts), np.concatenate(segs)


def sphere_lines(center, radius):
    return join(*(circle_lines(center, radius, axis) for axis in range(3)))


def capsule_lines(center, radius, height, direction=1):
    """A capsule along an axis (0 x, 1 y, 2 z): its two end spheres and the four lines between them."""
    c = np.asarray(center, float)
    half = max(height / 2 - radius, 0.0)
    offset = np.zeros(3)
    offset[direction] = half
    ends = [c + offset, c - offset]
    parts = [sphere_lines(e, radius) for e in ends]
    u, v = [k for k in range(3) if k != direction]
    for k, sign in ((u, 1), (u, -1), (v, 1), (v, -1)):
        side = np.zeros(3)
        side[k] = sign * radius
        parts.append(([ends[0] + side, ends[1] + side], [(0, 1)]))
    return join(*parts)


def cone_lines(length, angle_deg):
    """A spot light's cone: from the origin along +z, angle_deg wide."""
    r = length * np.tan(np.radians(min(angle_deg, 179.0)) / 2)
    ring = circle_lines((0, 0, length), r, 2)
    edges = ([(0, 0, 0), (r, 0, length), (-r, 0, length), (0, r, length), (0, -r, length)],
             [(0, 1), (0, 2), (0, 3), (0, 4)])
    return join(ring, edges)


def arrow_lines(length=1.5):
    """A directional light: an arrow along +z."""
    h = length * 0.15
    return (np.array([(0, 0, 0), (0, 0, length), (h, 0, length - h), (-h, 0, length - h), (0, h, length - h),
                      (0, -h, length - h)], float), np.array([(0, 1), (1, 2), (1, 3), (1, 4), (1, 5)], np.int64))


def frustum_lines(fov_deg, aspect, near, far, ortho_size=None):
    """A camera's view volume along +z (perspective, or orthographic when ortho_size is given)."""
    def rect(d, half_h):
        half_w = half_h * aspect
        return [(-half_w, -half_h, d), (half_w, -half_h, d), (half_w, half_h, d), (-half_w, half_h, d)]
    if ortho_size is not None:
        pts = rect(near, ortho_size) + rect(far, ortho_size)
    else:
        t = np.tan(np.radians(fov_deg) / 2)
        pts = rect(near, near * t) + rect(far, far * t)
    segs = [(i, (i + 1) % 4) for i in range(4)] + [(4 + i, 4 + (i + 1) % 4) for i in range(4)] + \
        [(i, i + 4) for i in range(4)]
    return np.array(pts, float), np.array(segs, np.int64)


def marker_lines(size=0.25):
    """A small diamond (sound sources and other point-like things)."""
    pts = np.array([(size, 0, 0), (-size, 0, 0), (0, size, 0), (0, -size, 0), (0, 0, size), (0, 0, -size)], float)
    segs = [(a, b) for a in range(2) for b in range(2, 6)] + [(2, 4), (2, 5), (3, 4), (3, 5)]
    return pts, np.array(segs, np.int64)


def _v3(d, default=0.0):
    d = d or {}
    return np.array([float(d.get(a, default)) for a in "xyz"])


def component_gizmo(name, tree):
    """(kind, points, segments) for a built-in component's type tree, or None."""
    if not tree.get("m_Enabled", 1):
        return None
    trigger = "trigger" if tree.get("m_IsTrigger") else "collider"
    if name == "BoxCollider":
        return (trigger,) + box_lines(_v3(tree.get("m_Center")), _v3(tree.get("m_Size"), 1.0))
    if name == "SphereCollider":
        return (trigger,) + sphere_lines(_v3(tree.get("m_Center")), float(tree.get("m_Radius", 0.5)))
    if name in ("CapsuleCollider", "CharacterController"):
        return (trigger,) + capsule_lines(_v3(tree.get("m_Center")), float(tree.get("m_Radius", 0.5)),
                                          float(tree.get("m_Height", 2.0)), int(tree.get("m_Direction", 1)))
    if name in ("BoxCollider2D", "CircleCollider2D"):
        off = tree.get("m_Offset") or {}
        c = (float(off.get("x", 0.0)), float(off.get("y", 0.0)), 0.0)
        if name == "CircleCollider2D":
            return (trigger,) + circle_lines(c, float(tree.get("m_Radius", 0.5)), 2)
        size = tree.get("m_Size") or {}
        return (trigger,) + box_lines(c, (float(size.get("x", 1.0)), float(size.get("y", 1.0)), 0.0))
    if name == "Light":
        kind = int(tree.get("m_Type", 2))
        rng = float(tree.get("m_Range", 10.0))
        if kind == 0:
            return ("light",) + cone_lines(rng, float(tree.get("m_SpotAngle", 30.0)))
        if kind == 1:
            return ("light",) + arrow_lines()
        if kind == 2:
            return ("light",) + sphere_lines((0, 0, 0), rng)
        return ("light",) + marker_lines()
    if name == "Camera":
        near = float(tree.get("near clip plane", 0.3))
        far = min(float(tree.get("far clip plane", 1000.0)), near + 5.0)  # the whole view would swamp the scene
        ortho = float(tree.get("orthographic size", 5.0)) if tree.get("orthographic") else None
        return ("camera",) + frustum_lines(float(tree.get("field of view", 60.0)), 16 / 9, near, far, ortho)
    if name == "AudioSource":
        return ("audio",) + marker_lines()
    return None


GIZMO_COMPONENTS = {"BoxCollider", "SphereCollider", "CapsuleCollider", "CharacterController", "BoxCollider2D",
                    "CircleCollider2D", "MeshCollider", "Light", "Camera", "AudioSource"}


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
        self.sprites = {}     # sprite key -> (type tree, texture size, texture Asset) or None
        self.sprite_count = 0
        self.effects = 0      # particle systems and lines drawn
        self.gizmos = {}      # kind -> [(points in UniView space, segments)]
        self.gizmo_segments = 0
        self.show_effects = view_option("show_effects")
        self.hide_lods = view_option("hide_lods")
        self.hide_inactive = view_option("hide_inactive")

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
            if not getattr(terrain, "m_Enabled", True) and self.hide_inactive:
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

    def _sprite(self, reader, matrix):
        """A SpriteRenderer: its sprite as a textured quad (both sides) at the object."""
        try:
            renderer = reader.read_typetree()
            if not renderer.get("m_Enabled", 1) and self.hide_inactive:
                return
            ptr = renderer.get("m_Sprite") or {}
            if not ptr.get("m_PathID"):
                return
            target = self.session.finder._file(reader.assets_file, ptr.get("m_FileID", 0))
            sprite = target.objects.get(ptr["m_PathID"]) if target is not None else None
            if sprite is None:
                return
            key = ("sprite", id(sprite.assets_file), sprite.path_id)
            if key not in self.sprites:
                self.sprites[key] = self._sprite_data(sprite)
            data = self.sprites[key]
            if data is None:
                return
            st, tex_size, tex_asset = data
            rd = st.get("m_RD") or {}
            rect, pivot = st["m_Rect"], st.get("m_Pivot") or {"x": 0.5, "y": 0.5}
            tex_rect = rd.get("textureRect") or rect
            offset = rd.get("textureRectOffset") or {"x": 0, "y": 0}
            size = renderer.get("m_Size") or {}
            points, uvs, tris = sprite_quad(
                (rect["width"], rect["height"]), (pivot["x"], pivot["y"]), st.get("m_PixelsToUnits", 100.0),
                (tex_rect["x"], tex_rect["y"], tex_rect["width"], tex_rect["height"]), (offset["x"], offset["y"]),
                tex_size, int(renderer.get("m_DrawMode", 0) or 0), (size.get("x", 0), size.get("y", 0)),
                bool(renderer.get("m_FlipX", 0)), bool(renderer.get("m_FlipY", 0)))
        except Exception as e:
            log.debug("Scene sprite: %s", e)
            return
        c = renderer.get("m_Color") or {}
        color = tuple(float(c.get(k, 1.0)) for k in "rgba")
        if color[3] <= 0.01:
            return  # fully transparent (fades and overlays the game turns on later)
        mkey = key + (color,)
        if mkey not in self.material_index:
            self.material_index[mkey] = len(self.material_list)
            refs = [TextureRef("_MainTex", tex_asset.name, tex_asset, ALBEDO)] if tex_asset is not None else []
            self.material_list.append(Material(st.get("m_Name") or "sprite", refs, color=color))
        points[:, 0] *= -1  # UniView meshes are x-flipped (see FLIP)
        md = MeshData(points, [tris], uvs={"UV0": uvs})
        if self._add(md, matrix, [self.material_index[mkey]]):
            self.renderers += 1
            self.sprite_count += 1

    def _sprite_data(self, sprite):
        """(Sprite type tree, texture size, texture Asset) or None."""
        try:
            st = sprite.read_typetree()
            tex_ptr = (st.get("m_RD") or {}).get("texture") or {}
            target = self.session.finder._file(sprite.assets_file, tex_ptr.get("m_FileID", 0))
            tex = target.objects.get(tex_ptr.get("m_PathID")) if target is not None else None
            if tex is None:
                return None
            header = tex.read()
            size = (float(header.m_Width or 1), float(header.m_Height or 1))
            return st, size, self.session._asset_for(tex, "Texture2D")
        except Exception as e:
            log.debug("Sprite %s: %s", sprite.peek_name(), e)
            return None

    def _ref(self, reader, ptr):
        """ObjectReader a PPtr dict of reader's type tree points at, or None."""
        if not isinstance(ptr, dict) or not ptr.get("m_PathID"):
            return None
        target = self.session.finder._file(reader.assets_file, ptr.get("m_FileID", 0))
        return target.objects.get(ptr["m_PathID"]) if target is not None else None

    def _tinted(self, material_reader, color):
        """Index of a material (ObjectReader or None) multiplied by a color (cached)."""
        index = None
        if material_reader is not None:
            index = self._material(_Deref(material_reader))
        if index is None:
            index = self._default_material()
        color = tuple(round(float(c), 3) for c in color)
        if color == (1.0, 1.0, 1.0, 1.0):
            return index
        key = ("tint", index, color)
        if key not in self.material_index:
            base = self.material_list[index]
            own = base.color or (1.0, 1.0, 1.0, 1.0)
            self.material_index[key] = len(self.material_list)
            self.material_list.append(Material(base.name, base.textures,
                                               tuple(a * b for a, b in zip(own, color)), base.properties))
        return self.material_index[key]

    def _add_local(self, points, uvs, tris, matrix, material):
        """Place geometry built in the object's own (Unity) space."""
        points = np.array(points, np.float32)
        points[:, 0] *= -1  # UniView meshes are x-flipped (see FLIP)
        return self._add(MeshData(points, [tris], uvs={"UV0": uvs}), matrix, [material])

    def _particles(self, ps_reader, renderer_reader, matrix):
        """A ParticleSystem as a few still puffs (or its particle mesh) where the particles would be."""
        try:
            if renderer_reader is None:
                return
            renderer = renderer_reader.read_typetree()
            if not renderer.get("m_Enabled", 1):
                return
            ps = ps_reader.read_typetree()
            centres, size = particle_puffs(ps, ps_reader.path_id)
            mats = renderer.get("m_Materials") or []
            material = self._tinted(self._ref(renderer_reader, mats[0]) if mats else None,
                                    gradient_color((ps.get("InitialModule") or {}).get("startColor")))
            mesh = self._ref(renderer_reader, renderer.get("m_Mesh"))
            if int(renderer.get("m_RenderMode", 0) or 0) == 4 and mesh is not None:
                md = self._mesh((id(mesh.assets_file), mesh.path_id), mesh)
                if md is not None:
                    for c in centres:
                        place = np.eye(4)
                        place[:3, :3] *= size
                        place[:3, 3] = c
                        self._add(md, matrix @ place, [material])
                    self.effects += 1
                    return
            uv = (0.0, 0.0, 1.0, 1.0)
            sheet = ps.get("UVModule") or {}
            tx, ty = int(sheet.get("tilesX", 1) or 1), int(sheet.get("tilesY", 1) or 1)
            if sheet.get("enabled") and tx * ty > 1:
                uv = (0.0, 1.0 - 1.0 / ty, 1.0 / tx, 1.0)  # the first frame (top-left tile)
            points, uvs, tris = crossed_quads(centres, size, uv)
        except Exception as e:
            log.debug("Scene particles: %s", e)
            return
        if self._add_local(points, uvs, tris, matrix, material):
            self.effects += 1

    def _line(self, reader, matrix):
        """A LineRenderer as crossed strips along its points."""
        try:
            line = reader.read_typetree()
            if not line.get("m_Enabled", 1) and self.hide_inactive:
                return
            positions = [(p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0)) for p in line.get("m_Positions") or []]
            params = line.get("m_Parameters") or {}
            keys = (params.get("widthCurve") or {}).get("m_Curve") or []
            width = float(params.get("widthMultiplier", 1.0)) * (float(keys[0].get("value", 1.0)) if keys else 1.0)
            built = ribbon(positions, width, bool(line.get("m_Loop", params.get("m_Loop", 0))))
            if built is None:
                return
            grad = params.get("colorGradient") or {}
            color = tuple(float((grad.get("key0") or {}).get(k, 1.0)) for k in "rgba")
            mats = line.get("m_Materials") or []
            material = self._tinted(self._ref(reader, mats[0]) if mats else None, color)
        except Exception as e:
            log.debug("Scene line: %s", e)
            return
        place = np.eye(4) if line.get("m_UseWorldSpace", 1) else matrix
        if self._add_local(*built, place, material):
            self.effects += 1

    def _gizmo(self, name, reader, matrix):
        """Wire shape of a collider / light / camera / sound source, kept for the gizmo overlay."""
        if self.gizmo_segments >= MAX_GIZMO_SEGMENTS:
            return
        try:
            tree = reader.read_typetree()
            if name == "MeshCollider":
                mesh = self._ref(reader, tree.get("m_Mesh"))
                md = self._mesh((id(mesh.assets_file), mesh.path_id), mesh) if mesh is not None else None
                if md is None or not len(md.points) or not tree.get("m_Enabled", 1):
                    return
                pts = md.points.astype(float) * [-1, 1, 1]  # back to Unity space
                lo, hi = pts.min(0), pts.max(0)
                found = ("trigger" if tree.get("m_IsTrigger") else "collider",) + box_lines((lo + hi) / 2, hi - lo)
            else:
                found = component_gizmo(name, tree)
        except Exception as e:
            log.debug("Scene gizmo %s: %s", name, e)
            return
        if found is None or not len(found[1]):
            return
        kind, pts, segs = found
        world = np.asarray(pts, float) @ matrix[:3, :3].T + matrix[:3, 3]
        world[:, 0] *= -1  # UniView space is x-flipped
        self.gizmos.setdefault(kind, []).append((world.astype(np.float32), np.asarray(segs, np.int64)))
        self.gizmo_segments += len(segs)

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
        active = parent_active and (bool(getattr(go, "m_IsActive", True)) or not self.hide_inactive)
        self.objects += 1
        if active:
            comps = _components(go)
            names = {n for n, _r in comps}
            for name, reader in comps:
                if name == "LODGroup" and self.hide_lods:
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
                if name in GIZMO_COMPONENTS:
                    self._gizmo(name, reader, matrix)
                    if name != "Light":
                        continue
                if name == "Terrain":
                    self._terrain(reader, matrix)
                    continue
                if name == "SpriteRenderer":
                    self._sprite(reader, matrix)
                    continue
                if name == "ParticleSystem" and self.show_effects:
                    self._particles(reader, next((r for n, r in comps if n == "ParticleSystemRenderer"), None), matrix)
                    continue
                if name == "LineRenderer" and self.show_effects:
                    self._line(reader, matrix)
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
                if not getattr(renderer, "m_Enabled", True) and self.hide_inactive:
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
        gizmos = {}
        for kind, parts in self.gizmos.items():
            pts, segs = join(*parts)
            gizmos[kind] = (pts.astype(np.float32), segs)
        if not self.subs and not gizmos:
            raise ValueError("Nothing visible here (no active mesh or sprite renderers, colliders or lights).")
        if not self.subs:
            # Only colliders / lights...: two invisible (flat) triangles at the corners give the view its size.
            allpts = np.concatenate([p for p, _s in gizmos.values()])
            lo, hi = allpts.min(0), allpts.max(0)
            self.points.append(np.array([lo, lo, lo, hi, hi, hi], np.float32))
            self.uvs.append(np.zeros((6, 2), np.float32))
            self.subs.append(np.array([[0, 1, 2], [3, 4, 5]], np.int64))
            self.slots.append(self._default_material())
        md = MeshData(np.concatenate(self.points), self.subs, uvs={"UV0": np.concatenate(self.uvs)},
                      material_slots=self.slots, name=name)
        md.gizmos = gizmos
        md.gizmos_only = self.renderers == 0 and self.effects == 0 and bool(gizmos)
        md.view_2d = self.sprite_count > 0 and self.sprite_count * 2 >= self.renderers
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
    if builder.sprite_count:
        info.append(("Sprites", f"{builder.sprite_count:,}"))
    if builder.gizmos:
        info.append(("Gizmos", ", ".join(f"{sum(len(s) for _p, s in parts):,} {kind}"
                                           for kind, parts in builder.gizmos.items()) + " lines"))
    if builder.effects:
        info.append(("Effects", f"{builder.effects:,} particle systems / lines (still, approximate)"))
    if builder.skipped:
        info.append(("Skipped", f"{builder.skipped:,} lower-detail LOD renderers"))
    if builder.triangles >= MAX_TRIANGLES:
        info.append(("Note", f"stopped at {MAX_TRIANGLES:,} triangles"))
    log.info("Built '%s': %d objects, %d renderers, %s tris in %.1fs", name, builder.objects, builder.renderers,
             f"{builder.triangles:,}", time.time() - started)
    return md, materials, info


def scene_path(assets_file, scene_paths):
    """The scene's path in the original project ('Assets/Scenes/Main.unity') from the build's scene list, or ''."""
    m = re.match(r"level(\d+)$", assets_file.name or "", re.I)
    if m and int(m.group(1)) < len(scene_paths):
        return str(scene_paths[int(m.group(1))] or "")
    return ""


def scene_name(assets_file, scene_paths):
    m = re.match(r"level(\d+)$", assets_file.name or "", re.I)
    if m and int(m.group(1)) < len(scene_paths):
        return os.path.splitext(os.path.basename(scene_paths[int(m.group(1))]))[0]
    if (assets_file.name or "").lower().startswith("buildplayer-"):
        return assets_file.name[len("BuildPlayer-"):].split(".")[0]
    return assets_file.name
