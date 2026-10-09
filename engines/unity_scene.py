"""Unity scenes and prefabs as one 3D view: every renderer's mesh placed where the game puts it."""

import logging
import os
import re
import time

import numpy as np

from .sdk import ALBEDO, Material, MeshData, TextureRef, view_level, view_option
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


def ribbon(positions, width, loop=False, widths=None):
    """(points, uvs, triangles) of a line as two crossed double-sided strips, or None under 2 points.
    widths: one per point (a tapering trail) - the texture is then stretched once over the whole line."""
    line = np.asarray(positions, np.float64).reshape(-1, 3)
    if loop and len(line) > 2:
        line = np.vstack([line, line[:1]])
    half = np.full(len(line), max(width, 1e-4) / 2)
    along = None
    if widths is not None:
        half = np.maximum(np.asarray(widths, float).reshape(-1)[:len(line)], 1e-4) / 2
        seg = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))])
        along = seg / seg[-1] if seg[-1] > 0 else seg
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
        ha, hb = half[i], half[i + 1]
        u0, u1 = (0.0, 1.0) if along is None else (along[i], along[i + 1])
        for n in (n1, n2):
            base = len(pts)
            pts += [a - n * ha, b - n * hb, b + n * hb, a + n * ha]
            uvs += [(u0, 0), (u1, 0), (u1, 1), (u0, 1)]
            tris += [[base, base + 1, base + 2], [base, base + 2, base + 3],
                     [base, base + 2, base + 1], [base, base + 3, base + 2]]
    if not pts:
        return None
    return np.array(pts, np.float32), np.array(uvs, np.float32), np.array(tris, np.int64)


TRAIL_SPEED = 10.0    # units/s a TrailRenderer's object is shown moving at (the preview strip's length / its time)
TRAIL_POINTS = 8


def trail_preview(trail, matrix):
    """A TrailRenderer (type tree) drawn as if its object had been moving forward: (Unity world points, widths,
    head colour rgba), the trail streaming back along the object's -z for its time x TRAIL_SPEED (0.25 to 8
    units), tapered by its width curve. None when it's switched off or has no time."""
    if not trail.get("m_Enabled", 1):
        return None
    time = float(trail.get("m_Time", 5.0) or 0.0)
    if time <= 0:
        return None
    from .unity_particles import eval_curve
    m = np.asarray(matrix, float)
    back = -m[:3, 2]
    norm = np.linalg.norm(back)
    back = back / norm if norm > 1e-9 else np.array([0.0, 0.0, -1.0])
    length = min(max(time * TRAIL_SPEED, 0.25), 8.0)
    along = np.linspace(0.0, 1.0, TRAIL_POINTS)
    points = m[:3, 3] + back * (along * length)[:, None]
    params = trail.get("m_Parameters") or {}
    mult = float(params.get("widthMultiplier", 1.0) if params.get("widthMultiplier") is not None else 1.0)
    widths = np.abs(eval_curve(params.get("widthCurve"), along)) * mult
    grad = params.get("colorGradient") or {}
    color = tuple(float((grad.get("key0") or {}).get(k, 1.0)) for k in "rgba")
    return points, widths, color


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
    if name == "ReflectionProbe":
        return ("probe",) + join(box_lines(_v3(tree.get("m_BoxOffset")), _v3(tree.get("m_BoxSize"), 1.0)),
                                 sphere_lines((0, 0, 0), 0.5))
    if name in ("OcclusionArea", "OcclusionPortal"):
        return ("occlusion",) + box_lines(_v3(tree.get("m_Center")), _v3(tree.get("m_Size"), 1.0))
    if name == "LightProbeGroup":
        positions = tree.get("m_SourcePositions") or []
        if not positions:
            return None
        parts = []
        for p in positions[:4096]:
            pts, segs = marker_lines(0.15)
            parts.append((np.asarray(pts, float) + _v3(p), segs))
        return ("probe",) + join(*parts)
    return None


GIZMO_COMPONENTS = {"BoxCollider", "SphereCollider", "CapsuleCollider", "CharacterController", "BoxCollider2D",
                    "CircleCollider2D", "MeshCollider", "Light", "Camera", "AudioSource", "ReflectionProbe",
                    "OcclusionArea", "OcclusionPortal", "LightProbeGroup"}
JOINT_COMPONENTS = {"CharacterJoint", "ConfigurableJoint", "HingeJoint", "FixedJoint", "SpringJoint"}


def _ptr_key(ptr):
    try:
        if not ptr.path_id:
            return None
        reader = ptr.deref()
        return (id(reader.assets_file), reader.path_id), reader
    except Exception:
        return None


def lod_hidden(levels, keep):
    """Renderer keys to hide when an LODGroup shows level `keep`: those of the other levels, except any the
    shown level lists too (Valheim's fire_pit_iron has its one mesh in LOD0 and LOD1)."""
    hidden = set()
    for level, keys in enumerate(levels):
        if level != keep:
            hidden |= keys
    return hidden - levels[keep]


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
        self.owner = None     # "go:<file>:<path id>" of the object being visited (hierarchy() node "go")
        self.owner_starts, self.owner_uids = [], []  # first vertex of each placed piece -> its GameObject
        self.material_list, self.material_index = [], {}
        self.count = 0
        self.triangles = 0
        self.objects = self.renderers = self.skipped = 0
        self.lod_skip = set()
        self.terrains = 0
        self.sprites = {}     # sprite key -> (type tree, texture size, texture Asset) or None
        self.sprite_count = 0
        self.effects = 0      # particle systems and lines drawn
        self.emitters = []    # unity_particles.Emitter per particle system, for playback
        self.sub_links = []   # (parent Emitter, child key, type, probability) of Sub Emitters modules
        self.effect_ranges = []  # (first, end) vertices of the still particle puffs (hidden during playback)
        self.lm_uvs = []      # per placed piece: lightmap UVs (or None)
        self.lightmapped = 0  # renderers drawn with their baked lightmap
        self.render_settings = None  # RenderSettings of the first scene file visited (sky, ambient, fog)
        self.joints = []      # (anchor (3,) UniView space, connected transform key or None, axis (3,) or None)
        self.cloths = 0
        self.suns = []        # (strength, direction (UniView), colour, intensity) of enabled directional lights
        self.vfx = []         # names of the VFX Graph effects (VisualEffect) placed - drawn as markers only
        self.node_pos = {}    # transform key -> (position in UniView space, parent transform key, name)
        self.skin_bones = []  # (bone transform keys (None for missing), bind poses or None, matrix) per skinned renderer
        self.binds = {}       # mesh key -> (B, 4, 4) bind poses
        self.gizmos = {}      # kind -> [(points in UniView space, segments)]
        self.gizmo_segments = 0
        self.show_effects = view_option("show_effects")
        self.hide_lods = view_option("hide_lods")
        self.lod_level = view_level("lod_level")  # which level of each LODGroup (when hide_lods)
        self.lod_count = 0
        self.hide_inactive = view_option("hide_inactive")

    def _mesh(self, key, reader):
        if key not in self.meshes:
            from .unity import bind_matrices, mesh_to_meshdata
            try:
                mesh = reader.read()
                self.meshes[key] = mesh_to_meshdata(mesh)
                bind = getattr(mesh, "m_BindPose", None)
                if bind:
                    self.binds[key] = bind_matrices(bind)
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
            from .unity import make_material, read_material
            try:
                material = make_material(read_material(reader.read()), self.session._texture_asset)
            except Exception:
                return None
            self.material_index[key] = len(self.material_list)
            self.material_list.append(material)
        return self.material_index[key]

    def _add(self, md, matrix, materials, submesh_filter=None, lightmap_uv=None):
        """Place a mesh: matrix is Unity-space local-to-world; materials are indices into material_list.
        lightmap_uv: (N, 2) baked-light texture coordinates of md's vertices, or None."""
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
            lightmap_uv = lightmap_uv[used] if lightmap_uv is not None else None
            picked = [(remap[t], s) for t, s in picked]
        pts = (points.astype(np.float64) @ rot.T + pos).astype(np.float32)
        for tris, slot in picked:
            self.subs.append((tris[:, ::-1] if mirrored else tris) + self.count)
            mat = materials[min(slot, len(materials) - 1)] if materials else None
            self.slots.append(mat if mat is not None else self._default_material())
        self.triangles += n_tris
        self.owner_starts.append(self.count)
        self.owner_uids.append(self.owner)
        self.points.append(pts)
        self.uvs.append(uv if uv is not None else np.zeros((len(pts), 2), np.float32))
        self.lm_uvs.append(lightmap_uv)
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
            self.material_list.append(Material(st.get("m_Name") or "sprite", refs, color=color, alpha_mode="blend"))
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

    def _lightmapped(self, reader, renderer, md, materials):
        """(lightmap UVs or None, materials) of a MeshRenderer baked into one of its scene's lightmaps: the
        mesh's second UV set (else its first) x the renderer's lightmap tiling/offset, and lightmapped
        variants of its materials."""
        try:
            index = int(getattr(renderer, "m_LightmapIndex", 0xFFFF))
            if index >= 0xFFFE:
                return None, materials
            _render, lightmaps = self.session.scene_settings(reader.assets_file)
            if index >= len(lightmaps) or lightmaps[index][0] is None:
                return None, materials
            uv = md.uvs.get("UV1", next(iter(md.uvs.values()), None))
            if uv is None:
                return None, materials
            st = renderer.m_LightmapTilingOffset
            lightmap_uv = (uv * np.array([st.x, st.y], np.float32) + np.array([st.z, st.w], np.float32))
        except Exception as e:
            log.debug("Scene lightmap: %s", e)
            return None, materials
        asset, mode = lightmaps[index]
        out = []
        for m in materials:
            base = m if m is not None else self._default_material()
            key = ("lightmap", base, index, id(reader.assets_file))
            if key not in self.material_index:
                src = self.material_list[base]
                copy = Material(src.name, src.textures, src.color, src.properties, alpha_mode=src.alpha_mode,
                                alpha_cutoff=src.alpha_cutoff)
                copy.lightmap, copy.lightmap_mode = asset, mode
                self.material_index[key] = len(self.material_list)
                self.material_list.append(copy)
            out.append(self.material_index[key])
        self.lightmapped += 1
        return lightmap_uv.astype(np.float32), out

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
                                               tuple(a * b for a, b in zip(own, color)), base.properties,
                                               alpha_mode=base.alpha_mode, alpha_cutoff=base.alpha_cutoff))
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
            mat_reader = self._ref(renderer_reader, mats[0]) if mats else None
            material = self._tinted(mat_reader, gradient_color((ps.get("InitialModule") or {}).get("startColor")))
            mesh = self._ref(renderer_reader, renderer.get("m_Mesh"))
            md = None
            if int(renderer.get("m_RenderMode", 0) or 0) == 4 and mesh is not None:
                md = self._mesh((id(mesh.assets_file), mesh.path_id), mesh)
            self._renderer_reader = renderer_reader
            self._emitter(ps, renderer, matrix, ps_reader, mat_reader, md)
            first = self.count
            if md is not None:
                for c in centres:
                    place = np.eye(4)
                    place[:3, :3] *= size
                    place[:3, 3] = c
                    self._add(md, matrix @ place, [material])
                self.effect_ranges.append((first, self.count))
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
        first = self.count
        if self._add_local(points, uvs, tris, matrix, material):
            self.effect_ranges.append((first, self.count))
            self.effects += 1

    def _emitter(self, ps, renderer, matrix, ps_reader, mat_reader, md):
        """Keep what playback needs to move this particle system's particles (see unity_particles)."""
        from .unity_particles import Emitter, sub_emitter_refs
        try:
            index = self._material(_Deref(mat_reader)) if mat_reader is not None else None
            mesh = None
            if md is not None:
                mesh = (md.points, np.concatenate(md.submeshes), next(iter(md.uvs.values()), None))
            trail = None
            mats = renderer.get("m_Materials") or []  # Unity keeps the trail material second
            trail_reader = self._ref(self._renderer_reader, renderer.get("m_TrailMaterial") or
                                     (mats[1] if len(mats) > 1 else None))
            if trail_reader is not None:
                trail = self._material(_Deref(trail_reader))
            emitter = Emitter(ps, renderer, matrix, ps_reader.path_id,
                              index if index is not None else self._default_material(), mesh, trail)
            emitter.key = (id(ps_reader.assets_file), ps_reader.path_id)
            for ptr, kind, chance in sub_emitter_refs(ps):
                child = self._ref(ps_reader, ptr)
                if child is not None:
                    self.sub_links.append((emitter, (id(child.assets_file), child.path_id), kind, chance))
            self.emitters.append(emitter)
        except Exception as e:
            log.debug("Scene particle playback: %s", e)

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

    def _trail(self, reader, matrix):
        """A TrailRenderer as a short tapering strip behind its object (trails only exist while things move)."""
        try:
            trail = reader.read_typetree()
            if not trail.get("m_Enabled", 1) and not self.hide_inactive:
                trail = dict(trail, m_Enabled=1)
            preview = trail_preview(trail, matrix)
            if preview is None:
                return
            points, widths, color = preview
            built = ribbon(points, 0.0, widths=widths)
            if built is None:
                return
            mats = trail.get("m_Materials") or []
            material = self._tinted(self._ref(reader, mats[0]) if mats else None, color)
        except Exception as e:
            log.debug("Scene trail: %s", e)
            return
        if self._add_local(*built, np.eye(4), material):
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

    def _joint(self, reader, matrix):
        """Remember a physics joint: its anchor, the body it's connected to and (hinges) its axis; drawn as gizmo
        lines once every transform's position is known (see _joint_gizmos)."""
        try:
            tree = reader.read_typetree()
            if not tree.get("m_Enabled", 1):
                return
            anchor = matrix[:3, :3] @ np.asarray(_v3(tree.get("m_Anchor")), float) + matrix[:3, 3]
            anchor[0] *= -1
            connected = None
            body = self._ref(reader, tree.get("m_ConnectedBody"))
            if body is not None:
                go = self._ref(body, body.read_typetree().get("m_GameObject"))
                if go is not None:
                    transform = next((r for n, r in _components(go.read()) if n in ("Transform", "RectTransform")), None)
                    if transform is not None:
                        connected = (id(transform.assets_file), transform.path_id)
            axis = None
            if "m_Axis" in tree:
                axis = matrix[:3, :3] @ np.asarray(_v3(tree.get("m_Axis"), 0.0), float)
                axis[0] *= -1
                norm = np.linalg.norm(axis)
                axis = axis / norm if norm > 1e-9 else None
            self.joints.append((anchor, connected, axis))
        except Exception as e:
            log.debug("Scene joint: %s", e)

    def _sun(self, reader, matrix):
        """Remember an enabled directional light (the scene's sun) for the Lighting button."""
        try:
            tree = reader.read_typetree()
            if int(tree.get("m_Type", 2)) != 1 or not tree.get("m_Enabled", 1):
                return
            c = tree.get("m_Color") or {}
            color = tuple(float(c.get(k, 1.0)) for k in "rgb")
            intensity = float(tree.get("m_Intensity", 1.0) or 0.0)
            direction = matrix[:3, :3] @ np.array([0.0, 0.0, 1.0])  # lights shine along their +z
            direction[0] *= -1
            norm = np.linalg.norm(direction)
            if norm < 1e-9 or intensity <= 0:
                return
            # HDRP stores physical units (lux, often 10^4+): only the relative order matters there.
            shown = intensity if intensity <= 8 else 1.2
            self.suns.append((intensity * max(color), direction / norm, color, min(shown, 2.0)))
        except Exception as e:
            log.debug("Scene light: %s", e)

    def _vfx(self, reader, matrix):
        """A VFX Graph effect: can't be simulated from the files, so a marker where it sits (and its name)."""
        try:
            tree = reader.read_typetree()
            if not tree.get("m_Enabled", 1):
                return
            asset = self._ref(reader, tree.get("m_Asset"))
            self.vfx.append((asset.peek_name() if asset is not None else "") or "VFX")
        except Exception as e:
            log.debug("Scene VFX: %s", e)
            return
        pts, segs = join(marker_lines(0.5), sphere_lines((0, 0, 0), 0.35))
        world = np.asarray(pts, float) @ matrix[:3, :3].T + matrix[:3, 3]
        world[:, 0] *= -1
        self.gizmos.setdefault("vfx", []).append((world.astype(np.float32), np.asarray(segs, np.int64)))

    def _joint_gizmos(self):
        """Joint lines: anchor -> connected body (a ragdoll's bone chain), a small cross at each anchor, and the
        hinge axis."""
        pts, segs = [], []
        for anchor, connected, axis in self.joints:
            other = self.node_pos.get(connected, (None,))[0] if connected is not None else None
            size = 0.05
            if other is not None:
                other = np.asarray(other, float)
                size = max(0.02, min(0.25, 0.15 * float(np.linalg.norm(other - anchor))))
                segs.append((len(pts), len(pts) + 1))
                pts += [anchor, other]
            for d in np.eye(3):
                segs.append((len(pts), len(pts) + 1))
                pts += [anchor - d * size, anchor + d * size]
            if axis is not None:
                segs.append((len(pts), len(pts) + 1))
                pts += [anchor - axis * size * 2, anchor + axis * size * 2]
        if pts:
            self.gizmos.setdefault("joint", []).append((np.asarray(pts, np.float32), np.asarray(segs, np.int64)))

    def _default_material(self):
        if None not in self.material_index:
            self.material_index[None] = len(self.material_list)
            self.material_list.append(Material("(no material)", []))
        return self.material_index[None]

    def visit(self, transform_reader, parent_matrix, parent_active=True, depth=0, parent_key=None):
        """Walk a transform and its children (depth first)."""
        if depth > 200 or self.triangles >= MAX_TRIANGLES:
            return
        try:
            t = transform_reader.read()
            go_reader = t.m_GameObject.deref()
            go = go_reader.read()
        except Exception:
            return
        self.owner = f"go:{go_reader.assets_file.name}:{go_reader.path_id}"
        p, r, s = t.m_LocalPosition, t.m_LocalRotation, t.m_LocalScale
        matrix = parent_matrix @ trs((p.x, p.y, p.z), (r.x, r.y, r.z, r.w), (s.x, s.y, s.z))
        key = (id(transform_reader.assets_file), transform_reader.path_id)
        if self.render_settings is None and depth == 0 and is_scene_file(transform_reader.assets_file):
            self.render_settings = self.session.scene_settings(transform_reader.assets_file)[0] or {}
        self.node_pos[key] = ((-matrix[0, 3], matrix[1, 3], matrix[2, 3]), parent_key, getattr(go, "m_Name", ""))
        active = parent_active and (bool(getattr(go, "m_IsActive", True)) or not self.hide_inactive)
        self.objects += 1
        if active:
            comps = _components(go)
            names = {n for n, _r in comps}
            for name, reader in comps:
                if name == "LODGroup" and self.hide_lods:
                    try:
                        lods = list(reader.read().m_LODs)
                        self.lod_count = max(self.lod_count, len(lods))
                        keep = min(self.lod_level, len(lods) - 1)  # groups with fewer levels show their last
                        self.lod_skip |= lod_hidden([{found[0] for lr in lod.renderers
                                                      if (found := _ptr_key(lr.renderer))} for lod in lods], keep)
                    except Exception:
                        pass
            for name, reader in comps:
                if name in JOINT_COMPONENTS:
                    self._joint(reader, matrix)
                    continue
                if name == "Cloth":
                    self.cloths += 1
                    continue
                if name == "VisualEffect":
                    self._vfx(reader, matrix)
                    continue
                if name == "Light":
                    self._sun(reader, matrix)
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
                if name == "TrailRenderer" and self.show_effects:
                    self._trail(reader, matrix)
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
                lightmap_uv = None
                if name == "MeshRenderer":
                    lightmap_uv, materials = self._lightmapped(reader, renderer, md, materials)
                batch = getattr(renderer, "m_StaticBatchInfo", None)
                first = getattr(batch, "firstSubMesh", 0) if batch is not None else 0
                count = getattr(batch, "subMeshCount", 0) if batch is not None else 0
                if count:
                    # Static batching: the combined mesh is already in world space.
                    self._add(md, np.eye(4), materials, (first, count), lightmap_uv)
                else:
                    self._add(md, matrix, materials, lightmap_uv=lightmap_uv)
                self.renderers += 1
                if name == "SkinnedMeshRenderer":
                    self.skin_bones.append(([f[0] if f else None for f in map(_ptr_key, renderer.m_Bones or [])],
                                            self.binds.get(found[0]), matrix.copy()))
        for child in t.m_Children or []:
            try:
                self.visit(child.deref(), matrix, active, depth + 1, key)
            except Exception:
                continue

    def _environment(self):
        """Sky colors, ambient light and fog of the scene (from its RenderSettings), or None."""
        render = self.render_settings
        if not render:
            return None

        def rgb(d, default=None):
            return tuple(float(d.get(k, 0.0)) for k in "rgb") if isinstance(d, dict) else default
        ambient = rgb(render.get("m_AmbientSkyColor"), (0.5, 0.5, 0.5))
        sky = sky_colors(self.session, render)
        if sky is None:
            ground = rgb(render.get("m_AmbientGroundColor"), ambient)
            horizon = rgb(render.get("m_AmbientEquatorColor"), ambient)
            sky = (ambient, horizon, ground) if int(render.get("m_AmbientMode", 0) or 0) == 1 else None
        fog = fog_params = None
        if render.get("m_Fog"):
            fog_params = {"mode": int(render.get("m_FogMode", 3) or 3),
                          "color": rgb(render.get("m_FogColor"), (0.5, 0.5, 0.5)),
                          "density": float(render.get("m_FogDensity", 0.01) or 0.0),
                          "start": float(render.get("m_LinearFogStart", 0.0) or 0.0),
                          "end": float(render.get("m_LinearFogEnd", 300.0) or 0.0)}
            mode = {1: "linear", 2: "exponential", 3: "exponential squared"}.get(int(render.get("m_FogMode", 3)), "")
            c = rgb(render.get("m_FogColor"), (0.5, 0.5, 0.5))
            fog = (f"{mode}, color #{''.join(f'{int(max(0, min(1, v)) * 255):02x}' for v in c)}" +
                   (f", {float(render.get('m_LinearFogStart', 0)):g}-{float(render.get('m_LinearFogEnd', 0)):g} units"
                    if mode == "linear" else f", density {float(render.get('m_FogDensity', 0)):g}"))
        name = ""
        try:
            ptr = render["_reader"].read().m_SkyboxMaterial
            if ptr.path_id:
                name = ptr.deref().peek_name() or ""
        except Exception:
            pass
        sun = max(self.suns, key=lambda s: s[0]) if self.suns else None
        return {"sky": sky, "ambient": ambient, "fog": fog, "fog_params": fog_params, "sky_name": name,
                "sun": {"direction": tuple(sun[1]), "color": sun[2], "intensity": sun[3]} if sun else None,
                "sky_texture": sky_texture(self.session, render)}

    def _bones(self):
        """Bones of the skinned meshes drawn (or None): where the mesh's bind pose puts them - the mesh is drawn in
        that pose at its renderer - else where the scene/prefab puts their transforms."""
        from .sdk import Bones
        keys = list(dict.fromkeys(k for bones, _b, _m in self.skin_bones for k in bones if k in self.node_pos))
        if not keys:
            return None
        at = {}
        for bones, bind, matrix in self.skin_bones:
            if bind is None or len(bind) < len(bones):
                continue
            for k, b in zip(bones, bind):
                if k is not None and k not in at and abs(np.linalg.det(b)) > 1e-12:
                    p = (matrix @ np.linalg.inv(b))[:3, 3]
                    at[k] = (-p[0], p[1], p[2])
        index = {k: i for i, k in enumerate(keys)}
        parents = []
        for k in keys:
            p, hops = self.node_pos[k][1], 0
            while p is not None and p not in index and p in self.node_pos and hops < 64:
                p, hops = self.node_pos[p][1], hops + 1
            parents.append(index.get(p, -1))
        return Bones([at.get(k, self.node_pos[k][0]) for k in keys], parents, [self.node_pos[k][2] for k in keys])

    def result(self, name):
        self._joint_gizmos()
        gizmos = {}
        for kind, parts in self.gizmos.items():
            pts, segs = join(*parts)
            gizmos[kind] = (pts.astype(np.float32), segs)
        if not self.subs and not gizmos:
            raise ValueError("Nothing visible here (no active mesh or sprite renderers, colliders or lights).")
        if not self.subs:
            # Only colliders / lights...: two invisible (flat) triangles at the corners give the view its size.
            self.owner_starts.append(self.count)
            self.owner_uids.append(None)
            allpts = np.concatenate([p for p, _s in gizmos.values()])
            lo, hi = allpts.min(0), allpts.max(0)
            self.points.append(np.array([lo, lo, lo, hi, hi, hi], np.float32))
            self.uvs.append(np.zeros((6, 2), np.float32))
            self.subs.append(np.array([[0, 1, 2], [3, 4, 5]], np.int64))
            self.slots.append(self._default_material())
        uvs = {"UV0": np.concatenate(self.uvs)}
        if any(lm is not None for lm in self.lm_uvs):
            uvs["lightmap"] = np.concatenate([lm if lm is not None else np.zeros((len(p), 2), np.float32)
                                              for lm, p in zip(self.lm_uvs, self.points)])
        md = MeshData(np.concatenate(self.points), self.subs, uvs=uvs, material_slots=self.slots, name=name)
        md.gizmos = gizmos
        md.owners = (np.array(self.owner_starts, np.int64), self.owner_uids)
        md.gizmos_only = self.renderers == 0 and self.effects == 0 and bool(gizmos)
        if self.sub_links:
            from .unity_particles import link_sub_emitters
            link_sub_emitters(self.emitters, self.sub_links)
        md.emitters = self.emitters
        md.environment = self._environment()
        md.lod_count = self.lod_count
        md.bones = self._bones()
        if self.effect_ranges:
            md.effect_mask = np.zeros(self.count, bool)
            for first, end in self.effect_ranges:
                md.effect_mask[first:end] = True
        md.view_2d = self.sprite_count > 0 and self.sprite_count * 2 >= self.renderers
        return md, self.material_list


# Three-colour gradient skies (Funly Sky Studio, Schedule I's Shader Graph): upper at the zenith, middle part-way
# up, lower at the horizon and below (not a ground colour).
SKY_THREE = (("_gradientskyuppercolor", "_gradientskymiddlecolor", "_gradientskylowercolor"),
             ("_skyuppercolor", "_skymiddlecolor", "_skylowercolor"))
SKY_MIDDLE_AT = 0.35     # height (0 horizon .. 1 zenith) of the middle colour when the material doesn't say


def sky_stops(colors, floats):
    """[(height -1..1, rgb)] of a three-colour gradient sky (lowercase property name dicts), or None."""
    for upper, middle, lower in SKY_THREE:
        if upper in colors and middle in colors and lower in colors:
            # Funly: lower up to _GradientFadeBegin, upper from _GradientFadeEnd, middle at _GradientFadeMiddlePosition
            # of the way between them
            begin = min(max(float(floats.get("_gradientfadebegin", 0.0)), -0.99), 0.98)
            end = min(max(float(floats.get("_gradientfadeend", 1.0)), begin + 0.01), 1.0)
            frac = float(floats.get("_gradientfademiddleposition", SKY_MIDDLE_AT))
            mid = begin + (end - begin) * min(max(frac, 0.0), 1.0)
            stops = [(-1.0, colors[lower]), (begin, colors[lower]), (mid, colors[middle]), (end, colors[upper])]
            return stops + ([(1.0, colors[upper])] if end < 1.0 else [])
    return None


SKY_TOP = ("_skytint", "_skycolor", "_topcolor", "_skytopcolor", "_zenithcolor", "_upcolor", "_gradientskyuppercolor",
           "_skyuppercolor", "_color1", "_tint")
SKY_HORIZON = ("_horizoncolor", "_skymiddlecolor", "_middlecolor", "_equatorcolor", "_gradientskymiddlecolor", "_color2")
SKY_GROUND = ("_groundcolor", "_bottomcolor", "_skybottomcolor", "_downcolor", "_gradientskylowercolor", "_skylowercolor",
              "_color3")
SKY_CUBE_SLOTS = ("_Tex", "_MainTex", "_Cubemap", "_CubeMap", "_SkyCubemap", "_SkyboxCubemap", "_Skybox")
CLOUD_CUBE_SLOTS = ("_CloudCubemapTexture", "_CloudCubemap", "_CloudsCubemap")


def sky_colors(session, render):
    """(top, horizon, bottom) colors of a scene's skybox material, from its color properties (procedural skies and
    most gradient sky shaders), or None. Textured skies (cubemaps, panoramas) aren't drawn."""
    try:
        ptr = render["_reader"].read().m_SkyboxMaterial
        if not ptr.path_id:
            return None
        mat = ptr.deref().read()
        colors = {}
        for prop, c in mat.m_SavedProperties.m_Colors or []:
            prop = prop if isinstance(prop, str) else getattr(prop, "name", str(prop))
            colors[prop.lower()] = (float(c.r), float(c.g), float(c.b))
        floats = {}
        for prop, v in mat.m_SavedProperties.m_Floats or []:
            prop = prop if isinstance(prop, str) else getattr(prop, "name", str(prop))
            floats[prop.lower()] = float(v)
    except Exception as e:
        log.debug("Sky colors: %s", e)
        return None

    def pick(names):
        return next((colors[n] for n in names if n in colors), None)
    top, horizon, bottom = pick(SKY_TOP), pick(SKY_HORIZON), pick(SKY_GROUND)
    stops = sky_stops(colors, floats)
    if stops is not None:  # the horizon is the lower colour; the middle one only shows in the skybox
        top, horizon, bottom = stops[-1][1], stops[0][1], stops[0][1]
    if "_skytint" in colors and "_atmospherethickness" in floats:
        # Unity's procedural sky: the tint (0.5 gray = default) scales a blue scattering color
        top = tuple(2.0 * t * b for t, b in zip(colors["_skytint"], (0.36, 0.56, 0.92)))
    if top is None and horizon is None:
        return None
    exposure = min(max(floats.get("_exposure", 1.0), 0.3), 2.0)
    top = tuple(min(1.0, c * exposure) for c in (top or horizon))
    horizon = horizon or tuple(min(1.0, 0.5 * t + 0.5) for t in top)  # procedural skies are pale at the horizon
    bottom = bottom or tuple(0.5 * c for c in horizon)
    return top, horizon, bottom


SIX_SIDED = ("_LeftTex", "_RightTex", "_UpTex", "_DownTex", "_FrontTex", "_BackTex")  # +X -X +Y -Y +Z -Z (Unity docs)


def sky_texture(session, render):
    """A scene's textured skybox: {"kind": "cube" (a Cubemap asset) / "six" (6 Texture2D assets, Unity's face
    order) / "pano" (an equirectangular Texture2D), "assets": [...], "tint": (r, g, b) (1 = unchanged),
    "rotation": degrees around y} from Skybox/Cubemap, 6 Sided and Panoramic style materials, or None."""
    try:
        ptr = render["_reader"].read().m_SkyboxMaterial
        if not ptr.path_id:
            return None
        mat = ptr.deref().read()
        shader = (shader_name_of(mat) or "").lower()
        saved = mat.m_SavedProperties
        texs = {}
        for prop, env in saved.m_TexEnvs or []:
            prop = prop if isinstance(prop, str) else getattr(prop, "name", str(prop))
            if env.m_Texture.path_id:
                reader = env.m_Texture.deref()
                texs[prop] = reader
        colors = {(p if isinstance(p, str) else getattr(p, "name", str(p))): c for p, c in saved.m_Colors or []}
        floats = {(p if isinstance(p, str) else getattr(p, "name", str(p))): float(v) for p, v in saved.m_Floats or []}
        stops = sky_stops({k.lower(): (float(c.r), float(c.g), float(c.b)) for k, c in colors.items()},
                          {k.lower(): v for k, v in floats.items()})
    except Exception as e:
        log.debug("Sky texture: %s", e)
        return None
    tint = colors.get("_Tint")
    exposure = floats.get("_Exposure", 1.0)
    scale = (tuple(min(2.0, 2.0 * float(getattr(tint, k)) * exposure) for k in "rgb") if tint is not None
             else (exposure,) * 3)
    out = {"tint": scale, "rotation": floats.get("_Rotation", 0.0)}
    # The sky's own cubemap - not a star field or cloud layer some sky shaders also have (Valheim's _StarFieldTex
    # is the night sky).
    cube = next((texs[k] for k in SKY_CUBE_SLOTS if k in texs and texs[k].type.name == "Cubemap"), None)
    if cube is None and "cubemap" in shader:
        cube = next((r for r in texs.values() if r.type.name == "Cubemap"), None)
    if cube is not None:
        return {**out, "kind": "cube", "assets": [session._asset_for(cube, "Cubemap")]}
    clouds = next((texs[k] for k in CLOUD_CUBE_SLOTS if k in texs and texs[k].type.name == "Cubemap"), None)
    if clouds is not None:  # gradient sky shaders (Funly Sky Studio): clouds over the sky colours
        return {**out, "tint": (1.0, 1.0, 1.0), "kind": "clouds", "assets": [session._asset_for(clouds, "Cubemap")],
                "cloud_alpha": min(max(floats.get("_CloudAlpha", 1.0), 0.0), 1.0), "stops": stops}
    if all(k in texs for k in SIX_SIDED):
        return {**out, "kind": "six", "assets": [session._asset_for(texs[k]) for k in SIX_SIDED]}
    if "panoram" in shader and texs.get("_MainTex") is not None and texs["_MainTex"].type.name == "Texture2D":
        return {**out, "kind": "pano", "assets": [session._asset_for(texs["_MainTex"])]}
    if stops is not None:  # a three-colour gradient: drawn as a skybox so the middle colour shows
        return {**out, "tint": (1.0, 1.0, 1.0), "kind": "gradient", "assets": [], "stops": stops}
    return None


def shader_name_of(mat):
    from .unity import shader_name
    return shader_name(mat.m_Shader)


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
    if md.bones is not None:
        info.append(("Bones", f"{len(md.bones):,}"))
    if builder.effects:
        info.append(("Effects", f"{builder.effects:,} particle systems / lines (still, approximate)"))
    if builder.joints:
        info.append(("Joints", f"{len(builder.joints):,} (pink gizmo lines: anchor to the connected body)"))
    if builder.vfx:
        names = sorted(set(builder.vfx))
        info.append(("VFX Graph", f"{len(builder.vfx):,} effects shown as violet markers (not simulated): "
                     + ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")))
    if builder.cloths:
        info.append(("Cloth", f"{builder.cloths:,} (simulated while the game runs; drawn at rest)"))
    if builder.lightmapped:
        info.append(("Lightmaps", f"{builder.lightmapped:,} renderers with baked lighting"))
    env = md.environment
    if env and env.get("sky_name"):
        info.append(("Skybox", env["sky_name"]))
    if env and env.get("fog"):
        info.append(("Fog", env["fog"]))
    if builder.skipped:
        info.append(("Skipped", f"{builder.skipped:,} renderers of other LOD levels (showing LOD{builder.lod_level})"
                     if builder.lod_level else f"{builder.skipped:,} lower-detail LOD renderers"))
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
