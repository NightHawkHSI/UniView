"""Readers for common game file formats that aren't tied to one engine plugin.

Each takes the file's bytes and returns something the viewer can show (text, an image, a mesh).
"""

import struct

# --------------------------------------------------------------------------- Unreal .locres (localized text)

LOCRES_MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")


class _Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def u8(self):
        self.pos += 1
        return self.data[self.pos - 1]

    def i32(self):
        self.pos += 4
        return struct.unpack_from("<i", self.data, self.pos - 4)[0]

    def u32(self):
        self.pos += 4
        return struct.unpack_from("<I", self.data, self.pos - 4)[0]

    def i64(self):
        self.pos += 8
        return struct.unpack_from("<q", self.data, self.pos - 8)[0]

    def fstring(self):
        """Unreal FString: int32 length with the terminator; negative = UTF-16."""
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            raw = self.data[self.pos:self.pos - 2 * n]
            self.pos -= 2 * n
            return raw.decode("utf-16-le", "replace").rstrip("\0")
        if n > len(self.data) - self.pos:
            raise ValueError("bad string length")
        raw = self.data[self.pos:self.pos + n]
        self.pos += n
        return raw.decode("utf-8", "replace").rstrip("\0")


def read_locres(data):
    """Unreal localization file -> [(namespace, key, text)]."""
    r = _Reader(bytes(data))
    if data[:16] != LOCRES_MAGIC:  # legacy: no header, strings stored inline
        out = []
        for _ in range(r.u32()):
            ns = r.fstring()
            for _ in range(r.u32()):
                key = r.fstring()
                r.u32()  # source string hash
                out.append((ns, key, r.fstring()))
        return out
    r.pos = 16
    version = r.u8()
    strings_at = r.i64()
    here = r.pos
    r.pos = strings_at
    strings = []
    for _ in range(r.i32()):
        strings.append(r.fstring())
        if version >= 2:
            r.i32()  # reference count
    r.pos = here
    if version >= 2:
        r.u32()  # total entries
    out = []
    for _ in range(r.u32()):
        if version >= 2:
            r.u32()  # namespace hash
        ns = r.fstring()
        for _ in range(r.u32()):
            if version >= 2:
                r.u32()  # key hash
            key = r.fstring()
            r.u32()  # source string hash
            index = r.i32()
            out.append((ns, key, strings[index] if 0 <= index < len(strings) else ""))
    return out


def locres_text(data):
    entries = read_locres(data)
    lines = [f"{len(entries):,} localized string(s)", ""]
    current = None
    for ns, key, text in entries:
        if ns != current:
            current = ns
            lines += ["", f"[{ns or '(no namespace)'}]"]
        lines.append(f"{key} = {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- Source .phy (collision models)

IVP_TO_INCHES = 39.3701  # Havok/IVP works in meters with Y down; Source in inches with Z up


def read_phy(data):
    """Collision hulls of a Source model: (points (N, 3) in Source space, triangles (M, 3), key-values text).

    Each solid is a set of convex 'ledges' (IVP compact surface). Ragdoll solids are each in their
    bone's space, so they show on top of each other."""
    import numpy as np
    data = bytes(data)
    header_size, _id, solid_count = struct.unpack_from("<iii", data, 0)
    pos = header_size
    points, tris, base = [], [], 0
    for _ in range(solid_count):
        solid_size = struct.unpack_from("<i", data, pos)[0]
        start = pos + 4
        surface = start + 28 if data[start:start + 4] == b"VPHY" else start + 4  # after the solid's header
        root = struct.unpack_from("<i", data, surface + 32)[0]
        ledge = surface + 48
        end = surface + root if root > 0 else start + solid_size
        while ledge + 16 <= end:
            point_off, _client, size_flags, n_tris = struct.unpack_from("<iiIh", data, ledge)
            size = (size_flags >> 8) * 16
            if n_tris <= 0 or size <= 0:
                break
            first_point = ledge + point_off
            used = {}
            for t in range(n_tris):
                tri = ledge + 16 + t * 16
                ids = [struct.unpack_from("<I", data, tri + 4 + e * 4)[0] & 0xFFFF for e in range(3)]
                for i in ids:
                    if i not in used:
                        x, y, z = struct.unpack_from("<fff", data, first_point + i * 16)
                        used[i] = base + len(used)
                        points.append((x * IVP_TO_INCHES, z * IVP_TO_INCHES, -y * IVP_TO_INCHES))
                tris.append([used[i] for i in ids])
            base += len(used)
            ledge += size
        pos = start + solid_size
    text = data[pos:].split(b"\0", 1)[0].decode("latin-1", "replace")
    return np.array(points, np.float32).reshape(-1, 3), np.array(tris, np.int64).reshape(-1, 3), text


# --------------------------------------------------------------------------- Source .nav (bot navigation mesh)

def read_nav(data):
    """Navigation mesh: (area corners (A, 4, 3) nw/ne/se/sw in Source space, place names, info rows).

    Games add their own data after each area (TF2, CS...); it's skipped by finding where the next
    area record starts."""
    import numpy as np
    r = _Reader(bytes(data))
    if r.u32() != 0xFEEDFACE:
        raise ValueError("Not a navigation mesh (.nav) file")
    version = r.u32()
    if version < 6 or version > 16:
        raise ValueError(f"Navigation mesh version {version} isn't supported")
    subversion = r.u32() if version >= 10 else 0
    r.u32()  # size of the BSP it was built for
    if version >= 14:
        r.u8()  # analyzed
    places = []
    count = struct.unpack_from("<H", r.data, r.pos)[0]
    r.pos += 2
    for _ in range(count):
        n = struct.unpack_from("<H", r.data, r.pos)[0]
        r.pos += 2
        places.append(r.data[r.pos:r.pos + n].split(b"\0", 1)[0].decode("latin-1"))
        r.pos += n
    if version > 11:
        r.u8()  # has unnamed areas
    area_count = r.u32()
    flags_size = 1 if version <= 8 else 2 if version <= 12 else 4
    corners = []
    for a in range(area_count):
        area_id = r.u32()
        r.pos += flags_size
        nw = struct.unpack_from("<3f", r.data, r.pos)
        se = struct.unpack_from("<3f", r.data, r.pos + 12)
        ne_z, sw_z = struct.unpack_from("<2f", r.data, r.pos + 24)
        r.pos += 32
        corners.append([(nw[0], nw[1], nw[2]), (se[0], nw[1], ne_z), (se[0], se[1], se[2]), (nw[0], se[1], sw_z)])
        for _ in range(4):  # connections per direction
            r.pos += 4 * r.u32()
        r.pos += r.u8() * 17  # hiding spots
        if version < 15:
            r.pos += r.u8() * 14  # approach spots
        for _ in range(r.u32()):  # encounter paths
            r.pos += 10
            r.pos += r.u8() * 5
        r.pos += 2  # place
        for _ in range(2):  # ladders up/down
            r.pos += 4 * r.u32()
        r.pos += 8  # earliest occupy times
        if version >= 11:
            r.pos += 16  # light intensity per corner
        if version >= 16:
            r.pos += 5 * r.u32()  # potentially visible areas
            r.pos += 4  # inherit visibility from
        if a + 1 < area_count:
            r.pos = _next_area(r.data, r.pos, area_id, flags_size)
            if r.pos < 0:
                break
    rows = [("Nav version", f"{version}.{subversion}"), ("Areas", f"{len(corners):,} of {area_count:,}"),
            ("Places", ", ".join(places[:30]) + (" ..." if len(places) > 30 else "") if places else "none")]
    return np.array(corners, np.float32).reshape(-1, 4, 3), places, rows


def _next_area(data, pos, last_id, flags_size):
    """Offset of the next area record (after any game-specific bytes), found by a sanity check."""
    for extra in range(0, 256):
        p = pos + extra
        if p + 40 > len(data):
            return -1
        next_id = struct.unpack_from("<I", data, p)[0]
        if not 0 < next_id <= last_id + 100000 or next_id == last_id:
            continue
        c = struct.unpack_from("<8f", data, p + 4 + flags_size)
        nw, se = c[0:3], c[3:6]
        if all(abs(v) < 65536 for v in c) and nw[0] <= se[0] < nw[0] + 20000 and nw[1] <= se[1] < nw[1] + 20000:
            return p
    return -1


# --------------------------------------------------------------------------- Valve DMX binary (.pcf particles, .dmx)

_DMX_SCALARS = {2: ("<i", 4), 3: ("<f", 4), 4: ("<?", 1), 8: ("<4B", 4), 9: ("<2f", 8), 10: ("<3f", 12),
                11: ("<4f", 16), 12: ("<3f", 12), 13: ("<4f", 16), 14: ("<16f", 64)}


def read_dmx(data):
    """Binary DMX (particle systems, models...) -> (header, [{"_type", "_name", attribute: value}])."""
    import re
    data = bytes(data)
    end = data.find(b"\0")
    header = data[:end].decode("latin-1")
    m = re.search(r"encoding binary (\d+)", header)
    if not m:
        raise ValueError("This DMX file is text (keyvalues2), not binary." if "keyvalues2" in header
                         else "Not a binary DMX file")
    version = int(m.group(1))
    r = _Reader(data, end + 1)

    def cstr():
        e = data.find(b"\0", r.pos)
        s = data[r.pos:e].decode("utf-8", "replace")
        r.pos = e + 1
        return s

    table = []
    if version >= 2:
        if version >= 4:
            count = r.i32()
        else:
            count = struct.unpack_from("<h", data, r.pos)[0]
            r.pos += 2
        table = [cstr() for _ in range(count)]

    def ref():
        if version >= 5:
            return table[r.i32()]
        if version >= 2:
            i = struct.unpack_from("<h", data, r.pos)[0]
            r.pos += 2
            return table[i]
        return cstr()

    elements = []
    for _ in range(r.i32()):
        etype = ref()
        name = ref() if version >= 4 else cstr()
        r.pos += 16  # GUID
        elements.append({"_type": etype, "_name": name})

    def value(t):
        if t == 1:  # element link
            i = r.i32()
            return "guid:" + cstr() if i == -2 else i
        if t == 5:
            return ref() if version >= 4 else cstr()
        if t == 6:
            ln = r.i32()
            r.pos += ln
            return f"<{ln} bytes>"
        if t == 7:  # time
            if version >= 3:
                return r.i32() / 10000.0
            v = struct.unpack_from("<f", data, r.pos)[0]
            r.pos += 4
            return v
        fmt, size = _DMX_SCALARS[t]
        v = struct.unpack_from(fmt, data, r.pos)
        r.pos += size
        return v[0] if len(v) == 1 else [round(x, 6) if isinstance(x, float) else x for x in v]

    for el in elements:
        for _ in range(r.i32()):
            name = ref()
            t = r.u8()
            el[name] = [value(t - 14) for _ in range(r.i32())] if t >= 15 else value(t)
    return header, elements


def dmx_text(data):
    """Readable DMX: every element with its attributes; links to other elements show their name."""
    header, elements = read_dmx(data)

    def link(i):
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(elements):
            return f"-> [{i}] {elements[i]['_name']}"
        return "(none)" if i == -1 else i

    # Particle systems: element links are indices; say what they point to.
    for i, el in enumerate(elements):
        for k, v in el.items():
            if k in ("children", "operators", "renderers", "initializers", "emitters", "forces", "constraints",
                     "particleSystemDefinitions", "child"):
                el[k] = [link(x) for x in v] if isinstance(v, list) else link(v)
    lines = [header.strip("<!-> "), f"{len(elements):,} element(s)", ""]
    for i, el in enumerate(elements):
        lines.append(f"[{i}] {el['_type']} \"{el['_name']}\"")
        for k, v in el.items():
            if not k.startswith("_"):
                lines.append(f"    {k} = {v}")
        lines.append("")
    return "\n".join(lines)
