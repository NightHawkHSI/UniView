"""What a prefab/scene with nothing to draw in 3D is made of: its GameObject tree, components and the assets
they use (sprites, sounds, textures...). Works on the node list of an engine session's hierarchy()."""

MAX_PROPS = 400  # per component in the text (a ParticleSystem has thousands)
# Kinds worth listing as "uses" links, most interesting first.
LINK_ORDER = ("sprite", "texture", "audio", "model", "animation", "data", "font", "video", "text")


def visible_flags(nodes):
    """Per node: active itself and every parent active."""
    out = []
    for n in nodes:
        parent = n.get("parent", -1)
        out.append(bool(n.get("active", True)) and (parent < 0 or out[parent]))
    return out


def component_names(node):
    """Short names of a node's components ('Image', 'AudioSource'...), Transform left out."""
    names = []
    if node.get("mesh") or node.get("batch"):
        names.append("SkinnedMeshRenderer" if node.get("skinned") else "MeshRenderer")
    if node.get("light"):
        names.append("Light")
    if node.get("terrain"):
        names.append("Terrain")
    for c in node.get("components") or []:
        names.append((c.get("script") or c["type"]).rsplit(".", 1)[-1])
    return names


def _short_path(path):
    return path.replace(".Array.data", "")


def ref_label(entry, nodes, by_uid):
    """Text for a "ref" property entry."""
    if "node" in entry:
        i = entry["node"]
        name = nodes[i]["name"] if 0 <= i < len(nodes) else "?"
        return f"→ {name} ({entry.get('cls', '')})"
    uid = entry.get("asset")
    asset = by_uid.get(uid)
    if asset is not None:
        return f"→ {asset.name} [{asset.kind}]"
    if entry.get("kind") == "gameobject":
        return f"→ {entry.get('cls') or 'GameObject'} in another prefab"
    if entry.get("kind") == "material":
        return "→ material"
    return f"→ {uid}"


def prop_value(entry, nodes, by_uid):
    t = entry["t"]
    if t == "ref":
        return ref_label(entry, nodes, by_uid)
    if t == "s":
        return repr(entry["s"])
    if t == "b":
        return "true" if entry["v"] else "false"
    if t == "f":
        return f"{entry['v']:g}"
    return str(entry["v"])


def node_state(nodes, index, visible=None):
    """'inactive', tag and layer of a GameObject as short phrases."""
    n = nodes[index]
    state = []
    if not n.get("active", True):
        state.append("inactive")
    elif visible is not None and not visible[index]:
        state.append("hidden (a parent is inactive)")
    if n.get("tag") and n["tag"] != "Untagged":
        state.append(f"tag {n['tag']}")
    if n.get("layer"):
        state.append(f"layer {n['layer']}")
    return state


def components(node, nodes, by_uid):
    """A GameObject's components, Transform left out:
    [{"title", "script" (bool: a MonoBehaviour), "rows": [(field, value text, referenced Asset or None)]}]."""
    out = []
    if node.get("mesh") or node.get("batch"):
        uid = node.get("mesh") or node["batch"]["mesh"]
        mesh = by_uid.get(uid)
        name = "SkinnedMeshRenderer" if node.get("skinned") else "MeshRenderer"
        out.append({"title": name + ("" if node.get("renderer_enabled", True) else " (disabled)"), "script": False,
                    "rows": [("mesh", mesh.name if mesh is not None else uid, mesh)]})
    if node.get("light"):
        light = node["light"]
        kinds = {0: "Spot", 1: "Directional", 2: "Point", 3: "Area"}
        out.append({"title": f"Light ({kinds.get(light['type'], light['type'])})", "script": False,
                    "rows": [("intensity", f"{light['intensity']:g}", None), ("range", f"{light['range']:g}", None)]})
    for c in node.get("components") or []:
        props = c.get("props") or []
        rows = []
        for entry in props[:MAX_PROPS]:
            if entry["p"].endswith(".Array.size"):
                continue
            asset = by_uid.get(entry.get("asset")) if entry["t"] == "ref" else None
            rows.append((_short_path(entry["p"]), prop_value(entry, nodes, by_uid), asset))
        if len(props) > MAX_PROPS:
            rows.append((f"... {len(props) - MAX_PROPS:,} more values", "", None))
        out.append({"title": c.get("script") or c["type"], "script": c["type"] == "MonoBehaviour", "rows": rows})
    return out


def scripts(nodes):
    """Script classes used anywhere in the prefab, each once, in order of first use."""
    seen = {}
    for n in nodes:
        for c in n.get("components") or []:
            if c["type"] == "MonoBehaviour" and c.get("script"):
                seen.setdefault(c["script"], None)
    return list(seen)


def node_text(nodes, index, by_uid, visible=None):
    """Readable description of one GameObject and its components."""
    n = nodes[index]
    lines = [n["name"]]
    state = node_state(nodes, index, visible)
    if state:
        lines.append("  " + ", ".join(state))
    p, s = n.get("pos") or (0, 0, 0), n.get("scale") or (1, 1, 1)
    lines.append(f"  position ({p[0]:g}, {p[1]:g}, {p[2]:g})   scale ({s[0]:g}, {s[1]:g}, {s[2]:g})")
    for comp in components(n, nodes, by_uid):
        lines += ["", comp["title"]]
        for field, value, _asset in comp["rows"]:
            lines.append(f"  {field} = {value}" if value else f"  {field}")
    return "\n".join(lines)


def used_assets(nodes, by_uid, material_textures=None):
    """Assets the prefab points at (sprites, sounds, textures, data...), each once, most interesting kinds
    first. material_textures(material uid) -> [texture Asset] adds the textures of referenced materials."""
    seen, out = set(), []

    def add(asset):
        if asset is not None and asset.uid not in seen:
            seen.add(asset.uid)
            out.append(asset)

    for n in nodes:
        for uid in [n.get("mesh"), (n.get("batch") or {}).get("mesh"), n.get("terrain")]:
            if uid:
                add(by_uid.get(uid))
        materials = list(n.get("materials") or [])
        for c in n.get("components") or []:
            for entry in c.get("props") or []:
                if entry["t"] != "ref" or "asset" not in entry:
                    continue
                if entry.get("kind") == "material":
                    materials.append(entry["asset"])
                else:
                    add(by_uid.get(entry["asset"]))
        if material_textures is not None:
            for uid in materials:
                if uid:
                    for tex in material_textures(uid) or []:
                        add(tex)
    rank = {k: i for i, k in enumerate(LINK_ORDER)}
    out.sort(key=lambda a: rank.get(a.kind, len(rank)))
    return out


def summary(nodes):
    """One line: how many objects and what kinds of components (top few)."""
    counts = {}
    for n in nodes:
        for name in component_names(n):
            counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    parts = [f"{len(nodes):,} object(s)"] + [f"{c} {name}" for name, c in top]
    return " · ".join(parts)
