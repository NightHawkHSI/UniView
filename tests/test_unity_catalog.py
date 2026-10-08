"""Addressables catalogs: GUID / address -> asset path (catalog.bin and catalog.json)."""

import base64
import json
import struct

from engines import unity_catalog as uc


class Buffer:
    """Builds a tiny catalog.bin the way BinaryStorageBuffer lays it out: objects with their byte size before them."""

    def __init__(self):
        self.data = bytearray(struct.pack("<iI", uc.BIN_MAGIC, 2) + bytes(24))  # header, offsets filled in later

    def add(self, payload):
        self.data += struct.pack("<I", len(payload))
        at = len(self.data)
        self.data += payload
        return at

    def text(self, s):
        return self.add(s.encode())

    def path(self, *parts):
        """A '/'-joined string as a linked list of parts, last part first."""
        node = uc.NONE
        for part in parts:  # first part ends up last in the chain
            node = self.add(struct.pack("<II", self.text(part), node))
        return node | uc.DYNAMIC


def test_binary_catalog():
    b = Buffer()
    guid_text = b.text("7e52ec229e3fe2c4b9c3ac57353f8383")
    guid_value = b.add(struct.pack("<II", guid_text, 0))
    key = b.add(struct.pack("<II", 0, guid_value))
    location = b.add(struct.pack("<8I", 0, b.path("Assets", "Mats", "Rail.mat"), 0, uc.NONE, 0, uc.NONE, 0, 0))
    loc_set = b.add(struct.pack("<I", location))
    keys = b.add(struct.pack("<II", key, loc_set))
    struct.pack_into("<I", b.data, 8, keys)
    entries = uc.BinaryCatalog(bytes(b.data)).entries()
    assert entries == {"7e52ec229e3fe2c4b9c3ac57353f8383": ["Assets/Mats/Rail.mat"]}


def test_json_catalog():
    ids = ["Assets/A.mat", "Assets/B.prefab"]
    key_data = b""
    offsets = []
    for k in ("guid-a", "guid-b"):
        offsets.append(len(key_data))
        key_data += bytes([0]) + struct.pack("<i", len(k)) + k.encode()
    buckets = struct.pack("<i", 2) + struct.pack("<ii", offsets[0], 1) + struct.pack("<i", 0) + \
        struct.pack("<ii", offsets[1], 1) + struct.pack("<i", 1)
    entries = struct.pack("<i", 2) + struct.pack("<7i", 0, 0, -1, 0, -1, 0, 0) + struct.pack("<7i", 1, 0, -1, 0, -1, 0, 0)
    doc = {"m_InternalIds": ids, "m_KeyDataString": base64.b64encode(key_data).decode(),
           "m_BucketDataString": base64.b64encode(buckets).decode(),
           "m_EntryDataString": base64.b64encode(entries).decode()}
    assert uc.json_entries(json.loads(json.dumps(doc))) == {"guid-a": ["Assets/A.mat"], "guid-b": ["Assets/B.prefab"]}
