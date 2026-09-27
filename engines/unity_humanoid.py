"""Unity humanoid (Mecanim "muscle") AnimationClips -> ordinary per-bone curves for one Avatar.

Humanoid clips don't store bone rotations: they store the body's position/orientation (RootT/RootQ)
and ~95 normalized muscle values, which each character's Avatar turns into bone rotations. This
redoes that with the Avatar's per-bone axes (pre/post rotations, limits, signs), so the result
plays through the same code as generic clips and exports the same way.
"""

import numpy as np

# Clip curve attributes (bindings on the Animator, class 95): 0-6 root motion, 7-9 RootT, 10-13 RootQ,
# 14-41 IK goals, 42-96 body muscles, 97-136 finger muscles.
ANIMATOR_CLASS = 95
ROOT_T, ROOT_Q, MUSCLES, FINGERS = 7, 10, 42, 97

# Human bones as the Avatar lists them (m_HumanBoneIndex).
HUMAN_BONES = ("Hips", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg", "LeftFoot", "RightFoot",
               "Spine", "Chest", "UpperChest", "Neck", "Head", "LeftShoulder", "RightShoulder", "LeftUpperArm",
               "RightUpperArm", "LeftLowerArm", "RightLowerArm", "LeftHand", "RightHand", "LeftToes", "RightToes",
               "LeftEye", "RightEye", "Jaw")
_B = {n: i for i, n in enumerate(HUMAN_BONES)}
X, Y, Z = 0, 1, 2  # X twists along the bone; Y and Z swing it


def _leg(side):
    return [(side + "UpperLeg", Z, "Upper Leg Front-Back"), (side + "UpperLeg", Y, "Upper Leg In-Out"),
            (side + "UpperLeg", X, "Upper Leg Twist In-Out"), (side + "LowerLeg", Z, "Lower Leg Stretch"),
            (side + "LowerLeg", X, "Lower Leg Twist In-Out"), (side + "Foot", Z, "Foot Up-Down"),
            (side + "Foot", Y, "Foot Twist In-Out"), (side + "Toes", Z, "Toes Up-Down")]


def _arm(side):
    return [(side + "Shoulder", Z, "Shoulder Down-Up"), (side + "Shoulder", Y, "Shoulder Front-Back"),
            (side + "UpperArm", Z, "Arm Down-Up"), (side + "UpperArm", Y, "Arm Front-Back"),
            (side + "UpperArm", X, "Arm Twist In-Out"), (side + "LowerArm", Z, "Forearm Stretch"),
            (side + "LowerArm", X, "Forearm Twist In-Out"), (side + "Hand", Z, "Hand Down-Up"),
            (side + "Hand", Y, "Hand In-Out")]


# Body muscles in clip order: (bone, axis, name).
BODY_MUSCLES = [(b, a, f"{b} {n}") for b, trio in (
    ("Spine", ("Front-Back", "Left-Right", "Twist Left-Right")),
    ("Chest", ("Front-Back", "Left-Right", "Twist Left-Right")),
    ("UpperChest", ("Front-Back", "Left-Right", "Twist Left-Right")),
    ("Neck", ("Nod Down-Up", "Tilt Left-Right", "Turn Left-Right")),
    ("Head", ("Nod Down-Up", "Tilt Left-Right", "Turn Left-Right"))) for a, n in zip((Z, Y, X), trio)]
BODY_MUSCLES += [("LeftEye", Z, "Left Eye Down-Up"), ("LeftEye", Y, "Left Eye In-Out"),
                 ("RightEye", Z, "Right Eye Down-Up"), ("RightEye", Y, "Right Eye In-Out"),
                 ("Jaw", Z, "Jaw Close"), ("Jaw", Y, "Jaw Left-Right")]
BODY_MUSCLES += [(b, a, ("Left " if side == "Left" else "Right ") + n) for side in ("Left", "Right")
                 for b, a, n in _leg(side)]
BODY_MUSCLES += [(b, a, ("Left " if side == "Left" else "Right ") + n) for side in ("Left", "Right")
                 for b, a, n in _arm(side)]

FINGER_NAMES = ("Thumb", "Index", "Middle", "Ring", "Little")
# Finger muscles in clip order: (hand, bone index in m_HandBoneIndex, axis, name); 3 bones per finger.
FINGER_MUSCLES = []
for _hand, _side in ((0, "Left"), (1, "Right")):
    for _f, _fname in enumerate(FINGER_NAMES):
        FINGER_MUSCLES += [(_hand, _f * 3, Z, f"{_side} {_fname} 1 Stretched"),
                           (_hand, _f * 3, Y, f"{_side} {_fname} Spread"),
                           (_hand, _f * 3 + 1, Z, f"{_side} {_fname} 2 Stretched"),
                           (_hand, _f * 3 + 2, Z, f"{_side} {_fname} 3 Stretched")]


def attribute_name(attr):
    """Readable name of a humanoid clip curve attribute."""
    if attr < ROOT_T:
        return "Motion" + ("T." + "xyz"[attr] if attr < 3 else "Q." + "xyzw"[attr - 3])
    if attr < ROOT_Q:
        return "RootT." + "xyz"[attr - ROOT_T]
    if attr < 14:
        return "RootQ." + "xyzw"[attr - ROOT_Q]
    if attr < MUSCLES:
        goal = ("LeftFoot", "RightFoot", "LeftHand", "RightHand")[(attr - 14) // 7]
        k = (attr - 14) % 7
        return f"{goal}{'T.' + 'xyz'[k] if k < 3 else 'Q.' + 'xyzw'[k - 3]}"
    if attr < FINGERS:
        return BODY_MUSCLES[attr - MUSCLES][2]
    if attr < FINGERS + len(FINGER_MUSCLES):
        return FINGER_MUSCLES[attr - FINGERS][3]
    return f"humanoid {attr}"


def is_humanoid(clip):
    """Muscle curves (not just the root motion generic clips can have too)."""
    return any(c.get("class_id") == ANIMATOR_CLASS and c.get("attribute", 0) >= MUSCLES for c in clip.get("curves", []))


# ---------------------------------------------------------------------------- quaternion helpers (x, y, z, w)

def _q(d):
    return np.array([d["x"], d["y"], d["z"], d["w"]], float)


def _v(d):
    return np.array([d["x"], d["y"], d["z"]], float)


def qmul(a, b):
    """Hamilton product of (..., 4) arrays."""
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], -1)


def qconj(q):
    return q * np.array([-1, -1, -1, 1.0])


def qrot(q, v):
    """Rotate vectors (..., 3) by unit quaternions (..., 4)."""
    u, w = q[..., :3], q[..., 3:]
    t = 2 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


def _normalize(q):
    return q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)


def _continuous(q):
    """Flip quaternions so consecutive frames stay on the same hemisphere (clean interpolation)."""
    q = q.copy()
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    return q


# ---------------------------------------------------------------------------- the Avatar

class HumanRig:
    """What an Avatar knows about its humanoid skeleton."""

    def __init__(self, avatar_tree):
        tos = {int(h) & 0xFFFFFFFF: p for h, p in avatar_tree.get("m_TOS") or []}
        av = avatar_tree["m_Avatar"]
        human = _data(av["m_Human"])
        sk = _data(human["m_Skeleton"])
        self.name = avatar_tree.get("m_Name", "")
        self.parents = [n["m_ParentId"] for n in sk["m_Node"]]
        self.axes_ids = [n["m_AxesId"] for n in sk["m_Node"]]
        self.axes = sk["m_AxesArray"]
        self.paths = [tos.get(int(i) & 0xFFFFFFFF) for i in sk["m_ID"]]
        pose = _data(human["m_SkeletonPose"])["m_X"]
        self.t = np.array([_v(x["t"]) for x in pose])
        self.q = np.array([_q(x["q"]) for x in pose])
        self.s = np.array([_v(x["s"]) for x in pose])
        self.mass = [float(m) for m in human.get("m_HumanBoneMass") or []]
        self.bones = list(human["m_HumanBoneIndex"])
        self.hands = [list(_data(human.get(k) or {}).get("m_HandBoneIndex") or []) for k in ("m_LeftHand", "m_RightHand")]
        self.scale = float(human.get("m_Scale") or 1.0)
        root = human["m_RootX"]
        self.root_t, self.root_q = _v(root["t"]), _normalize(_q(root["q"]))
        # The real (non-human) skeleton, to express the hips relative to their actual parent.
        ask = _data(av["m_AvatarSkeleton"])
        self.av_parents = [n["m_ParentId"] for n in ask["m_Node"]]
        self.av_paths = [tos.get(int(i) & 0xFFFFFFFF) for i in ask["m_ID"]]
        self.av_pose = [(_v(x["t"]), _normalize(_q(x["q"])), _v(x["s"])) for x in _data(av["m_DefaultPose"])["m_X"]]

    @property
    def valid(self):
        hips = self.bones[0] if self.bones else -1
        return 0 <= hips < len(self.paths) and bool(self.paths[hips])

    @property
    def human_paths(self):
        """Transform paths of the human bones (and fingers) this Avatar drives."""
        if not hasattr(self, "_human_paths"):
            nodes = [i for i in self.bones + self.hands[0] + self.hands[1] if 0 <= i < len(self.paths)]
            self._human_paths = {self.paths[i] for i in nodes if self.paths[i]}
        return self._human_paths

    def pose_world(self, rotations, frames):
        """node -> (positions (F, 3), rotations (F, 4)) in the Avatar's space, with `rotations`
        (node -> (F, 4) local rotations) applied over the T-pose; the root stays put."""
        world, scale = {}, {}
        for i, p in enumerate(self.parents):
            q = rotations.get(i)
            q = np.broadcast_to(self.q[i], (frames, 4)) if q is None else q
            if p < 0:
                world[i] = (np.broadcast_to(self.t[i], (frames, 3)), q)
                scale[i] = self.s[i]
                continue
            pt, pq = world[p]
            world[i] = (pt + qrot(pq, np.broadcast_to(self.t[i] * scale[p], (frames, 3))), qmul(pq, q))
            scale[i] = scale[p] * self.s[i]
        return world

    def body(self, world):
        """(center of mass (F, 3), orientation (F, 4)) of a posed skeleton, the way Mecanim measures
        RootT/RootQ: bone segment midpoints weighted by the Avatar's masses; up from the hips to the
        shoulders, right across the legs and arms."""
        pos = {b: world[self.bones[i]][0] for i, b in enumerate(HUMAN_BONES)
               if i < len(self.bones) and self.bones[i] >= 0}
        total, center = 0.0, 0.0
        for i, bone in enumerate(HUMAN_BONES):
            m = self.mass[i] if i < len(self.mass) else 0.0
            if bone not in pos or not m:
                continue
            end = next((c for c in _SEGMENT_END.get(bone, ()) if c in pos), None)
            center = center + m * ((pos[bone] + pos[end]) / 2 if end else pos[bone])
            total += m
        center = center / max(total, 1e-9)
        up = (pos["LeftUpperArm"] + pos["RightUpperArm"] - pos["LeftUpperLeg"] - pos["RightUpperLeg"]) / 2
        right = pos["RightUpperLeg"] - pos["LeftUpperLeg"] + pos["RightUpperArm"] - pos["LeftUpperArm"]
        up = up / np.maximum(np.linalg.norm(up, axis=-1, keepdims=True), 1e-9)
        right = right - up * np.sum(right * up, axis=-1, keepdims=True)
        right = right / np.maximum(np.linalg.norm(right, axis=-1, keepdims=True), 1e-9)
        forward = np.cross(right, up)
        q = np.array([_mat_quat(m) for m in np.stack([right, up, forward], -1)])
        return center, _continuous(q)

    def _av_parent_world(self, path):
        """4x4 matrix of the real skeleton parent of the node at `path` (default pose), or identity."""
        try:
            i = self.av_paths.index(path)
        except ValueError:
            return np.eye(4)
        m = np.eye(4)
        p = self.av_parents[i]
        chain = []
        while p >= 0:
            chain.append(p)
            p = self.av_parents[p]
        for j in reversed(chain):
            t, q, s = self.av_pose[j]
            local = np.eye(4)
            local[:3, :3] = _qmat(q) * s[None, :]
            local[:3, 3] = t
            m = m @ local
        return m

    def local_rotation(self, node, dof):
        """Local rotation(s) of a human bone from its three muscle angles (..., 3), in radians' limit space."""
        a = self.axes[self.axes_ids[node]]
        lo, hi, sgn = _v(a["m_Limit"]["m_Min"]), _v(a["m_Limit"]["m_Max"]), _v(a["m_Sgn"])
        angle = np.where(dof < 0, -dof * lo, dof * hi) * sgn  # lo is negative: -1 maps to it
        h = np.tan(np.clip(angle * 0.5, -1.5, 1.5))
        x, y, z = h[..., 0], h[..., 1], h[..., 2]
        zyroll = _normalize(np.stack([x, y + x * z, z - x * y, np.ones_like(x)], -1))
        pre, post = _normalize(_q(a["m_PreQ"])), _normalize(_q(a["m_PostQ"]))
        return _normalize(qmul(qmul(np.broadcast_to(pre, zyroll.shape), zyroll), np.broadcast_to(qconj(post), zyroll.shape)))


# Where each human bone's segment ends (first one the skeleton has), for the center of mass.
_SEGMENT_END = {"Hips": ("Spine",), "Spine": ("Chest",), "Chest": ("UpperChest", "Neck"), "UpperChest": ("Neck",),
                "Neck": ("Head",)}
for _s in ("Left", "Right"):
    _SEGMENT_END.update({_s + "UpperLeg": (_s + "LowerLeg",), _s + "LowerLeg": (_s + "Foot",),
                         _s + "Foot": (_s + "Toes",), _s + "Shoulder": (_s + "UpperArm",),
                         _s + "UpperArm": (_s + "LowerArm",), _s + "LowerArm": (_s + "Hand",)})


def _data(v):
    return v.get("data", v) if isinstance(v, dict) else v


def _qmat(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


# ---------------------------------------------------------------------------- conversion

def to_generic(clip, rig, fps=30.0, in_place=True):
    """Decoded humanoid clip (unity_anim.decode_clip) -> the same dict with transform curves for `rig`'s bones.

    in_place: keep the body over its starting spot (no walking off screen); the height still moves.
    """
    values = {}
    for c in clip.get("curves", []):
        if c.get("class_id") == ANIMATOR_CLASS and c["keys"]:
            values[c.get("attribute", -1)] = np.asarray(c["keys"], float)
    length = max(float(clip.get("length") or 0.0), 1e-3)
    frames = max(2, int(round(length * fps)) + 1)
    times = np.linspace(0.0, length, frames)

    def sample(attr, default=0.0):
        k = values.get(attr)
        if k is None:
            return np.full(frames, default)
        return np.interp(times, k[:, 0], k[:, 1])

    curves = []

    def emit(path, prop, arr):
        comps = "xyzw" if arr.shape[1] == 4 else "xyz"
        for i, comp in enumerate(comps):
            curves.append({"path": path, "property": prop, "component": comp,
                           "keys": [(round(float(t), 6), float(v)) for t, v in zip(times, arr[:, i])]})

    # Muscles -> bone rotations.
    dof = {}  # skeleton node -> (frames, 3)
    for m, (bone, axis, _n) in enumerate(BODY_MUSCLES):
        node = rig.bones[_B[bone]] if _B[bone] < len(rig.bones) else -1
        if node >= 0:
            dof.setdefault(node, np.zeros((frames, 3)))[:, axis] = sample(MUSCLES + m)
    for m, (hand, bone, axis, _n) in enumerate(FINGER_MUSCLES):
        idx = rig.hands[hand]
        node = idx[bone] if bone < len(idx) else -1
        if node >= 0:
            dof.setdefault(node, np.zeros((frames, 3)))[:, axis] = sample(FINGERS + m)
    # Body position/orientation -> hips. RootT/RootQ are the pose's center of mass and average
    # orientation, so pose the skeleton first, measure its body, then move the hips to match.
    rot = {node: _continuous(rig.local_rotation(node, d)) for node, d in dof.items()
           if rig.axes_ids[node] >= 0 and rig.paths[node]}
    for node, q in rot.items():
        emit(rig.paths[node], "rotation", q)
    world = rig.pose_world(rot, frames)
    center, body_q = rig.body(world)
    root_t = np.stack([sample(ROOT_T + i) for i in range(3)], -1) * rig.scale
    root_q = sample_rotation(values, ROOT_Q, times)
    if in_place:
        root_t[:, [0, 2]] = 0.0
    move_q = qmul(root_q, qconj(body_q))
    move_t = root_t - qrot(move_q, center)
    hips = rig.bones[0]
    hips_t, hips_q = world[hips]
    world_t = qrot(move_q, hips_t) + move_t
    world_q = qmul(move_q, hips_q)
    out = dict(clip)
    out["curves"] = curves + [c for c in clip.get("curves", []) if c.get("class_id") != ANIMATOR_CLASS]
    out["humanoid_avatar"] = rig.name
    # The hips relative to the animated character's root; place_hips() turns them into local curves
    # for a model's own skeleton (its hips' parents may split the same pose differently).
    out["hips"] = {"path": rig.paths[hips], "times": times, "t": world_t, "q": world_q}
    return place_hips(out, rig._av_parent_world(rig.paths[hips]))


def place_hips(clip, parent):
    """Clip with hips position/rotation curves local to their parent, whose matrix relative to the
    animated root is `parent` (4x4)."""
    hips = clip["hips"]
    inv = np.linalg.inv(parent)
    local_t = (inv[:3, :3] @ hips["t"].T).T + inv[:3, 3]
    parent_q = _mat_quat(parent[:3, :3] / np.linalg.norm(parent[:3, :3], axis=0, keepdims=True))
    local_q = _continuous(_normalize(qmul(np.broadcast_to(qconj(parent_q), hips["q"].shape), hips["q"])))
    curves = [c for c in clip["curves"] if not (c["path"] == hips["path"] and c["property"] == "position")
              and not (c["path"] == hips["path"] and c["property"] == "rotation")]
    for prop, arr in (("position", local_t), ("rotation", local_q)):
        for i, comp in enumerate("xyzw"[:arr.shape[1]]):
            curves.append({"path": hips["path"], "property": prop, "component": comp,
                           "keys": [(round(float(t), 6), float(v)) for t, v in zip(hips["times"], arr[:, i])]})
    out = dict(clip)
    out["curves"] = curves
    return out


def sample_rotation(values, first_attr, times):
    """(frames, 4) quaternions from four x/y/z/w curves. Keys may flip sign between frames (q and -q
    are the same rotation), which breaks per-component interpolation: line them up first."""
    comps = [values.get(first_attr + i) for i in range(4)]
    key_times = np.unique(np.concatenate([k[:, 0] for k in comps if k is not None] or [np.zeros(1)]))
    keys = np.stack([np.interp(key_times, k[:, 0], k[:, 1]) if k is not None else
                     np.full(len(key_times), 1.0 if i == 3 else 0.0) for i, k in enumerate(comps)], -1)
    keys = _continuous(_normalize(keys))
    return _normalize(np.stack([np.interp(times, key_times, keys[:, i]) for i in range(4)], -1))


def _mat_quat(m):
    """Rotation matrix -> quaternion (x, y, z, w)."""
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = np.sqrt(tr + 1.0) * 2
        return np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s])
    i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = np.sqrt(1.0 + m[i, i] - m[j, j] - m[k, k]) * 2
    q = np.zeros(4)
    q[i] = 0.25 * s
    q[j] = (m[j, i] + m[i, j]) / s
    q[k] = (m[k, i] + m[i, k]) / s
    q[3] = (m[k, j] - m[j, k]) / s
    return q
