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
        return trs(*self.local_trs(key, t))

    def local_trs(self, key, t):
        """(position, rotation x/y/z/w, scale) of a node at time t (its rest values where not animated)."""
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
        return pos, rot, scale

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

    def _display(self, t, cache):
        """Skin in the mesh's space, then place it relative to the animated character's root so it
        stands the way it does in the game: (display matrix, world -> mesh matrix)."""
        mesh_world = self._world(self.mesh_node, t, cache) if self.mesh_node in self.nodes else np.eye(4)
        display = (np.linalg.inv(self._world(self.root_node, t, cache)) @ mesh_world
                   if self.root_node in self.nodes else np.eye(4))
        return display, np.linalg.inv(mesh_world)

    def bones_at(self, t):
        """(B, 3) bone positions at time t in UniView's space, in bone_keys order (rest pose for unknown ones)."""
        t = t % self.length
        cache = {}
        display, to_mesh = self._display(t, cache)
        out = np.array([(display @ to_mesh @ self._world(k, t, cache))[:3, 3] if k in self.nodes
                        else np.linalg.inv(b)[:3, 3] for k, b in zip(self.bone_keys, self.bind)], np.float32)
        out = out.reshape(-1, 3)
        out[:, 0] *= -1
        return out

    def points_at(self, t):
        t = t % self.length
        cache = {}
        display, to_mesh = self._display(t, cache)
        mats = np.stack([display @ to_mesh @ self._world(k, t, cache) @ b if k in self.nodes else display
                         for k, b in zip(self.bone_keys, self.bind)])
        blended = np.einsum("nk,nkij->nij", self.weights, mats[np.clip(self.indices, 0, len(mats) - 1)])
        out = np.einsum("nij,nj->ni", blended, self.vertices)[:, :3].astype(np.float32)
        out[:, 0] *= -1  # Unity is left-handed; UniView meshes are x-flipped
        return out


# ---------------------------------------------------------------------------- rigged exports (glTF)
# UniView shows Unity's left-handed data mirrored on x; transforms are mirrored the same way.

_MIRROR = np.diag([-1.0, 1.0, 1.0, 1.0])


def _mirror_trs(pos, rot, scale):
    return ([-float(pos[0]), float(pos[1]), float(pos[2])],
            [float(rot[0]), -float(rot[1]), -float(rot[2]), float(rot[3])], [float(v) for v in scale])


def export_rig(nodes, bone_keys, bind_poses, bone_indices, bone_weights, mesh_node, root=None):
    """Skeleton of a skinned mesh for glTF, in UniView's (mirrored) space.

    Joints are the bones plus their ancestors up to `root` (the animated character's root when a clip
    plays; otherwise the bones' closest common ancestor), which is kept at the origin.
    Returns (rig dict, {node key: joint index}).
    """
    chains = []
    for key in [k for k in bone_keys if k in nodes] + ([mesh_node] if mesh_node in nodes else []):
        chain, k = [], key
        while k is not None and k in nodes and len(chain) < 256:
            chain.append(k)
            if k == root:
                break
            k = nodes[k]["parent"]
        chains.append(chain[::-1])  # top ... node
    if not chains:
        raise ValueError("This model's bones aren't in the game files.")
    if root is None or not all(c[0] == root for c in chains):
        # Closest common ancestor of everything.
        common = chains[0]
        for c in chains[1:]:
            n = 0
            while n < min(len(common), len(c)) and common[n] == c[n]:
                n += 1
            common = common[:n]
        root = common[-1] if common else None
        chains = [c[c.index(root):] if root in c else c for c in chains]
    order, joint_of = [], {}
    for chain in chains:
        for k in chain:
            if k not in joint_of:
                joint_of[k] = len(order)
                order.append(k)
    joints = []
    for k in order:
        node = nodes[k]
        parent = node["parent"] if k != root else None
        if k == root or parent not in joint_of:
            pos, rot, scale = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 1.0]  # the character's origin
            parent = None
        else:
            pos, rot, scale = _mirror_trs(node["pos"], node["rot"], node["scale"])
        joints.append({"name": node["name"] or f"joint{len(joints)}",
                       "parent": joint_of[parent] if parent is not None else -1,
                       "translation": pos, "rotation": rot, "scale": scale})
    top = joint_of[order[0]]
    skin_joints = [joint_of.get(k, top) for k in bone_keys]
    inverse_bind = np.stack([_MIRROR @ np.asarray(b, float) @ _MIRROR if k in joint_of else np.eye(4)
                             for k, b in zip(bone_keys, bind_poses)])
    indices = np.clip(np.asarray(bone_indices, np.int64).reshape(len(bone_indices), -1), 0, len(bone_keys) - 1)
    weights = np.asarray(bone_weights, np.float32).reshape(len(bone_weights), -1)
    if weights.shape[1] > 4:  # glTF's JOINTS_0/WEIGHTS_0 hold 4 influences: keep the strongest
        top = np.argsort(-weights, axis=1)[:, :4]
        weights = np.take_along_axis(weights, top, 1)
        indices = np.take_along_axis(indices, top, 1)
    elif weights.shape[1] < 4:
        pad = 4 - weights.shape[1]
        weights = np.hstack([weights, np.zeros((len(weights), pad), np.float32)])
        indices = np.hstack([indices, np.zeros((len(indices), pad), np.int64)])
    total = weights.sum(axis=1, keepdims=True)
    weights = np.where(total > 0, weights / np.maximum(total, 1e-8), np.array([1, 0, 0, 0], np.float32))
    rig = {"joints": joints, "skin_joints": skin_joints, "inverse_bind": inverse_bind,
           "joints_0": indices.astype(np.uint16), "weights_0": weights.astype(np.float32)}
    return rig, joint_of


def export_animation(animator, joint_of, name, fps=30.0):
    """The clip playing in `animator`, sampled to glTF channels for the joints in `joint_of`."""
    length = animator.length
    times = np.linspace(0.0, length, max(2, int(round(length * fps)) + 1))
    channels = []
    root = animator.root_node
    for key, track in animator.tracks.items():
        joint = joint_of.get(key)
        if joint is None or key == root:
            continue
        samples = [_mirror_trs(*animator.local_trs(key, float(t))) for t in times]
        if "position" in track:
            channels.append({"joint": joint, "path": "translation", "values": np.array([s[0] for s in samples])})
        if "rotation" in track or "euler" in track:
            rot = np.array([s[1] for s in samples])
            for i in range(1, len(rot)):
                if np.dot(rot[i], rot[i - 1]) < 0:
                    rot[i] = -rot[i]
            rot /= np.maximum(np.linalg.norm(rot, axis=1, keepdims=True), 1e-12)
            channels.append({"joint": joint, "path": "rotation", "values": rot})
        if "scale" in track:
            channels.append({"joint": joint, "path": "scale", "values": np.array([s[2] for s in samples])})
    return {"name": name, "times": times.astype(np.float32), "channels": channels}
