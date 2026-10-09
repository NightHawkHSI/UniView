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


def _json_catalog():
    """Two bundled assets in one bundle: a prefab with address 'Ship' + label 'Map', a texture by path."""
    ids = ["{UnityEngine.AddressableAssets.Addressables.RuntimePath}/StandaloneWindows64/maps_assets_all_0123abcd.bundle",
           "Assets/Ships/Ship.prefab", "Assets/Art/hull.png"]
    keys = ["maps_assets_all_0123abcd.bundle", "Ship", "b833ea2a0d99b764491931ae2ee38a9d", "Map", "Assets/Art/hull.png",
            "dep-hash"]
    targets = [[0], [1], [1], [1], [2], [0]]
    key_data, offsets = b"", []
    for k in keys:
        offsets.append(len(key_data))
        key_data += bytes([0]) + struct.pack("<i", len(k)) + k.encode()
    buckets = struct.pack("<i", len(keys))
    for off, t in zip(offsets, targets):
        buckets += struct.pack("<ii", off, len(t)) + struct.pack(f"<{len(t)}i", *t)
    # entry: internal id, provider, dependency key, dep hash, data, primary key, resource type
    rows = [(0, 0, -1, 0, -1, 0, 0), (1, 1, 5, 0, -1, 1, 0), (2, 1, 5, 0, -1, 4, 0)]
    entries = struct.pack("<i", len(rows)) + b"".join(struct.pack("<7i", *r) for r in rows)
    return {"m_InternalIds": ids, "m_KeyDataString": base64.b64encode(key_data).decode(),
            "m_ProviderIds": ["UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider",
                              "UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider"],
            "m_BucketDataString": base64.b64encode(buckets).decode(),
            "m_EntryDataString": base64.b64encode(entries).decode()}


def test_json_locations():
    locs = uc.json_locations(_json_catalog())
    assert locs[1] == ("Assets/Ships/Ship.prefab", "Ship",
                       "UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider",
                       ["{UnityEngine.AddressableAssets.Addressables.RuntimePath}/StandaloneWindows64/"
                        "maps_assets_all_0123abcd.bundle"])
    assert locs[0][2].endswith("AssetBundleProvider") and locs[0][3] == []


def test_addressable_assets():
    doc = _json_catalog()
    out = uc.addressable_assets(uc.json_locations(doc), uc.json_entries(doc))
    assert out == [{"path": "Assets/Ships/Ship.prefab", "address": "Ship", "guid": "b833ea2a0d99b764491931ae2ee38a9d",
                    "labels": ["Map"], "group": "maps"},
                   {"path": "Assets/Art/hull.png", "address": "Assets/Art/hull.png", "guid": "", "labels": [],
                    "group": "maps"}]


def test_bundle_group():
    assert uc.bundle_group("{RuntimePath}\\StandaloneWindows64\\localization-locales_assets_all.bundle") == \
        "localization-locales"
    assert uc.bundle_group("x/referencedatagroup_assets_all_d3834a283a085d44de131ae8f9c2ffc8.bundle") == \
        "referencedatagroup"
    assert uc.bundle_group("x/level1_scenes_all.bundle") == "level1"
    assert uc.bundle_group("x/weird.bundle") == ""
