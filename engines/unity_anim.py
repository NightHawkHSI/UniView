"""Unity AnimationClip decoding: keyframes of every animated property.

Mecanim clips pack their curves in three blocks - streamed (keyframes), dense (sampled every
frame) and constant (one value) - and name the animated objects by a CRC32 of their transform
path. The decoding follows AssetStudio's AnimationClip / StreamedClip code (MIT License).
"""

import math
import struct
import zlib

import numpy as np

TRANSFORM_ATTRIBUTES = {1: ("position", 3), 2: ("rotation", 4), 3: ("scale", 3), 4: ("euler", 3)}


def _streamed_frames(words):
    """StreamedClip data (uint32 words) -> [(time, [(curve index, value)])]."""
    data = np.asarray(words, dtype=np.uint32).tobytes()
    frames, pos = [], 0
    while pos + 8 <= len(data):
        time, count = struct.unpack_from("<fi", data, pos)
        pos += 8
        keys = []
        for _ in range(count):
            index, _a, _b, _c, value = struct.unpack_from("<i4f", data, pos)
            pos += 20
            keys.append((index, value))
        frames.append((time, keys))
    return frames


def _bindings(tree):
    """[(path hash, attribute, curve count, property name)] in curve order."""
    out = []
    for b in (tree.get("m_ClipBindingConstant") or {}).get("genericBindings") or []:
        attribute, type_id = b.get("attribute", 0), b.get("typeID", 0)
        if type_id == 4 and attribute in TRANSFORM_ATTRIBUTES:
            name, count = TRANSFORM_ATTRIBUTES[attribute]
        else:
            name, count = f"property {attribute} (class {type_id})", 1
        out.append((b.get("path", 0), attribute, count, name))
    return out


def decode_clip(tree, path_names=None):
    """AnimationClip typetree -> summary dict with curves: [{path, property, component, keys: [[t, v]]}]."""
    path_names = path_names or {}
    muscle = tree.get("m_MuscleClip") or {}
    clip = muscle.get("m_Clip") or {}
    clip = clip.get("data", clip)
    streamed = clip.get("m_StreamedClip") or {}
    dense = clip.get("m_DenseClip") or {}
    constant = clip.get("m_ConstantClip") or {}
    n_streamed = streamed.get("curveCount", 0) or 0
    n_dense = dense.get("m_CurveCount", 0) or 0
    keys = {}  # curve index -> [(t, v)]
    for time, frame_keys in _streamed_frames(streamed.get("data") or []):
        if not math.isfinite(time) or time < -1e30:
            continue
        for index, value in frame_keys:
            keys.setdefault(index, []).append((round(time, 6), value))
    samples = np.asarray(dense.get("m_SampleArray") or [], dtype=np.float32)
    if n_dense and len(samples):
        frames = len(samples) // n_dense
        rate = dense.get("m_SampleRate") or tree.get("m_SampleRate") or 30
        begin = dense.get("m_BeginTime", 0.0)
        grid = samples[:frames * n_dense].reshape(frames, n_dense)
        for c in range(n_dense):
            keys[n_streamed + c] = [(round(begin + f / rate, 6), float(grid[f, c])) for f in range(frames)]
    for c, value in enumerate(constant.get("data") or []):
        keys[n_streamed + n_dense + c] = [(round(muscle.get("m_StartTime", 0.0), 6), float(value))]

    curves = []
    index = 0
    component_names = {3: "xyz", 4: "xyzw"}
    for path_hash, _attr, count, prop in _bindings(tree):
        path = path_names.get(path_hash, f"#{path_hash:08x}")
        for k in range(count):
            comp = component_names.get(count, "")[k] if count > 1 else ""
            if index in keys:
                curves.append({"path": path, "property": prop, "component": comp, "keys": keys[index]})
            index += 1
    # Legacy clips keep readable curves directly.
    for field, prop in (("m_PositionCurves", "position"), ("m_RotationCurves", "rotation"),
                        ("m_ScaleCurves", "scale"), ("m_EulerCurves", "euler")):
        for curve in tree.get(field) or []:
            frames = (curve.get("curve") or {}).get("m_Curve") or []
            comps = "xyzw" if prop == "rotation" else "xyz"
            for k, comp in enumerate(comps):
                pts = []
                for fr in frames:
                    v = fr.get("value")
                    v = [v.get(c) for c in comps] if isinstance(v, dict) else v
                    if isinstance(v, (list, tuple)) and k < len(v):
                        pts.append((round(fr.get("time", 0.0), 6), float(v[k])))
                if pts:
                    curves.append({"path": curve.get("path", ""), "property": prop, "component": comp, "keys": pts})
    for curve in tree.get("m_FloatCurves") or []:
        frames = (curve.get("curve") or {}).get("m_Curve") or []
        pts = [(round(fr.get("time", 0.0), 6), float(fr.get("value", 0.0))) for fr in frames]
        if pts:
            curves.append({"path": curve.get("path", ""), "property": curve.get("attribute", "float"),
                           "component": "", "keys": pts})
    length = float(muscle.get("m_StopTime", 0.0) or 0.0) - float(muscle.get("m_StartTime", 0.0) or 0.0)
    if length <= 0:
        length = max((k[-1][0] for k in (c["keys"] for c in curves) if k), default=0.0)
    return {
        "name": tree.get("m_Name", ""),
        "length": round(length, 4),
        "sample_rate": tree.get("m_SampleRate", 0),
        "legacy": bool(tree.get("m_Legacy")),
        "wrap_mode": tree.get("m_WrapMode", 0),
        "events": [{"time": e.get("time"), "function": e.get("functionName")} for e in tree.get("m_Events") or []],
        "curves": curves,
    }


def summary_text(info):
    lines = [f"{info['name']}", "",
             f"Length: {info['length']:.3f} s   Sample rate: {info['sample_rate']} fps   "
             f"{'Legacy' if info['legacy'] else 'Mecanim'} clip", f"Curves: {len(info['curves'])}"]
    if info["events"]:
        lines.append("Events: " + ", ".join(f"{e['function']} @ {e['time']:.2f}s" for e in info["events"][:20]))
    lines += ["", "Animated properties (path: property, keyframes):"]
    grouped = {}
    for c in info["curves"]:
        grouped.setdefault((c["path"], c["property"]), []).append(len(c["keys"]))
    for (path, prop), counts in list(grouped.items())[:400]:
        lines.append(f"  {path or '(root)'}: {prop}  ({max(counts)} keys)")
    if len(grouped) > 400:
        lines.append(f"  ... and {len(grouped) - 400} more")
    lines += ["", "Save... exports every curve's keyframes as JSON (time, value) for use in other tools.",
              "Playing animations on their model isn't supported yet."]
    return "\n".join(lines)


def path_hash(path):
    return zlib.crc32(path.encode("utf-8")) & 0xFFFFFFFF
