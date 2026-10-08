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


def catalog_entries(path):
    """{key: [internal ids]} of a catalog.bin / catalog.json file ({} if it can't be read)."""
    with open(path, "rb") as f:
        data = f.read()
    if path.lower().endswith(".bin"):
        return BinaryCatalog(data).entries()
    return json_entries(json.loads(data.decode("utf-8-sig")))
