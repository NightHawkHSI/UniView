"""Particle playback geometry: the particles an emitter simulates (MeshData.emitters) as camera-facing quads or
mesh copies, ready to draw. No Qt here."""

import numpy as np

MAX_PLAYING = 200       # emitters played at once (the ones nearest the camera)
MAX_PARTICLES = 40_000  # particles drawn per frame at most


def nearest(emitters, position, k=MAX_PLAYING):
    """Indices of the k emitters closest to a point (all of them if there are no more than k)."""
    if len(emitters) <= k:
        return list(range(len(emitters)))
    centers = np.array([e.center for e in emitters], float)
    dist = np.einsum("ij,ij->i", centers - position, centers - position)
    return list(np.argpartition(dist, k)[:k])


def camera_axes(position, focal_point, up):
    """(view direction, right, up) unit vectors of a camera."""
    view = np.asarray(focal_point, float) - np.asarray(position, float)
    view /= np.linalg.norm(view) or 1.0
    right = np.cross(view, up)
    if np.linalg.norm(right) < 1e-6:
        right = np.cross(view, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right) or 1.0
    return view, right, np.cross(right, view)


def _unit(v, fallback):
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return np.where(n > 1e-9, v / np.maximum(n, 1e-9), fallback)


def frame_uvs(frame, tiles):
    """(N, 4, 2) corner UVs of each particle's texture sheet frame (frame 0 = top-left)."""
    tx, ty = tiles
    col, row = frame % tx, frame // tx
    u0, u1 = col / tx, (col + 1) / tx
    v1 = 1.0 - row / ty
    v0 = v1 - 1.0 / ty
    return np.stack([np.stack([u0, v0], 1), np.stack([u1, v0], 1), np.stack([u1, v1], 1), np.stack([u0, v1], 1)], 1)


def quads(emitter, p, axes):
    """(points (M, 3), uvs (M, 2), colors (M, 4) 0..1, triangles (T, 3)) of one emitter's particles p."""
    n = len(p)
    view, right, up = axes
    h = (p.size / 2)[:, None]
    if emitter.render_mode == "mesh" and emitter.mesh is not None:
        pts, tris, uv = emitter.mesh
        m = len(pts)
        points = (pts[None, :, :] * p.size[:, None, None] + p.pos[:, None, :]).reshape(-1, 3)
        uvs = np.tile(uv if uv is not None else np.zeros((m, 2)), (n, 1))
        triangles = (tris[None, :, :] + (np.arange(n) * m)[:, None, None]).reshape(-1, 3)
        return points, uvs, np.repeat(p.color, m, axis=0), triangles
    if emitter.render_mode == "stretch":
        d = _unit(p.vel, up)
        r = _unit(np.cross(d, view), right)
        length = p.size * emitter.length_scale + np.linalg.norm(p.vel, axis=1) * emitter.velocity_scale
        tail = p.pos - d * length[:, None]
        corners = np.stack([tail - r * h, tail + r * h, p.pos + r * h, p.pos - r * h], 1)
    else:
        if emitter.render_mode == "horizontal":
            right_n, up_n = np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0])
        elif emitter.render_mode == "vertical":
            up_n = np.array([0.0, 1.0, 0.0])
            right_n = np.cross(view, up_n)
            right_n = right_n / np.linalg.norm(right_n) if np.linalg.norm(right_n) > 1e-6 else right
        else:
            right_n, up_n = right, up
        c, s = np.cos(p.rot)[:, None], np.sin(p.rot)[:, None]
        r = c * right_n + s * up_n
        u = c * up_n - s * right_n
        corners = np.stack([p.pos - (r + u) * h, p.pos + (r - u) * h, p.pos + (r + u) * h, p.pos - (r - u) * h], 1)
    base = (np.arange(n) * 4)[:, None]
    triangles = np.concatenate([base + [0, 1, 2], base + [0, 2, 3]])
    return (corners.reshape(-1, 3), frame_uvs(p.frame, emitter.tiles).reshape(-1, 2),
            np.repeat(p.color, 4, axis=0), triangles)


def trail_strips(trails, axes):
    """(points, uvs, colors, triangles) of Particles.trails as camera-facing strips (u along the trail)."""
    pts, widths, colors = trails
    m, k = widths.shape
    view = axes[0]
    d = np.zeros_like(pts)
    d[:, 1:-1] = pts[:, 2:] - pts[:, :-2]
    d[:, 0] = pts[:, 1] - pts[:, 0]
    d[:, -1] = pts[:, -1] - pts[:, -2]
    side = np.cross(d, view)
    norm = np.linalg.norm(side, axis=2, keepdims=True)
    side = np.where(norm > 1e-9, side / np.maximum(norm, 1e-9), axes[1]) * (widths / 2)[..., None]
    points = np.stack([pts - side, pts + side], axis=2).reshape(-1, 3)        # (M, K, 2) -> rows
    u = np.repeat(np.linspace(0.0, 1.0, k)[None, :], m, 0)
    uvs = np.stack([np.stack([u, np.zeros_like(u)], -1), np.stack([u, np.ones_like(u)], -1)], axis=2).reshape(-1, 2)
    cols = np.repeat(colors[:, :, None, :], 2, axis=2).reshape(-1, 4)
    base = (np.arange(m)[:, None] * k + np.arange(k - 1)[None, :]).reshape(-1) * 2
    tris = np.concatenate([np.stack([base, base + 2, base + 3], 1), np.stack([base, base + 3, base + 1], 1)])
    return points, uvs, cols, tris


def frame_geometry(emitters, indices, t, axes, look_of, trail_look_of=None):
    """Everything to draw at time t: {look key: (points, uvs, colors uint8, triangles)}, particle count.
    look_of(emitter index) -> a hashable key; emitters sharing a key are drawn as one mesh. trail_look_of(index)
    -> the key of an emitter's trails (Trails module), or None to skip them."""
    pieces, total = {}, 0
    for i in indices:
        e = emitters[i]
        try:
            p = e.simulate(t)
        except Exception:
            continue
        if p is None or not len(p) or total >= MAX_PARTICLES:
            continue
        total += len(p)
        if e.render_mode != "none":
            pieces.setdefault(look_of(i), []).append(quads(e, p, axes))
        if p.trails is not None and trail_look_of is not None and p.trails[1].shape[1] >= 2:
            key = trail_look_of(i)
            if key is not None:
                pieces.setdefault(key, []).append(trail_strips(p.trails, axes))
    out = {}
    for key, parts in pieces.items():
        offset, tris = 0, []
        for pts, _uv, _c, t_ in parts:
            tris.append(t_ + offset)
            offset += len(pts)
        colors = np.concatenate([c for _p, _u, c, _t in parts])
        out[key] = (np.concatenate([pts for pts, *_ in parts]).astype(np.float32),
                    np.concatenate([uv for _p, uv, _c, _t in parts]).astype(np.float32),
                    (np.clip(colors, 0.0, 1.0) * 255).astype(np.uint8), np.concatenate(tris))
    return out, total
