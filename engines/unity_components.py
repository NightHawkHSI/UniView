"""Unity components as flat lists of saved values, so an editor script can put them back.

Every component's saved data uses the same field names as Unity's editor serialization (m_Center,
m_IsTrigger, m_audioClip...), so a component can be rebuilt generically: add a component of the same
type and set each field through SerializedObject. This module turns a component's type tree into
[{"p": property path, "t": kind, ...}] entries in the order the editor needs them (array sizes
before their elements). References become either another object of the same prefab/scene or an
asset the exporter wrote.
"""

# Handled elsewhere (the tree itself, renderers) or needing the game's code (scripts).
SKIP_COMPONENTS = {"Transform", "RectTransform", "MeshFilter", "MeshRenderer", "SkinnedMeshRenderer"}
# Bookkeeping fields every object has; they describe the file, not the component.
SKIP_FIELDS = {"m_GameObject", "m_ObjectHideFlags", "m_CorrespondingSourceObject", "m_PrefabInstance",
               "m_PrefabAsset", "m_PrefabParentObject", "m_PrefabInternal", "m_Script", "m_EditorHideFlags",
               "m_EditorClassIdentifier", "m_Name"}
MAX_ENTRIES = 50_000  # per component (a huge ParticleSystem is ~5k)
ASSET_KINDS = {"Mesh": "mesh", "Material": "material", "Texture2D": "file", "AudioClip": "file", "Font": "file",
               "VideoClip": "file", "TextAsset": "file", "Cubemap": "file"}


def flatten(tree, resolve):
    """[{"p": path, "t": "f"|"i"|"b"|"s"|"ref", ...}] for a component's type tree.

    resolve(file_id, path_id) -> None (leave unset) or a dict merged into a "ref" entry, e.g.
    {"node": 3, "cls": "Transform"} or {"asset": uid, "kind": "mesh"}. A null reference (path id 0)
    is written as a ref with nothing, so the field ends up empty like in the game."""
    out = []

    def add(entry):
        if len(out) < MAX_ENTRIES:
            out.append(entry)

    def walk(value, path, depth):
        if depth > 40 or len(out) >= MAX_ENTRIES:
            return
        if isinstance(value, dict):
            if set(value) == {"m_FileID", "m_PathID"}:
                if not value["m_PathID"]:
                    return  # null reference: the new component's default is null too
                target = resolve(value["m_FileID"], value["m_PathID"])
                if target:
                    add({"p": path, "t": "ref", **target})
                return
            for key, item in value.items():
                if not path and (key in SKIP_FIELDS or key.startswith("(")):  # "(text in undecoded data)"
                    continue
                walk(item, f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(value, (list, tuple)):
            if isinstance(value, tuple) and len(value) == 2 and path:  # (key, value) pair of a map
                walk(value[0], f"{path}.first", depth + 1)
                walk(value[1], f"{path}.second", depth + 1)
                return
            add({"p": f"{path}.Array.size", "t": "i", "v": len(value)})
            for i, item in enumerate(value):
                walk(item, f"{path}.Array.data[{i}]", depth + 1)
        elif isinstance(value, bool):
            add({"p": path, "t": "b", "v": int(value)})
        elif isinstance(value, int):
            add({"p": path, "t": "i", "v": value})
        elif isinstance(value, float):
            add({"p": path, "t": "f", "v": value})
        elif isinstance(value, str):
            add({"p": path, "t": "s", "s": value})
        # bytes and anything else: not settable through SerializedObject, left out

    walk(tree, "", 0)
    return out


def baked_probe_as_custom(props):
    """A baked ReflectionProbe's cubemap lives in the game's lighting data, which an editor project doesn't have:
    turn it into a Custom probe (m_Mode 2) showing that cubemap, until the scene is baked again."""
    mode = next((e for e in props if e["p"] == "m_Mode"), None)
    baked = next((e for e in props if e["p"] == "m_BakedTexture" and e["t"] == "ref"), None)
    if mode is None or mode.get("v") != 0 or baked is None:
        return props
    out = [({**e, "v": 2} if e is mode else e) for e in props if e["p"] not in ("m_BakedTexture", "m_CustomBakedTexture")]
    return out + [{**baked, "p": "m_CustomBakedTexture"}]
