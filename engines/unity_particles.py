"""Unity ParticleSystem playback: where each particle is at a given time, worked out from its type tree.

Stateless: every particle that can be alive at time t is regenerated from its spawn time and a per-particle
random seed, so any time can be shown directly (scrubbing, looping) without stepping a simulation.
Approximate on purpose: curves are linear between keys, and noise, collisions, sub-emitters, trails,
limit-velocity damping and rate-over-distance are left out.
"""

import numpy as np

from .unity_scene import curve_value, euler_matrix

MAX_PER_EMITTER = 1500   # particles one emitter shows at most
TRAIL_POINTS = 10        # points along each particle's trail
GRAVITY = 9.81

RENDER_MODES = {0: "billboard", 1: "stretch", 2: "horizontal", 3: "vertical", 4: "mesh", 5: "none"}


class Particles:
    """Particles alive at one moment, in UniView space (x-flipped Unity world space)."""

    __slots__ = ("pos", "vel", "size", "rot", "color", "frame", "trails")

    def __init__(self, pos, vel, size, rot, color, frame, trails=None):
        self.pos = pos      # (N, 3) float
        self.vel = vel      # (N, 3) float, units per second
        self.size = size    # (N,) float, world units
        self.rot = rot      # (N,) float, radians around the view axis
        self.color = color  # (N, 4) float 0..1 (start color x color over lifetime)
        self.frame = frame  # (N,) int, texture sheet frame
        self.trails = trails  # Trails module: (points (M, K, 3), widths (M, K), colors (M, K, 4)), newest point first

    def __len__(self):
        return len(self.pos)


# ---------------------------------------------------------------------------- curves and gradients

def _keys(curve):
    keys = (curve or {}).get("m_Curve") or []
    t = np.array([float(k.get("time", 0.0)) for k in keys if isinstance(k, dict)], float)
    v = np.array([float(k.get("value", 0.0)) for k in keys if isinstance(k, dict)], float)
    return t, v


def eval_curve(curve, x):
    """An AnimationCurve dict at x (array), linear between keys; 1 if it has no keys."""
    t, v = _keys(curve)
    x = np.asarray(x, float)
    if not len(t):
        return np.ones_like(x)
    if len(t) == 1:
        return np.full_like(x, v[0])
    return np.interp(x, t, v)


def eval_minmax(c, u, x, default=0.0):
    """A MinMaxCurve dict per particle: u = its random 0..1, x = where on the curve (0..1)."""
    u = np.asarray(u, float)
    if not isinstance(c, dict):
        return np.full_like(u, default)
    state = int(c.get("minMaxState", 0) or 0)
    scalar = float(c.get("scalar", default))
    if state == 3:  # random between two constants
        lo = c.get("minScalar")
        lo = float(lo) if lo is not None else scalar * float(eval_curve(c.get("minCurve"), 0.0))
        return lo + (scalar - lo) * u
    if state == 1:
        return scalar * eval_curve(c.get("maxCurve"), x) * np.ones_like(u)
    if state == 2:  # random between two curves
        lo, hi = eval_curve(c.get("minCurve"), x), eval_curve(c.get("maxCurve"), x)
        return scalar * (lo + (hi - lo) * u)
    return np.full_like(u, scalar)


def _rgba(d):
    return [float((d or {}).get(k, 1.0)) for k in "rgba"]


def eval_gradient(g, x):
    """A Gradient dict at x (array 0..1) -> (N, 4)."""
    x = np.asarray(x, float)
    out = np.ones((len(x), 4))
    if not isinstance(g, dict):
        return out
    n_color = max(1, min(8, int(g.get("m_NumColorKeys", 2) or 1)))
    n_alpha = max(1, min(8, int(g.get("m_NumAlphaKeys", 2) or 1)))
    keys = [_rgba(g.get(f"key{i}")) for i in range(8)]
    stepped = int(g.get("m_Mode", 0) or 0) == 1
    for channels, n, prefix in (((0, 1, 2), n_color, "ctime"), ((3,), n_alpha, "atime")):
        times = np.array([float(g.get(f"{prefix}{i}", 0)) / 65535.0 for i in range(n)])
        order = np.argsort(times, kind="stable")
        times = times[order]
        for ch in channels:
            values = np.array([keys[i][ch] for i in range(n)])[order]
            if stepped:
                out[:, ch] = values[np.clip(np.searchsorted(times, x, side="left"), 0, n - 1)]
            else:
                out[:, ch] = np.interp(x, times, values)
    return out


def eval_minmax_gradient(g, u, x):
    """A MinMaxGradient dict per particle -> (N, 4)."""
    u = np.asarray(u, float)
    n = len(u)
    if not isinstance(g, dict):
        return np.ones((n, 4))
    state = int(g.get("minMaxState", 0) or 0)
    x = np.broadcast_to(np.asarray(x, float), (n,))
    if state == 0:
        return np.tile(_rgba(g.get("maxColor")), (n, 1))
    if state == 1:
        return eval_gradient(g.get("maxGradient"), x)
    if state == 2:
        lo, hi = np.array(_rgba(g.get("minColor"))), np.array(_rgba(g.get("maxColor")))
        return lo + (hi - lo) * u[:, None]
    if state == 3:
        lo, hi = eval_gradient(g.get("minGradient"), x), eval_gradient(g.get("maxGradient"), x)
        return lo + (hi - lo) * u[:, None]
    return eval_gradient(g.get("maxGradient"), u)  # random color from the gradient


def _max_of(c, default):
    """Largest value a MinMaxCurve can give (how long particles can live)."""
    if not isinstance(c, dict):
        return default
    state = int(c.get("minMaxState", 0) or 0)
    scalar = float(c.get("scalar", default))
    if state == 3:
        return max(scalar, float(c.get("minScalar", scalar) or 0.0))
    if state in (1, 2):
        values = [_keys(c.get(n))[1] for n in ("maxCurve", "minCurve")]
        top = max((float(v.max()) for v in values if len(v)), default=1.0)
        return scalar * top
    return scalar


# ---------------------------------------------------------------------------- random numbers

def _hash(ids, stream):
    """splitmix64 of (id, stream) -> uniform floats in [0, 1)."""
    z = ids.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15) + np.uint64((stream * 0xBF58476D1CE4E5B9) & (2**64 - 1))
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    z = z ^ (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def _sphere_dirs(u, v):
    z = 2.0 * u - 1.0
    a = 2.0 * np.pi * v
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    return np.stack([r * np.cos(a), r * np.sin(a), z], axis=1)


def _normalize(v):
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


# ---------------------------------------------------------------------------- emitter

class Emitter:
    """One ParticleSystem placed in a scene/prefab. simulate(t) -> Particles alive t seconds after it started.

    material: index into the scene's material list; render_mode: billboard / stretch / horizontal / vertical /
    mesh; tiles: (x, y) texture sheet frames; mesh: (points, triangles, uvs) for mesh particles (UniView space,
    size 1); center: where it is (UniView space), so busy scenes can play the ones near the camera."""

    def __init__(self, ps, renderer, matrix, seed=0, material=None, mesh=None, trail_material=None):
        self.ps = ps
        trails = ps.get("TrailModule") or {}
        # Trails (streaks behind each particle, or ribbons joining them) drawn with the renderer's trail material
        self.trails = trails if trails.get("enabled") and trail_material is not None else None
        self.trail_material = trail_material
        self.matrix = np.asarray(matrix, float)        # Unity local-to-world
        self.rot = self.matrix[:3, :3]
        self.scale = abs(np.linalg.det(self.rot)) ** (1 / 3) or 1.0
        self.seed = int(seed) & 0xFFFFFFFF
        self.material = material
        self.mesh = mesh
        mode = int(renderer.get("m_RenderMode", 0) or 0)
        self.render_mode = RENDER_MODES.get(mode, "billboard")
        if self.render_mode == "mesh" and mesh is None:
            self.render_mode = "billboard"
        self.length_scale = float(renderer.get("m_LengthScale", 2.0) or 0.0)
        self.velocity_scale = float(renderer.get("m_VelocityScale", 0.0) or 0.0)
        pos = self.matrix[:3, 3]
        self.center = np.array([-pos[0], pos[1], pos[2]])

        init = ps.get("InitialModule") or {}
        self.init = init
        self.duration = max(float(ps.get("lengthInSec", 5.0) or 5.0), 0.05)
        self.looping = bool(ps.get("looping", 1))
        self.prewarm = bool(ps.get("prewarm", 0)) and self.looping
        self.delay = max(curve_value(ps.get("startDelay"), 0.0), 0.0)
        self.max_life = max(_max_of(init.get("startLifetime"), 5.0), 0.01)
        self.max_count = int(min(int(init.get("maxNumParticles", 1000) or 1000), MAX_PER_EMITTER))
        self.gravity = curve_value(init.get("gravityModifier"), 0.0)
        emission = ps.get("EmissionModule") or {}
        on = bool(emission.get("enabled", 1))
        self.rate = max(curve_value(emission.get("rateOverTime"), 10.0), 0.0) if on else 0.0
        self.bursts = []
        if on:
            for b in (emission.get("m_Bursts") or [])[:int(emission.get("m_BurstCount", 8) or 0)]:
                if not isinstance(b, dict):
                    continue
                count = (curve_value(b["countCurve"], 0.0) if "countCurve" in b else
                         (float(b.get("minCount", 0)) + float(b.get("maxCount", 0))) / 2)
                self.bursts.append((float(b.get("time", 0.0)), int(round(count)),
                                    int(b.get("cycleCount", 1) or 0),  # 0 = repeat for the whole loop
                                    max(float(b.get("repeatInterval", 0.01) or 0.01), 0.01),
                                    float(b.get("probability", 1.0) if b.get("probability") is not None else 1.0)))
        sheet = ps.get("UVModule") or {}
        self.sheet = sheet if sheet.get("enabled") else None
        tx, ty = (int(sheet.get("tilesX", 1) or 1), int(sheet.get("tilesY", 1) or 1)) if self.sheet else (1, 1)
        self.tiles = (max(tx, 1), max(ty, 1))
        # one-shot effects (explosions, flashes) repeat in the preview
        self.period = self.delay + self.duration + self.max_life + 0.5

    def _module(self, name):
        m = self.ps.get(name) or {}
        return m if m.get("enabled") else None

    # ---- spawning
    def _spawns(self, t):
        """(spawn times, ids) of particles that may be alive at system time t (newest last)."""
        lo = t - self.max_life
        times, ids = [], []
        if self.rate > 0:
            k0 = int(np.ceil((lo - self.delay) * self.rate))
            k1 = int(np.floor((t - self.delay) * self.rate))
            if not self.prewarm:
                k0 = max(k0, 0)
            if not self.looping:
                k1 = min(k1, int(np.ceil(self.duration * self.rate)) - 1)
            k0 = max(k0, k1 - self.max_count * 2)
            if k1 >= k0:
                k = np.arange(k0, k1 + 1, dtype=np.int64)
                times.append(self.delay + k / self.rate)
                ids.append(k * 2)
        if self.bursts:
            first = int(np.floor((lo - self.delay) / self.duration))
            last = int(np.floor((t - self.delay) / self.duration))
            if not self.prewarm:
                first = max(first, 0)
            if not self.looping:
                first, last = 0, min(last, 0)
            for loop in range(max(first, last - 50), last + 1):
                start = self.delay + loop * self.duration
                for i, (when, count, cycles, interval, chance) in enumerate(self.bursts):
                    c = 0
                    while cycles == 0 or c < cycles:
                        bt = start + when + c * interval
                        if bt >= start + self.duration or bt > t or c > 1000:
                            break
                        bid = ((loop + 1000) * 64 + i) * 1024 + c
                        if bt >= lo and count > 0 and (chance >= 1 or _hash(np.array([bid]), 99)[0] < chance):
                            n = min(count, self.max_count)
                            times.append(np.full(n, bt))
                            ids.append((np.int64(bid) * 4096 + np.arange(n, dtype=np.int64)) * 2 + 1)
                        c += 1
        if not times:
            return np.zeros(0), np.zeros(0, np.int64)
        times, ids = np.concatenate(times), np.concatenate(ids)
        order = np.argsort(times, kind="stable")
        return times[order], ids[order]

    # ---- shape
    def _shape(self, ids):
        """Start positions and directions in the emitter's space."""
        n = len(ids)
        shape = self._module("ShapeModule") if "ShapeModule" in self.ps else None
        if shape is None:
            return np.zeros((n, 3)), np.tile([0.0, 0.0, 1.0], (n, 1))
        u = [_hash(ids, self.seed + 10 + i) for i in range(5)]
        kind = int(shape.get("type", 4) or 0)
        r = shape.get("radius", 1.0)
        radius = float(r.get("value", 1.0) if isinstance(r, dict) else r or 0.0)
        thick = float(shape.get("radiusThickness", 1.0) if shape.get("radiusThickness") is not None else 1.0)
        a = shape.get("arc", 360.0)
        arc = np.radians(float(a.get("value", 360.0) if isinstance(a, dict) else a or 360.0))
        phi = arc * u[0]
        if kind in (0, 1, 2, 3):  # spheres and hemispheres
            d = _sphere_dirs(u[1], u[2])
            if kind in (2, 3):
                d[:, 2] = np.abs(d[:, 2])
            rr = radius * (1 - thick + thick * np.cbrt(u[3])) if kind in (0, 2) else np.full(n, radius)
            pos, dirs = d * rr[:, None], d
        elif kind in (4, 7, 8, 9):  # cones along +z
            rho = 1 - thick + thick * np.sqrt(u[1]) if kind in (4, 8) else np.ones(n)
            ring = np.stack([np.cos(phi), np.sin(phi), np.zeros(n)], axis=1)
            tan = np.tan(np.radians(min(float(shape.get("angle", 25.0) or 0.0), 89.0)))
            dirs = _normalize(ring * (rho * tan)[:, None] + [0.0, 0.0, 1.0])
            pos = ring * (rho * radius)[:, None]
            if kind in (8, 9):
                pos = pos + dirs * (float(shape.get("length", 5.0) or 0.0) * u[2])[:, None]
        elif kind in (5, 15, 16, 18):  # boxes and rectangles, emitting along +z
            pos = np.stack(u[1:4], axis=1) - 0.5
            if kind == 18:
                pos[:, 2] = 0.0
            dirs = np.tile([0.0, 0.0, 1.0], (n, 1))
        elif kind in (10, 11, 17):  # circles and donuts: outwards in the xy plane
            ring = np.stack([np.cos(phi), np.sin(phi), np.zeros(n)], axis=1)
            rho = 1 - thick + thick * np.sqrt(u[1]) if kind == 10 else np.ones(n)
            pos, dirs = ring * (rho * radius)[:, None], ring
        elif kind == 12:  # edge along x, emitting along +y
            pos = np.stack([(2 * u[1] - 1) * radius, np.zeros(n), np.zeros(n)], axis=1)
            dirs = np.tile([0.0, 1.0, 0.0], (n, 1))
        else:  # meshes, sprites...: from the middle, any direction
            pos, dirs = np.zeros((n, 3)), _sphere_dirs(u[1], u[2])
        scale = np.array([float((shape.get("m_Scale") or {}).get(k, 1.0)) for k in "xyz"])
        if kind in (5, 15, 16) and "m_Scale" not in shape and "boxX" in shape:
            scale = np.array([float(shape.get(k, 1.0)) for k in ("boxX", "boxY", "boxZ")])
        rot = euler_matrix(*[float((shape.get("m_Rotation") or {}).get(k, 0.0)) for k in "xyz"])
        offset = np.array([float((shape.get("m_Position") or {}).get(k, 0.0)) for k in "xyz"])
        pos = (pos * scale) @ rot.T + offset
        dirs = dirs @ rot.T
        rand = float(shape.get("randomDirectionAmount", 0.0) or 0.0)
        if rand > 0:
            dirs = _normalize(dirs + (_sphere_dirs(u[3], u[4]) - dirs) * rand)
        spherical = float(shape.get("sphericalDirectionAmount", 0.0) or 0.0)
        if spherical > 0:
            dirs = _normalize(dirs + (_normalize(pos - offset) - dirs) * spherical)
        return pos, dirs

    # ---- one moment
    def simulate(self, t):
        st = t if self.looping else t % self.period
        spawn, ids = self._spawns(st)
        if not len(ids):
            return None
        init = self.init
        loop_x = ((spawn - self.delay) % self.duration) / self.duration
        age = st - spawn
        life = np.maximum(eval_minmax(init.get("startLifetime"), _hash(ids, self.seed + 1), loop_x, 5.0), 1e-3)
        alive = (age >= 0) & (age < life)
        if not alive.any():
            return None
        if alive.sum() > self.max_count:  # the newest ones stay, like Unity's particle cap
            alive &= np.cumsum(alive[::-1])[::-1] <= self.max_count
        ids, age, life, loop_x = ids[alive], age[alive], life[alive], loop_x[alive]
        f = age / life
        n = len(ids)
        speed = eval_minmax(init.get("startSpeed"), _hash(ids, self.seed + 2), loop_x, 5.0)
        size = eval_minmax(init.get("startSize"), _hash(ids, self.seed + 3), loop_x, 1.0)
        rot = eval_minmax(init.get("startRotation"), _hash(ids, self.seed + 4), loop_x, 0.0)
        color = eval_minmax_gradient(init.get("startColor"), _hash(ids, self.seed + 5), loop_x)
        pos, dirs = self._shape(ids)
        world, wvel = self._motion(ids, pos, dirs, speed, age, life)
        size = size * self.scale
        mod = self._module("SizeModule")
        if mod is not None:
            size = size * eval_minmax(mod.get("curve"), _hash(ids, self.seed + 8), f, 1.0)
        mod = self._module("ColorModule")
        if mod is not None:
            color = color * eval_minmax_gradient(mod.get("gradient"), _hash(ids, self.seed + 9), f)
        mod = self._module("RotationModule")
        if mod is not None:
            rot = rot + eval_minmax(mod.get("curve"), _hash(ids, self.seed + 15), f, 0.0) * age
        frame = np.zeros(n, np.int64)
        if self.sheet is not None:
            frames = self.tiles[0] * self.tiles[1]
            if int(self.sheet.get("animationType", 0) or 0) == 1:  # one row of the sheet
                frames = self.tiles[0]
            over = eval_minmax(self.sheet.get("frameOverTime"), _hash(ids, self.seed + 16), f, 0.0)
            start = eval_minmax(self.sheet.get("startFrame"), _hash(ids, self.seed + 17), f, 0.0)
            over = np.where(over > 1.001, over / frames, over)
            start = np.where(start > 1.001, start / frames, start)
            cycles = float(self.sheet.get("cycles", 1.0) or 1.0)
            frame = np.floor((start + over * cycles) * frames).astype(np.int64) % max(frames, 1)
        trails = None
        if self.trails is not None:
            trails = self._trails(ids, pos, dirs, speed, age, life, world, np.abs(size), color)
        world[:, 0] *= -1  # UniView space is x-flipped
        wvel[:, 0] *= -1
        return Particles(world, wvel, np.abs(size), -rot, np.clip(color, 0.0, 1.0), frame, trails)

    def _motion(self, ids, pos, dirs, speed, age, life):
        """(world positions, world velocities) in Unity space of particles `age` seconds after they were born."""
        n = len(ids)
        f = age / life
        a = age[:, None]
        vel = dirs * speed[:, None]                    # emitter space
        move = vel * a
        wvel, wmove = np.zeros((n, 3)), np.zeros((n, 3))  # world space
        mod = self._module("VelocityModule")
        if mod is not None:
            u = _hash(ids, self.seed + 6)
            extra = np.stack([eval_minmax(mod.get(k), u, f, 0.0) for k in "xyz"], axis=1)
            if mod.get("inWorldSpace"):
                wvel, wmove = wvel + extra, wmove + extra * a
            else:
                vel, move = vel + extra, move + extra * a
        mod = self._module("ForceModule")
        if mod is not None:
            u = _hash(ids, self.seed + 7)
            force = np.stack([eval_minmax(mod.get(k), u, f, 0.0) for k in "xyz"], axis=1)
            if mod.get("inWorldSpace"):
                wvel, wmove = wvel + force * a, wmove + 0.5 * force * a ** 2
            else:
                vel, move = vel + force * a, move + 0.5 * force * a ** 2
        g = np.array([0.0, -GRAVITY * self.gravity, 0.0])
        world = (pos + move) @ self.rot.T + self.matrix[:3, 3] + wmove + 0.5 * g * a ** 2
        return world, vel @ self.rot.T + wvel + g * a

    def _trails(self, ids, pos, dirs, speed, age, life, world, size, color):
        """Trails module at this moment: (points (M, K, 3) UniView space, widths (M, K), colors (M, K, 4)), newest
        point first, or None. "Particles" mode: each particle's path over the last trail lifetime (a fraction of
        its life); "Ribbon" mode: lines joining the particles in the order they were born."""
        tm = self.trails
        u = _hash(ids, self.seed + 20)
        keep = u < float(tm.get("ratio", 1.0) if tm.get("ratio") is not None else 1.0)
        if not keep.any():
            return None
        ribbon = int(tm.get("mode", 0) or 0) == 1
        if ribbon:
            count = max(1, int(tm.get("ribbonCount", 1) or 1))
            order = np.flatnonzero(keep)[::-1]  # newest first
            groups = [order[r::count] for r in range(count)]
            groups = [g for g in groups if len(g) >= 2]
            if not groups:
                return None
            k = max(len(g) for g in groups)
            idx = np.array([np.pad(g, (0, k - len(g)), mode="edge") for g in groups])  # (M, K)
            points, along = world[idx], np.linspace(0.0, 1.0, k)[None, :].repeat(len(idx), 0)
            base_width, base_color = size[idx], color[idx]
        else:
            sel = np.flatnonzero(keep)
            k = TRAIL_POINTS
            span = eval_minmax(tm.get("lifetime"), _hash(ids[sel], self.seed + 21), age[sel] / life[sel], 1.0)
            span = np.clip(span, 0.0, 1.0) * life[sel]
            along = np.linspace(0.0, 1.0, k)[None, :]
            back = np.maximum(age[sel, None] - span[:, None] * along, 0.0)  # (M, K) ages along the trail
            m = len(sel)
            rep_ = np.repeat(np.arange(m), k)
            pts, _v = self._motion(ids[sel][rep_], pos[sel][rep_], dirs[sel][rep_], speed[sel][rep_],
                                   back.reshape(-1), np.repeat(life[sel], k))
            points = pts.reshape(m, k, 3)
            along = along.repeat(m, 0)
            base_width = np.repeat(size[sel, None], k, 1)
            base_color = np.repeat(color[sel, None, :], k, 1)
        width = eval_minmax(tm.get("widthOverTrail"), np.zeros(along.size), along.reshape(-1), 1.0).reshape(along.shape)
        if tm.get("sizeAffectsWidth", 1):
            width = width * base_width
        trail_color = eval_minmax_gradient(tm.get("colorOverTrail"), np.zeros(along.size), along.reshape(-1))
        colors = trail_color.reshape(along.shape + (4,))
        if tm.get("inheritParticleColor", 1):
            colors = colors * base_color
        points = points.copy()
        points[..., 0] *= -1
        return points, np.abs(width), np.clip(colors, 0.0, 1.0)
