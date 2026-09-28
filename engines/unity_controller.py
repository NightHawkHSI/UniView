"""AnimatorControllers as builds store them (compiled: hashes instead of names, flattened state machines)
turned back into parameters, layers, states, blend trees and transitions with names.

Every name is recovered from the controller's own hash -> string table (m_TOS). Blend trees are a flat
node list (children by index), which is also how UniViewBuilder.cs reads them back.
"""

PARAM_TYPES = {1: "Float", 3: "Int", 4: "Bool", 9: "Trigger"}
# AnimatorConditionMode values are the same in the compiled data and the editor API.
BLEND_TYPES = {0: "Simple1D", 1: "SimpleDirectional2D", 2: "FreeformDirectional2D", 3: "FreeformCartesian2D",
               4: "Direct"}
NONE = 0xFFFFFFFF


def _d(value):
    """Compiled constants are wrapped as {"data": {...}} (OffsetPtr); unwrap."""
    return value.get("data", value) if isinstance(value, dict) else value


def decode_controller(tree, clip_uid):
    """tree: AnimatorController type tree. clip_uid(index into m_AnimationClips) -> exported clip uid or None.

    Returns {"name", "parameters": [{"name", "type", "default"}], "layers": [{"name", "weight", "blending",
    "ik", "default_state", "states": [...], "any_transitions": [...]}]}."""
    names = {int(k): v for k, v in (tree.get("m_TOS") or [])}
    controller = _d(tree.get("m_Controller") or {})

    def name(h, fallback=""):
        text = names.get(int(h)) if h is not None else None
        return text if text is not None else fallback

    # parameters (with their defaults)
    values = _d(controller.get("m_Values") or {}).get("m_ValueArray") or []
    defaults = _d(controller.get("m_DefaultValues") or {})
    params, param_name = [], {}
    for v in values:
        v = _d(v)
        kind = PARAM_TYPES.get(v.get("m_Type"))
        if kind is None:
            continue
        pname = name(v["m_ID"], f"param_{v['m_ID']:08x}")
        param_name[v["m_ID"]] = pname
        index = v.get("m_Index", 0)
        store = {"Float": "m_FloatValues", "Int": "m_IntValues"}.get(kind, "m_BoolValues")
        arr = defaults.get(store) or []
        default = arr[index] if index < len(arr) else 0
        params.append({"name": pname, "type": kind, "default": float(default)})

    def transition(t, states):
        t = _d(t)
        dest = t.get("m_DestinationState", -1)
        conditions = []
        for c in t.get("m_ConditionConstantArray") or []:
            c = _d(c)
            param = param_name.get(c.get("m_EventID"))
            if param is not None:
                conditions.append({"mode": int(c.get("m_ConditionMode", 1)), "param": param,
                                   "threshold": float(c.get("m_EventThreshold", 0.0))})
        return {"dest": int(dest) if isinstance(dest, int) and 0 <= dest < len(states) else -1,
                "duration": float(t.get("m_TransitionDuration", 0.25)), "offset": float(t.get("m_TransitionOffset", 0)),
                "exit_time": float(t.get("m_ExitTime", 0.75)), "has_exit_time": bool(t.get("m_HasExitTime")),
                "fixed_duration": bool(t.get("m_HasFixedDuration", True)),
                "interruption": int(t.get("m_InterruptionSource", 0)),
                "ordered": bool(t.get("m_OrderedInterruption", True)),
                "to_self": bool(t.get("m_CanTransitionToSelf", True)), "conditions": conditions}

    def blend_tree(bt):
        """-> {"nodes": [{"type", "param", "param_y", "clip", "children", "thresholds", "positions"}], "root": 0}"""
        nodes = []
        for n in _d(bt).get("m_NodeArray") or []:
            n = _d(n)
            clip_id = n.get("m_ClipID", NONE)
            one_d = _d(n.get("m_Blend1dData") or {}).get("m_ChildThresholdArray") or []
            two_d = _d(n.get("m_Blend2dData") or {}).get("m_ChildPositionArray") or []
            nodes.append({"type": BLEND_TYPES.get(n.get("m_BlendType", 0), "Simple1D"),
                          "param": param_name.get(n.get("m_BlendEventID"), ""),
                          "param_y": param_name.get(n.get("m_BlendEventYID"), ""),
                          "clip": clip_uid(clip_id) if clip_id != NONE else None,
                          "children": [int(i) for i in n.get("m_ChildIndices") or []],
                          "thresholds": [float(x) for x in one_d],
                          "positions": [float(p[k]) for p in two_d for k in ("x", "y")]})
        return {"nodes": nodes, "root": 0}

    layers = []
    machines = controller.get("m_StateMachineArray") or []
    for layer in controller.get("m_LayerArray") or []:
        layer = _d(layer)
        sm_index = layer.get("m_StateMachineIndex", 0)
        if sm_index >= len(machines):
            continue
        sm = _d(machines[sm_index])
        raw_states = [_d(s) for s in sm.get("m_StateConstantArray") or []]
        states = []
        for s in raw_states:
            full = name(s.get("m_FullPathID"), "")
            short = name(s.get("m_NameID"), full.rsplit(".", 1)[-1] or "State")
            motion = None
            trees = s.get("m_BlendTreeConstantArray") or []
            if trees:
                tree_desc = blend_tree(trees[0])
                nodes = tree_desc["nodes"]
                if len(nodes) == 1 and not nodes[0]["children"]:
                    motion = {"clip": nodes[0]["clip"]}  # a plain clip, not a real blend tree
                elif nodes:
                    motion = {"tree": tree_desc}
            states.append({"name": short, "path": full, "tag": name(s.get("m_TagID"), ""),
                           "speed": float(s.get("m_Speed", 1.0)), "cycle_offset": float(s.get("m_CycleOffset", 0)),
                           "mirror": bool(s.get("m_Mirror")), "ik_on_feet": bool(s.get("m_IKOnFeet")),
                           "write_defaults": bool(s.get("m_WriteDefaultValues", True)),
                           "speed_param": param_name.get(s.get("m_SpeedParamID"), ""),
                           "motion": motion, "transitions": []})
        for s, raw in zip(states, raw_states):
            s["transitions"] = [transition(t, states) for t in raw.get("m_TransitionConstantArray") or []]
        blending = layer.get("(int&)m_LayerBlendingMode", layer.get("m_LayerBlendingMode", 0))
        layers.append({"name": name(layer.get("m_Binding"), f"Layer {len(layers)}"),
                       "weight": float(layer.get("m_DefaultWeight", 1.0)), "blending": int(blending or 0),
                       "ik": bool(layer.get("m_IKPass")), "default_state": int(sm.get("m_DefaultState", 0)),
                       "states": states,
                       "any_transitions": [transition(t, states) for t in sm.get("m_AnyStateTransitionConstantArray") or []]})
    return {"name": tree.get("m_Name", "Controller"), "parameters": params, "layers": layers}
