"""Addressables catalogs (StreamingAssets/aa/catalog.json): switch off the CRC check of modded bundles.

The catalog stores each AssetBundle's load options (AssetBundleRequestOptions) as JSON inside the base64
m_ExtraDataString, and m_Crc there makes the game refuse a bundle whose content changed. Mod Maker sets it to
0 (= no check) for the bundles it rewrites. The new value is padded with spaces to the old length, so no
offset in the catalog moves and the rest of the file stays byte-for-byte the same.
"""

import base64
import json
import os
import re
import struct

RUNTIME_PATH = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
JSON_OBJECT = 7  # ObjectType.JsonObject in Addressables' SerializationUtilities
CRC = re.compile(r'"m_Crc":\s*(\d+)')


def aa_folder(rel):
    """'X_Data/StreamingAssets/aa' for a file under it, else None ('/' separated, case kept)."""
    parts = rel.split("/")
    for i in range(len(parts) - 1):
        if parts[i].lower() == "streamingassets" and parts[i + 1].lower() == "aa":
            return "/".join(parts[:i + 2])
    return None


def catalog_files(aa_dir):
    """Catalog JSON files in an aa folder (names relative to it); [] if there are none."""
    try:
        names = os.listdir(aa_dir)
    except OSError:
        return []
    return sorted(n for n in names if n.lower().startswith("catalog") and n.lower().endswith(".json"))


def _internal_ids(doc):
    ids = doc.get("m_InternalIds") or []
    prefixes = doc.get("m_InternalIdPrefixes") or []
    if not prefixes:
        return ids
    out = []
    for i in ids:  # newer catalogs shorten ids to "<prefix index>#<rest>"
        m = re.match(r"^(\d+)#(.*)$", i)
        out.append(prefixes[int(m.group(1))] + m.group(2) if m and int(m.group(1)) < len(prefixes) else i)
    return out


def bundle_options(doc):
    """[(internal id, byte offset of the JSON text in the extra data, JSON text)] of the AssetBundle entries."""
    ids = _internal_ids(doc)
    extra = base64.b64decode(doc.get("m_ExtraDataString") or "")
    entries = base64.b64decode(doc.get("m_EntryDataString") or "")
    if len(entries) < 4:
        return []
    count = struct.unpack_from("<i", entries)[0]
    out, seen = [], set()
    for i in range(count):
        internal, _prov, _dep, _hash, data, _key, _type = struct.unpack_from("<7i", entries, 4 + i * 28)
        if data < 0 or data >= len(extra) or data in seen or extra[data] != JSON_OBJECT:
            continue
        seen.add(data)
        p = data + 1
        p += 1 + extra[p]                 # assembly name
        class_len = extra[p]
        class_name = extra[p + 1:p + 1 + class_len].decode("ascii", "replace")
        p += 1 + class_len
        size = struct.unpack_from("<i", extra, p)[0]
        if not class_name.endswith("AssetBundleRequestOptions") or not 0 <= internal < len(ids):
            continue
        out.append((ids[internal], p + 4, extra[p + 4:p + 4 + size].decode("utf-16-le")))
    return out


def _bundle_rel(internal_id, aa_rel):
    path = internal_id.replace("\\", "/")
    if not path.startswith(RUNTIME_PATH):
        return None  # a remote bundle (URL) or another location: not a game file we wrote
    return aa_rel + "/" + path[len(RUNTIME_PATH):].lstrip("/")


def patch_catalog(text, aa_rel, changed):
    """(new catalog text, [bundle rels whose CRC check was switched off]) for the bundles in `changed`
    (rel paths). The text comes back unchanged if nothing needed patching."""
    doc = json.loads(text.lstrip(chr(0xFEFF)))
    wanted = {r.lower() for r in changed}
    extra = bytearray(base64.b64decode(doc.get("m_ExtraDataString") or ""))
    patched = []
    for internal_id, start, options in bundle_options(doc):
        rel = _bundle_rel(internal_id, aa_rel)
        if rel is None or rel.lower() not in wanted:
            continue
        m = CRC.search(options)
        if m is None or int(m.group(1)) == 0:
            continue
        old = m.group(0)
        new = ('"m_Crc":0').ljust(len(old))  # JSON allows the spaces; the length (and every offset) stays
        options = options[:m.start()] + new + options[m.end():]
        encoded = options.encode("utf-16-le")
        extra[start:start + len(encoded)] = encoded
        patched.append(rel)
    if not patched:
        return text, []
    old_b64, new_b64 = doc["m_ExtraDataString"], base64.b64encode(bytes(extra)).decode("ascii")
    if text.count(old_b64) != 1:
        raise ValueError("Couldn't find the catalog's extra data to patch.")
    return text.replace(old_b64, new_b64), patched
