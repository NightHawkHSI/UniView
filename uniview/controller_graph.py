"""Animation state machines (GameSession.controller()) as a graph to draw: node layout and readable transition
text. No Qt here."""

MODES = {1: "", 2: "not ", 3: ">", 4: "<", 6: "==", 7: "!="}  # Unity's AnimatorConditionMode


def condition_text(c):
    """{"mode", "param", "threshold"} -> 'Speed > 0.1', 'Grounded', 'not Dead'."""
    mode = int(c.get("mode", 1))
    if mode in (1, 2):
        return f"{MODES[mode]}{c['param']}"
    t = float(c.get("threshold", 0.0))
    return f"{c['param']} {MODES.get(mode, '?')} {t:g}"


def transition_text(t, states):
    """One transition as a line: 'Run  (Speed > 0.1, exit 0.75, 0.25 s)'."""
    dest = states[t["dest"]]["name"] if 0 <= t.get("dest", -1) < len(states) else "Exit"
    parts = [condition_text(c) for c in t.get("conditions") or []]
    if t.get("has_exit_time"):
        parts.append(f"at {t.get('exit_time', 0):g} of the clip")
    if t.get("duration"):
        parts.append(f"blend {t['duration']:g}{' s' if t.get('fixed_duration', True) else ''}")
    return f"{dest}  ({', '.join(parts)})" if parts else dest


def sub_machine(state):
    """The sub-state machine a state was in ('Emotes', 'Locomotion.Ground'), '' for the layer itself. Builds
    flatten them, but each state keeps its full path ('Base Layer.Emotes.Wave')."""
    parts = (state.get("path") or "").split(".")
    return ".".join(parts[1:-1]) if len(parts) > 2 else ""


def motion_clips(motion):
    """Clip Assets a state plays (its clip, or every clip of its blend tree)."""
    if not motion:
        return []
    if motion.get("clip") is not None:
        return [motion["clip"]]
    return [n["clip"] for n in (motion.get("tree") or {}).get("nodes", []) if n.get("clip") is not None]


def layout(layer, max_rows=10):
    """{state index: (column, row)} for a layer: columns by steps from the default state (breadth first over
    transitions, Any State's targets one step in), states nothing reaches last; at most max_rows per column."""
    states = layer.get("states") or []
    n = len(states)
    if not n:
        return {}
    start = min(max(int(layer.get("default_state", 0)), 0), n - 1)
    depth = {start: 0}
    queue = [start]
    any_targets = [t["dest"] for t in layer.get("any_transitions") or [] if 0 <= t.get("dest", -1) < n]
    while queue:
        nxt = []
        for i in queue:
            targets = [t["dest"] for t in states[i].get("transitions") or [] if 0 <= t.get("dest", -1) < n]
            if i == start:
                targets += any_targets
            for d in targets:
                if d not in depth:
                    depth[d] = depth[i] + 1
                    nxt.append(d)
        queue = nxt
    last = max(depth.values()) + 1
    for i in range(n):
        depth.setdefault(i, last)
    out, col = {}, -1
    for d in sorted(set(depth.values())):
        # a sub-state machine's states stay together (they get a box around them)
        group = sorted((i for i in range(n) if depth[i] == d), key=lambda i: (sub_machine(states[i]), i))
        for k, i in enumerate(group):  # a long step (e.g. 100 states behind Any State) wraps into more columns
            if k % max_rows == 0:
                col += 1
            out[i] = (col, k % max_rows)
    return out


def edges(layer):
    """[(from, to, transition)] between states (from -1 = Any State); self transitions and exits left out."""
    states = layer.get("states") or []
    out = []
    for i, s in enumerate(states):
        for t in s.get("transitions") or []:
            if 0 <= t.get("dest", -1) < len(states) and t["dest"] != i:
                out.append((i, t["dest"], t))
    for t in layer.get("any_transitions") or []:
        if 0 <= t.get("dest", -1) < len(states):
            out.append((-1, t["dest"], t))
    return out
