"""plugins/wakfu.py: .pk sound packs, .tgam images, the .anm reader/renderer, bdata, and (if installed) the real game."""
import importlib.util
import io
import os
import struct
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wakfu", os.path.join(HERE, "..", "plugins", "wakfu.py"))
wk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wk)

GAME = r"D:\SteamLibrary\steamapps\common\Wakfu"


def _s(text):
    return text.encode() + b"\0"


def _anm():
    """A tiny .anm: one 4x2 shape, one named 2-frame sprite (frame 0: moved by (10, 5); frame 1: also scaled x2)."""
    d = struct.pack("<BhB", 0x3F, 0, 25)
    d += b"\x00" + struct.pack("<H", 1) + _s("1_AnimTest") + struct.pack("<ih", 0, -1)  # index: one animation
    d += struct.pack("<h", 1) + _s("tex_0") + struct.pack("<i", 0)  # atlas
    d += struct.pack("<H", 1) + struct.pack("<hhHHHHhhff", 100, 0, 0, 0, 65535, 65535, 4, 2, 0.0, 0.0)  # shape 100
    d += struct.pack("<i4f", 4, 1, 1, 1, 1)  # colors
    d += struct.pack("<i4f", 4, 2, 0, 0, 2)  # matrices
    d += struct.pack("<i2f", 2, 10, 5)  # translations
    d += struct.pack("<i", 0)  # actions
    d += struct.pack("<H", 1) + struct.pack("<BhB", 4, 1, 64) + _s("1_AnimTest") + struct.pack("<ii", 0, 0)
    d += struct.pack("<H4i", 4, 0, 0, 2, 0)  # frames: (data offset, child list index) x 2
    d += struct.pack("<H2h", 2, 1, 100)  # child list: 1 child, shape 100
    d += struct.pack("<H", 0)  # no actions
    d += struct.pack("<Bi", 1, 5) + bytes([2, 0, 3, 0, 0])  # frame 0: translate; frame 1: matrix + translate
    d += struct.pack("<H", 0)  # imports
    return d


def test_anm_index_and_parse():
    data = _anm()
    assert wk.anm_index(data) == ([], [("1_AnimTest", -1)])
    a = wk.Anm(data)
    assert a.fps == 25 and a.texture == "tex_0" and a.named == {"1_AnimTest": 1}
    assert a.frame_count(1) == 2
    parts = []
    a.collect(1, 0, wk.IDENTITY, parts)
    assert len(parts) == 1 and parts[0][2][:6] == (1.0, 0.0, 0.0, 1.0, 10.0, 5.0)
    parts = []
    a.collect(1, 1, wk.IDENTITY, parts)
    assert parts[0][2][:6] == (2.0, 0.0, 0.0, 2.0, 10.0, 5.0)


def test_anm_draws_shapes_where_the_transform_puts_them():
    from PIL import Image
    a = wk.Anm(_anm())
    parts = []
    a.collect(1, 1, wk.IDENTITY, parts)
    quads = wk._quads(parts, 1.0)
    assert quads[0][3] == (8.0, 0.0) and quads[0][4] == (0.0, 4.0)
    img = wk.draw(quads, {id(a): Image.new("RGBA", (4, 2), (255, 0, 0, 255))}, (0, 0, 20, 12))
    assert img.getpixel((13, 7)) == (255, 0, 0, 255)  # inside 10..18 x 5..9
    assert img.getpixel((9, 7))[3] == 0 and img.getpixel((13, 10))[3] == 0
    # Pieces are stored upside down in the atlas: its bottom row lands at the top of the quad.
    atlas = Image.new("RGBA", (4, 2), (255, 0, 0, 255))
    atlas.paste((0, 0, 255, 255), (0, 1, 4, 2))
    img = wk.draw(quads, {id(a): atlas}, (0, 0, 20, 12))
    assert img.getpixel((13, 5))[:3] == (0, 0, 255) and img.getpixel((13, 8))[:3] == (255, 0, 0)


def test_animation_code_lists_each_frames_opcodes():
    lines = wk.Anm(_anm()).code(1)
    assert lines[0] == 'sprite 1 "1_AnimTest": kind 4, 2 frames, holds its last frame'
    assert lines[1:5] == ["  frame 0:", "    shape 100 (4x2)  move(10, 5)",
                          "  frame 1:", "    shape 100 (4x2)  matrix(2, 0, 0, 2)  move(10, 5)"]


def test_imports_resolve_against_the_host():
    a = wk.Anm(_anm())
    host = wk.Anm(_anm())
    a.imports[100] = "AnimTest"  # the shape id now names a part of the host (matched without its "1_")
    parts = []
    a.collect(1, 0, wk.IDENTITY, parts, host)
    assert parts and parts[0][0] is host


def test_directions():
    assert wk.split_direction("5_AnimStatique") == ("5", "AnimStatique")
    assert wk.split_direction("Idle") == ("", "Idle")
    assert wk.pick_direction(["5_AnimMarche", "1_AnimMarche"]) == "1_AnimMarche"  # facing the camera first


def test_pk_pack():
    ogg = b"OggS\x00\x02rest"
    shifted = bytes((b + 1) & 255 for b in ogg)
    pack = struct.pack("<II", 12, 1) + struct.pack("<II", 1234, 0) + struct.pack("<Q", len(shifted)) + shifted
    entries = wk.pk_entries(io.BytesIO(pack))
    assert entries == [(1234, 24, len(ogg))]
    assert wk.pk_ogg(pack[24:24 + len(ogg)]) == ogg


def test_tgam():
    pixels = bytes([1, 2, 3, 255]) * (4 * 2)
    data = b"mAGT" + struct.pack("<HHII", 3, 2, len(pixels), 0) + b"\0" * 5 + pixels
    img = wk.tgam_image(data)
    assert img.size == (3, 2) and img.getpixel((0, 0)) == (1, 2, 3, 255)
    assert wk.tgam_size(data) == (3, 2)


class _BinWriter:
    """Encodes like the client's bdata writer: each number plus the rolling key (the inverse of wk._BinReader)."""

    def __init__(self, file_id, seed, key, start=0):
        self.file_id, self.seed, self.key, self.out, self.start = file_id, seed, key, bytearray(), start

    def num(self, code, v):
        self.key = ((self.key + self.file_id * (self.start + len(self.out)) + self.seed + 128) & 255) - 128
        if code in "fd":
            self.out += struct.pack("<" + code, v)
        else:
            bits = {"i": 32, "q": 64, "b": 8, "z": 8, "h": 16}[code]
            self.out += ((v + self.key) & ((1 << bits) - 1)).to_bytes(bits // 8, "little")

    def string(self, text):
        self.num("i", len(text.encode()))
        self.out += text.encode()


def _bdata(file_id, records):
    """A .bin holding `records` (lists of (code, value); code "s" for strings) with ids 1, 2, ... and no indexes."""
    seed = 1234
    bodies, start = [], 0
    for i, fields in enumerate(records):
        w = _BinWriter(file_id, seed, 7 * i, start)  # key steps count from the first record
        for code, v in fields:
            w.string(v) if code == "s" else w.num(code, v)
        bodies.append(bytes(w.out))
        start += len(w.out)
    head = _BinWriter(file_id, seed, (((file_id ^ seed) + 128) & 255) - 128)
    head.out += bytes(4)  # the seed, stored as is
    head.num("i", len(records))
    off = 0
    for i, body in enumerate(bodies):
        for code, v in (("q", i + 1), ("i", off), ("i", len(body)), ("b", 7 * i)):
            head.num(code, v)
        off += len(body)
    head.num("b", 0)
    return struct.pack("<i", seed - wk.BDATA_SEED) + bytes(head.out[4:]) + b"".join(bodies)


def _monster(mid, gfx):
    """A monster record (MONSTER_SPEC) with every array empty."""
    fields, depth = [], 0
    for c in wk.MONSTER_SPEC:
        if c == "(":
            if depth == 0:
                fields.append(("i", 0))  # sub-record count
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            fields.append(("s", "") if c == "s" else ("i", 0) if c in "AHS" else (c, 0))
    fields[0], fields[-4] = ("i", mid), ("i", gfx)
    return fields


def test_bdata_monster_names():
    data = _bdata(wk.MONSTER_TABLE, [_monster(5577, 100505577), _monster(3745, 100500198), _monster(9, 100500198)])
    gfx = wk.monster_gfx(data)
    assert gfx == {100505577: [5577], 100500198: [3745, 9]}
    text = "content.7.5577=Sadida Tree\ncontent.7.3745=The Voodoll\ncontent.7.9=Caf\\u00e9 / Doll\ncontent.8.1=x\n"
    names = wk.properties(text, "content.7.")
    assert names["content.7.9"] == "Café / Doll" and "content.8.1" not in names
    assert wk.gfx_labels(gfx, names) == {"100505577": "Sadida Tree", "100500198": "The Voodoll, Café _ Doll"}


@pytest.mark.skipif(not os.path.isdir(os.path.join(GAME, "contents", "animations")), reason="Wakfu not installed")
def test_real_game_files():
    plugin = wk.WakfuPlugin()
    assert plugin.detect(GAME) >= 90
    contents = os.path.join(GAME, "contents")
    with zipfile.ZipFile(os.path.join(contents, "animations", "npcs", "npcs.jar")) as z:
        name = next(n for n in z.namelist() if n.endswith(".anm"))
        a = wk.Anm(z.read(name))
        assert a.named and a.shapes
    with open(os.path.join(contents, "sounds", "SFX", "SFX.pk"), "rb") as f:
        _sid, off, size = wk.pk_entries(f)[0]
        f.seek(off)
        assert wk.pk_ogg(f.read(size))[:4] == b"OggS"
    with zipfile.ZipFile(os.path.join(contents, "maps", "gfx.jar")) as z:
        name = next(n for n in z.namelist() if n.endswith(".tgam") and z.getinfo(n).file_size > 100)
        assert wk.tgam_image(z.read(name)).width > 0
    labels = wk.npc_labels(contents)
    assert labels.get("100505577") == "Sadida Tree" and labels.get("100500198") == "The Voodoll"
