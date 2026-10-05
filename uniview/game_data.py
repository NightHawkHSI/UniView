"""Game data that names a prefab: many data-driven games define their items/blocks/units in JSON text
assets ({"50": {"Name": "Logic_AND", "Data": {"Path": "Logic_AND", "LogicOperation": "And", ...}}}).
The prefab itself then carries only visuals, and its gameplay values live in those tables.

index(session) reads every JSON text asset once (cached per session); lookup() finds the table entries
whose values name a prefab, and rows() turns an entry into (field, value) rows with localization keys
("strLogic_AND") resolved to their text."""

import json
import time

from uniview.constants import log

MAX_TEXT = 30_000_000   # skip text assets bigger than this (bytes/characters)
MAX_MATCHES = 20
MAX_ROWS = 400
MIN_STRINGS_TABLE = 50  # a flat {key: text} dict at least this big counts as a localization table

_indexes = {}  # session -> GameDataIndex


class GameDataIndex:
    def __init__(self):
        self.by_value = {}  # lower-case string value -> [(table name, entry path, entry)]
        self.strings = {}   # localization key -> text
        self.tables = 0

    # ---- building
    def add_table(self, name, data):
        self.tables += 1
        if _is_strings_table(data):
            self.strings.update(data)
            return
        self._walk(name, data, (), None)

    def _walk(self, name, node, path, entry):
        if isinstance(node, dict):
            children = list(node.items())
        elif isinstance(node, list):
            children = list(enumerate(node))
        else:
            return
        # The outermost "collection" (a container of 3+ dicts/lists) makes each child its own entry.
        collection = entry is None and sum(isinstance(v, (dict, list)) for _k, v in children) >= 3
        for key, value in children:
            child_path = path + (str(key),)
            child_entry = entry
            if collection and isinstance(value, (dict, list)):
                child_entry = (name, child_path, value)
            if isinstance(value, str):
                if 2 <= len(value) <= 120:
                    owner = child_entry or (name, path, node)
                    self.by_value.setdefault(value.lower(), []).append(owner)
            elif isinstance(value, (dict, list)):
                self._walk(name, value, child_path, child_entry)

    # ---- queries
    def lookup(self, names):
        """Entries with a string value equal (ignoring case) to one of `names`: [(table, path, entry)]."""
        out, seen = [], set()
        for name in names:
            for table, path, entry in self.by_value.get((name or "").lower(), []):
                if id(entry) not in seen:
                    seen.add(id(entry))
                    out.append((table, path, entry))
                    if len(out) >= MAX_MATCHES:
                        return out
        return out

    def rows(self, entry):
        """[(field, value text)] of an entry, nested fields as dotted paths, localization keys resolved."""
        rows = []

        def add(prefix, value):
            if len(rows) >= MAX_ROWS:
                return
            if isinstance(value, dict):
                for k, v in value.items():
                    add(f"{prefix}.{k}" if prefix else str(k), v)
            elif isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
                for i, v in enumerate(value):
                    add(f"{prefix}[{i}]", v)
            else:
                rows.append((prefix or "value", self.value_text(value)))

        add("", entry)
        return rows

    def value_text(self, value):
        if isinstance(value, str):
            text = self.strings.get(value)
            return f'"{value}"  → "{text}"' if isinstance(text, str) else f'"{value}"'
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False)
        return "null" if value is None else str(value)


def _is_strings_table(data):
    if not isinstance(data, dict) or len(data) < MIN_STRINGS_TABLE:
        return False
    texts = sum(isinstance(v, str) for v in data.values())
    return texts >= 0.9 * len(data)


def _json_of(text):
    text = text.lstrip("﻿ \t\r\n")
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def index(session):
    """The GameDataIndex of a session (built on first use)."""
    cached = _indexes.get(session)
    if cached is not None:
        return cached
    started = time.perf_counter()
    result = GameDataIndex()
    if hasattr(session, "text"):
        for asset in session.assets:
            if asset.kind != "text" or (asset.size or 0) > MAX_TEXT:
                continue
            try:
                text = session.text(asset)
            except Exception:
                continue
            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8", "replace")
            data = _json_of(text) if isinstance(text, str) and len(text) <= MAX_TEXT else None
            if data is not None:
                result.add_table(asset.name.rsplit("/", 1)[-1], data)
    _indexes.clear()  # one game at a time
    _indexes[session] = result
    log.info("Indexed %d game data table(s) (%d localized strings) in %.1fs", result.tables, len(result.strings),
             time.perf_counter() - started)
    return result


def prefab_names(asset_name, nodes):
    """Names a prefab can be referred to by in data: its file name and its root object's name."""
    name = asset_name.split(":", 1)[-1].strip()
    names = [name.rsplit("/", 1)[-1]]
    if nodes:
        names.append(nodes[0].get("name") or "")
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def entries_for(session, asset_name, nodes):
    """[{"title", "entry", "rows"}] for the game data entries that name this prefab."""
    idx = index(session)
    out = []
    for table, path, entry in idx.lookup(prefab_names(asset_name, nodes)):
        title = f"{table} › {'.'.join(path)}" if path else table
        out.append({"title": title, "entry": entry, "rows": idx.rows(entry)})
    return out
