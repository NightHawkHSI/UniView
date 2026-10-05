"""Addressables catalog patch: the CRC check of modded bundles is switched off without moving anything."""

import base64
import json
import struct

from uniview import addressables

RT = addressables.RUNTIME_PATH


def _object(options):
    asm, cls = b"Unity.ResourceManager", b"UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"
    js = json.dumps(options, separators=(",", ":")).encode("utf-16-le")
    return bytes([7, len(asm)]) + asm + bytes([len(cls)]) + cls + struct.pack("<i", len(js)) + js


def _catalog(ids, crcs, prefixes=None):
    extra, entries = b"", []
    for i, crc in enumerate(crcs):
        entries.append((i, 0, -1, 0, len(extra), i, 0))
        extra += _object({"m_Hash": "abc", "m_Crc": crc, "m_BundleName": f"b{i}", "m_BundleSize": 10})
    entry_data = struct.pack("<i", len(entries)) + b"".join(struct.pack("<7i", *e) for e in entries)
    doc = {"m_LocatorId": "AddressablesMainContentCatalog", "m_InternalIds": ids,
           "m_EntryDataString": base64.b64encode(entry_data).decode(),
           "m_ExtraDataString": base64.b64encode(extra).decode()}
    if prefixes:
        doc["m_InternalIdPrefixes"] = prefixes
    return json.dumps(doc)


def _crcs(text):
    return [json.loads(o)["m_Crc"] for _i, _s, o in addressables.bundle_options(json.loads(text))]


def test_only_the_modded_bundles_lose_their_crc():
    aa = "Game_Data/StreamingAssets/aa"
    text = _catalog([RT + "\\StandaloneWindows64\\a.bundle", RT + "/StandaloneWindows64/b.bundle",
                     "https://cdn.example.com/c.bundle"], [111, 4294967295, 333])
    new, patched = addressables.patch_catalog(text, aa, [aa + "/StandaloneWindows64/b.bundle",
                                                        aa + "/standalonewindows64/A.BUNDLE"])
    assert sorted(patched) == [aa + "/StandaloneWindows64/a.bundle", aa + "/StandaloneWindows64/b.bundle"]
    assert _crcs(new) == [0, 0, 333]
    assert len(new) == len(text)  # padded: same size, no offsets moved


def test_nothing_to_do_returns_the_same_text():
    aa = "D/StreamingAssets/aa"
    text = _catalog([RT + "/x.bundle"], [0])
    assert addressables.patch_catalog(text, aa, [aa + "/x.bundle"]) == (text, [])
    assert addressables.patch_catalog(text, aa, [aa + "/other.bundle"]) == (text, [])


def test_prefixed_internal_ids():
    aa = "D/StreamingAssets/aa"
    text = _catalog(["0#/w/x.bundle"], [5], prefixes=[RT])
    new, patched = addressables.patch_catalog(text, aa, [aa + "/w/x.bundle"])
    assert patched == [aa + "/w/x.bundle"] and _crcs(new) == [0]


def test_aa_folder():
    assert addressables.aa_folder("G_Data/StreamingAssets/aa/Win/x.bundle") == "G_Data/StreamingAssets/aa"
    assert addressables.aa_folder("G_Data/sharedassets0.assets") is None
