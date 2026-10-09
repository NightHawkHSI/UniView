"""Addressables content catalogs: which asset path each key (address, label, asset GUID) stands for.

Games load Addressables by key; data assets often keep an AssetReference - the asset's GUID - instead of a
direct reference (Procelio's material list: {"name": "RailgunPart", "thing": {"m_AssetGUID": ...}}), and only the
catalog says that GUID is "Assets/.../HDRPRailgun.mat".

Newer Addressables (1.21+, Unity 6 games) write catalog.bin, a BinaryStorageBuffer:
  header: magic 0x0DE38942, version, then offsets: keys, ...
  keys: (key object, location set) pairs, the array's byte size in the 4 bytes before it
  key object: (type, value); a string value is (string id, separator)
  string id: plain = offset of the text (its length in the 4 bytes before); with bit 30 set = a linked list
             of (part string id, next node or 0xFFFFFFFF), last part first, joined with the separator
  location set: offsets of locations; location: (primary key, internal id, provider, dependencies, ...)
Older catalog.json files keep it base64-packed (m_KeyDataString / m_BucketDataString / m_EntryDataString).
"""

import base64
import json
import re
import struct

BIN_MAGIC = 0x0DE38942
DYNAMIC = 0x40000000
NONE = 0xFFFFFFFF


class BinaryCatalog:
    def __init__(self, data):
        self.d = data
        magic, self.version = struct.unpack_from("<iI", data, 0)
        if magic & 0xFFFFFFFF != BIN_MAGIC:
            raise ValueError("Not an Addressables catalog.bin")
        self.keys_offset = struct.unpack_from("<I", data, 8)[0]

    def _u32(self, o):
        return struct.unpack_from("<I", self.d, o)[0]

    def text(self, sid, sep="/"):
        """A string id (plain or a linked list of parts) as text."""
        if sid == NONE or (not sid & DYNAMIC and sid >= len(self.d)):
            return ""
        if not sid & DYNAMIC:
            n = self._u32(sid - 4)
            return self.d[sid:sid + n].decode("utf-8", "replace") if 0 <= n < 4096 else ""
        parts, node, hops = [], sid & ~DYNAMIC, 0
        while node != NONE and node < len(self.d) and hops < 256:
            part, nxt = struct.unpack_from("<II", self.d, node)
            parts.append(self.text(part, sep))
            node, hops = nxt, hops + 1
        return sep.join(reversed(parts))

    def string_object(self, o):
        """A key's string value: (string id, separator char)."""
        sid, sep = struct.unpack_from("<II", self.d, o)
        return self.text(sid, chr(sep) if 0 < sep < 128 else "")

    def entries(self):
        """{key text: [internal ids]} for every string key."""
        out = {}
        size = self._u32(self.keys_offset - 4)
        for i in range(size // 8):
            key_obj, loc_set = struct.unpack_from("<II", self.d, self.keys_offset + 8 * i)
            try:
                _type, value = struct.unpack_from("<II", self.d, key_obj)
                key = self.string_object(value)
                n = self._u32(loc_set - 4) // 4
                ids = []
                for loc in struct.unpack_from(f"<{min(n, 256)}I", self.d, loc_set):
                    internal = self._u32(loc + 4)
                    ids.append(self.text(internal, "/"))
            except (struct.error, IndexError):
                continue
            if key:
                out.setdefault(key, []).extend(i for i in ids if i)
        return out


def json_entries(doc):
    """{key text: [internal ids]} of a catalog.json (Addressables 1.x) - string keys only."""
    ids = doc.get("m_InternalIds") or []
    keys_raw = base64.b64decode(doc.get("m_KeyDataString") or "")
    buckets = base64.b64decode(doc.get("m_BucketDataString") or "")
    entries = base64.b64decode(doc.get("m_EntryDataString") or "")
    entry_count = struct.unpack_from("<i", entries, 0)[0] if len(entries) >= 4 else 0
    internal_of = [struct.unpack_from("<i", entries, 4 + 28 * e)[0] for e in range(entry_count)]

    def key_at(pos):
        kind = keys_raw[pos]
        if kind in (0, 1):  # ASCII / Unicode string
            n = struct.unpack_from("<i", keys_raw, pos + 1)[0]
            raw = keys_raw[pos + 5:pos + 5 + n]
            return raw.decode("ascii" if kind == 0 else "utf-16-le", "replace")
        return None
    out = {}
    count = struct.unpack_from("<i", buckets, 0)[0] if len(buckets) >= 4 else 0
    pos = 4
    for _ in range(count):
        data_offset, n = struct.unpack_from("<ii", buckets, pos)
        pos += 8
        targets = struct.unpack_from(f"<{n}i", buckets, pos)
        pos += 4 * n
        try:
            key = key_at(data_offset)
        except (IndexError, struct.error):
            key = None
        if key:
            out.setdefault(key, []).extend(ids[internal_of[t]] for t in targets
                                           if 0 <= t < len(internal_of) and 0 <= internal_of[t] < len(ids))
    return out


def json_locations(doc):
    """[(internal id, primary key (the address), provider, [internal ids of its dependencies (bundles)])] of a
    catalog.json - one per location."""
    ids = doc.get("m_InternalIds") or []
    prefixes = doc.get("m_InternalIdPrefixes") or []
    if prefixes:  # newer catalogs shorten ids to "<prefix index>#<rest>"
        full = []
        for i in ids:
            head, sep, rest = i.partition("#")
            full.append(prefixes[int(head)] + rest if sep and head.isdigit() and int(head) < len(prefixes) else i)
        ids = full
    providers = doc.get("m_ProviderIds") or []
    keys_raw = base64.b64decode(doc.get("m_KeyDataString") or "")
    buckets = base64.b64decode(doc.get("m_BucketDataString") or "")
    entries = base64.b64decode(doc.get("m_EntryDataString") or "")
    entry_count = struct.unpack_from("<i", entries, 0)[0] if len(entries) >= 4 else 0
    rows = [struct.unpack_from("<7i", entries, 4 + 28 * e) for e in range(entry_count)]
    key_offsets, key_targets = [], []
    pos, count = 4, (struct.unpack_from("<i", buckets, 0)[0] if len(buckets) >= 4 else 0)
    for _ in range(count):
        data_offset, n = struct.unpack_from("<ii", buckets, pos)
        key_offsets.append(data_offset)
        key_targets.append(struct.unpack_from(f"<{n}i", buckets, pos + 8))
        pos += 8 + 4 * n

    def key_text(index):
        if not 0 <= index < len(key_offsets):
            return ""
        p = key_offsets[index]
        if p >= len(keys_raw) or keys_raw[p] not in (0, 1):
            return ""
        n = struct.unpack_from("<i", keys_raw, p + 1)[0]
        return keys_raw[p + 5:p + 5 + n].decode("ascii" if keys_raw[p] == 0 else "utf-16-le", "replace")

    def internal(entry):
        return ids[rows[entry][0]] if 0 <= entry < len(rows) and 0 <= rows[entry][0] < len(ids) else ""

    out = []
    for e, (internal_id, provider, dep_key, _hash, _data, primary, _type) in enumerate(rows):
        deps = [internal(t) for t in key_targets[dep_key]] if 0 <= dep_key < len(key_targets) else []
        out.append((internal(e), key_text(primary),
                    providers[provider] if 0 <= provider < len(providers) else "", [d for d in deps if d]))
    return out


def binary_locations(cat):
    """json_locations() for a BinaryCatalog: location = (primary key, internal id, provider, dependency set, ...)."""
    out, seen = [], set()
    size = cat._u32(cat.keys_offset - 4)

    def location(loc):
        primary, internal, provider, dep_set = struct.unpack_from("<4I", cat.d, loc)
        return cat.text(primary, "/"), cat.text(internal, "/"), cat.text(provider, "."), dep_set

    for i in range(size // 8):
        try:
            _key, loc_set = struct.unpack_from("<II", cat.d, cat.keys_offset + 8 * i)
            n = cat._u32(loc_set - 4) // 4
            for loc in struct.unpack_from(f"<{min(n, 256)}I", cat.d, loc_set):
                if loc in seen:
                    continue
                seen.add(loc)
                primary, internal, provider, dep_set = location(loc)
                deps = []
                if dep_set != NONE and 4 <= dep_set < len(cat.d):
                    m = cat._u32(dep_set - 4) // 4
                    deps = [location(d)[1] for d in struct.unpack_from(f"<{min(m, 256)}I", cat.d, dep_set)]
                out.append((internal, primary, provider, [d for d in deps if d]))
        except (struct.error, IndexError):
            continue
    return out


def _read(path):
    with open(path, "rb") as f:
        data = f.read()
    if path.lower().endswith(".bin"):
        return BinaryCatalog(data)
    return json.loads(data.decode("utf-8-sig"))


def catalog_entries(path):
    """{key: [internal ids]} of a catalog.bin / catalog.json file ({} if it can't be read)."""
    cat = _read(path)
    return cat.entries() if isinstance(cat, BinaryCatalog) else json_entries(cat)


def catalog_locations(path):
    """json_locations() of a catalog.bin / catalog.json file."""
    cat = _read(path)
    return binary_locations(cat) if isinstance(cat, BinaryCatalog) else json_locations(cat)


BUNDLE_GROUP = re.compile(r"^(.+?)_(?:assets|scenes)_.*\.bundle$", re.I)
GUID_KEY = re.compile(r"^[0-9a-f]{32}$")


def bundle_group(internal_id):
    """The Addressables group a bundle was built from ('initialmaps' for .../initialmaps_assets_all.bundle), or ''."""
    m = BUNDLE_GROUP.match(internal_id.replace("\\", "/").rsplit("/", 1)[-1])
    return m.group(1) if m else ""


def addressable_assets(locations, entries):
    """[{"path", "address", "guid", "labels", "group"}] for the project assets (Assets/...) loaded from bundles,
    one per path: what an Addressables group entry needs. locations: catalog_locations(); entries: catalog_entries()
    (every key - address, GUID, labels - pointing at each path)."""
    keys = {}
    for key, ids in entries.items():
        for i in ids:
            keys.setdefault(i, []).append(key)
    out, seen = [], set()
    for internal, address, provider, deps in locations:
        if (not provider.endswith("BundledAssetProvider") or internal in seen
                or not internal.replace("\\", "/").lower().startswith("assets/")):
            continue
        seen.add(internal)
        mine = list(dict.fromkeys(keys.get(internal, [])))
        guid = next((k for k in mine if GUID_KEY.match(k)), "")
        labels = [k for k in mine if k not in (address, guid, internal)]
        group = next((g for g in map(bundle_group, deps) if g and not g.endswith("unitybuiltinshaders")), "")
        out.append({"path": internal.replace("\\", "/"), "address": address or internal, "guid": guid,
                    "labels": labels, "group": group})
    return out
