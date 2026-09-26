"""Playing Unity AnimationClips on skinned meshes: sample curves, pose the skeleton, skin the vertices."""

import numpy as np


def quat_to_mat(q):
    """(..., 4) x,y,z,w -> (..., 3, 3)."""
    q = q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], -2)


def euler_to_quat(deg):
    """Unity euler angles (degrees) -> quaternion x,y,z,w (rotation order Z, then X, then Y)."""
    x, y, z = np.radians(deg) / 2
    qx = np.array([np.sin(x), 0, 0, np.cos(x)])
    qy = np.array([0, np.sin(y), 0, np.cos(y)])
    qz = np.array([0, 0, np.sin(z), np.cos(z)])
    return _qmul(_qmul(qy, qx), qz)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz])


def trs(pos, rot, scale):
    m = np.eye(4)
    m[:3, :3] = quat_to_mat(np.asarray(rot, float)) * np.asarray(scale, float)[None, :]
    m[:3, 3] = pos
    return m


def _sample(keys, t):
    if keys is None or not len(keys):
        return None
    times = keys[:, 0]
    if t <= times[0]:
        return keys[0, 1]
    if t >= times[-1]:
        return keys[-1, 1]
    return float(np.interp(t, times, keys[:, 1]))


class Animator:
    """points_at(t) -> skinned vertex positions (N, 3) in UniView's space (x flipped like the mesh)."""

    def __init__(self, vertices, bone_indices, bone_weights, bind_poses, bone_keys, nodes, mesh_node, clip):
        self.vertices = np.hstack([np.asarray(vertices, float)[:, :3], np.ones((len(vertices), 1))])
        self.indices = np.asarray(bone_indices, np.int64)
        self.weights = np.asarray(bone_weights, float)
        s = self.weights.sum(axis=1, keepdims=True)
        self.weights = np.where(s > 0, self.weights / np.maximum(s, 1e-8), self.weights)
        self.bind = np.asarray(bind_poses, float)
        self.bone_keys = bone_keys
        self.nodes = nodes
        self.mesh_node = mesh_node
        self.length = max(float(clip.get("length") or 0.0), 1e-3)
        self.tracks = self._tracks(clip)
        self.matched = len(self.tracks)

    def _paths(self):
        """Full path of every node that matters (bones, their ancestors, the mesh node)."""
        paths = {}
        for key in set(self.bone_keys) | {self.mesh_node}:
            k = key
            while k is not None and k in self.nodes and k not in paths:
                chain, j = [], k
                while j is not None and j in self.nodes and len(chain) < 64:
                    chain.append(self.nodes[j]["name"])
                    j = self.nodes[j]["parent"]
                paths[k] = "/".join(reversed(chain))
                k = self.nodes[k]["parent"]
        return paths

    def _tracks(self, clip):
        """node key -> {"position": [x, y, z keys], "rotation": [...], "euler": [...], "scale": [...]}"""
        paths = self._paths()
        by_suffix = {}
        for key, path in paths.items():
            parts = path.split("/")
            for i in range(len(parts)):
                by_suffix.setdefault("/".join(parts[i:]), key)
        tracks = {}
        comp_index = {"x": 0, "y": 1, "z": 2, "w": 3}
        self.root_node = None
        for curve in clip.get("curves") or []:
            key = by_suffix.get(curve["path"])
            if key is None or curve["property"] not in ("position", "rotation", "euler", "scale"):
                continue
            if self.root_node is None:
                # Clip paths are relative to the Animator's object: climb one level per path part.
                root = key
                for _ in range(len(curve["path"].split("/"))):
                    root = self.nodes[root]["parent"] if root in self.nodes else None
                self.root_node = root
            prop = tracks.setdefault(key, {}).setdefault(curve["property"], [None] * 4)
            prop[comp_index.get(curve["component"], 0)] = np.asarray(curve["keys"], float)
        return tracks

    def _local(self, key, t):
        node = self.nodes[key]
        pos, rot, scale = list(node["pos"]), np.array(node["rot"], float), list(node["scale"])
        track = self.tracks.get(key)
        if track:
            for i, keys in enumerate(track.get("position", [])[:3]):
                v = _sample(keys, t) if keys is not None else None
                if v is not None:
                    pos[i] = v
            for i, keys in enumerate(track.get("scale", [])[:3]):
                v = _sample(keys, t) if keys is not None else None
                if v is not None:
                    scale[i] = v
            if "rotation" in track:
                q = [(_sample(k, t) if k is not None else rot[i]) for i, k in enumerate(track["rotation"])]
                rot = np.array(q, float)
            elif "euler" in track:
                e = [(_sample(k, t) if k is not None else 0.0) for k in track["euler"][:3]]
                rot = euler_to_quat(e)
        return trs(pos, rot, scale)

    def _world(self, key, t, cache):
        if key in cache:
            return cache[key]
        node = self.nodes.get(key)
        if node is None:
            return np.eye(4)
        local = self._local(key, t)
        parent = node["parent"]
        world = self._world(parent, t, cache) @ local if parent in self.nodes else local
        cache[key] = world
        return world

    def points_at(self, t):
        t = t % self.length
        cache = {}
        # Skin in the mesh's space, then place it relative to the animated character's root so it
        # stands the way it does in the game.
        mesh_world = self._world(self.mesh_node, t, cache) if self.mesh_node in self.nodes else np.eye(4)
        to_mesh = np.linalg.inv(mesh_world)
        display = (np.linalg.inv(self._world(self.root_node, t, cache)) @ mesh_world
                   if self.root_node in self.nodes else np.eye(4))
        mats = np.stack([display @ to_mesh @ self._world(k, t, cache) @ b if k in self.nodes else display
                         for k, b in zip(self.bone_keys, self.bind)])
        blended = np.einsum("nk,nkij->nij", self.weights, mats[np.clip(self.indices, 0, len(mats) - 1)])
        out = np.einsum("nij,nj->ni", blended, self.vertices)[:, :3].astype(np.float32)
        out[:, 0] *= -1  # Unity is left-handed; UniView meshes are x-flipped
        return out
