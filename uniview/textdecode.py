"""Make sense of a "text" file's bytes: real text in whatever encoding it uses, a known file format hiding inside
(images, archives, compressed data...), or a hex dump of game-specific binary data."""

import gzip
import io
import re
import zlib

HEX_BYTES = 4096        # how much of a binary file the hex dump shows
MAX_STRINGS = 300       # readable strings listed for binary data
_STRINGS = re.compile(rb"[\x20-\x7e]{6,}")

# (magic bytes at offset 0, description); images are opened, compressed data is unpacked.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"), (b"\xff\xd8\xff", "JPEG image"), (b"GIF8", "GIF image"),
    (b"BM", "BMP image"), (b"RIFF", "RIFF file (WAV audio / WebP image / AVI video)"),
    (b"OggS", "Ogg audio"), (b"ID3", "MP3 audio"), (b"fLaC", "FLAC audio"),
    (b"\x1f\x8b", "gzip-compressed data"), (b"PK\x03\x04", "ZIP archive"), (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"\x28\xb5\x2f\xfd", "Zstandard-compressed data"), (b"SQLite format 3\x00", "SQLite database"),
    (b"UnityFS", "Unity AssetBundle"), (b"UnityWeb", "Unity web bundle"), (b"UnityRaw", "Unity raw bundle"),
    (b"\x1bLua", "compiled Lua script"), (b"\x1bLJ", "compiled LuaJIT script"), (b"MZ", "Windows program / .NET assembly"),
    (b"\x7fELF", "Linux program"), (b"%PDF", "PDF document"), (b"FSB5", "FMOD sound bank"),
    (b"BKHD", "Wwise sound bank"), (b"AKPK", "Wwise package"), (b"glTF", "glTF binary model"),
)


def _is_binary(data):
    sample = data[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    control = sum(1 for b in sample if b < 9 or 13 < b < 32 or b == 127)
    return control > len(sample) * 0.02


def _utf16_guess(data):
    """'utf-16-le'/'utf-16-be' for BOM-less UTF-16 text (every other byte zero), else None."""
    sample = data[:4096]
    if len(sample) < 8 or len(sample) % 2:
        return None
    even, odd = sample[0::2], sample[1::2]
    if odd.count(0) > len(odd) * 0.4 and even.count(0) < len(even) * 0.1:
        return "utf-16-le"
    if even.count(0) > len(even) * 0.4 and odd.count(0) < len(odd) * 0.1:
        return "utf-16-be"
    return None


def decode_text(data):
    """(text, encoding name) if the bytes are text in some encoding, else (None, "")."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc), enc.replace("-sig", " (BOM)")
            except UnicodeDecodeError:
                break
    enc16 = _utf16_guess(data)
    if enc16:
        try:
            return data.decode(enc16), enc16
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if _is_binary(data):
        return None, ""
    # Legacy 8-bit text. East-Asian encodings use pairs of high bytes; Western ones single accented letters.
    high = [i for i, b in enumerate(data[:20000]) if b >= 0x80]
    paired = sum(1 for i in high if i + 1 < len(data) and data[i + 1] >= 0x40 and (data[i - 1] >= 0x80 if i else False))
    if high and paired > len(high) * 0.4:
        for enc in ("cp932", "gbk", "big5", "cp949"):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                continue
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def hex_dump(data, limit=HEX_BYTES):
    lines = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {hexes:<47}  {text}")
    if len(data) > limit:
        lines.append(f"... {len(data) - limit:,} more bytes")
    return "\n".join(lines)


def _format_of(data):
    for magic, what in MAGIC:
        if data.startswith(magic):
            return what
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        return "zlib-compressed data"
    return ""


def _unpacked(data, what):
    try:
        if what.startswith("gzip"):
            return gzip.decompress(data)
        if what.startswith("zlib"):
            return zlib.decompress(data)
        if what.startswith("Zstandard"):
            import zstandard
            return zstandard.ZstdDecompressor().decompress(data, max_output_size=256 << 20)
    except Exception:
        return None
    return None


def inspect_bytes(data, depth=0):
    """{"text": what to show, "encoding", "format" ("" for plain text), "image": PIL image or None}."""
    text, encoding = decode_text(data)
    if text is not None:
        return {"text": text, "encoding": encoding, "format": "", "image": None}
    what = _format_of(data)
    out = {"text": "", "encoding": "", "format": what or "binary data", "image": None}
    if "image" in what or what.startswith("RIFF") and data[8:12] == b"WEBP":
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            out["image"] = img
        except Exception:
            pass
    unpacked = _unpacked(data, what) if what and depth < 3 else None
    if unpacked is not None:
        inner = inspect_bytes(unpacked, depth + 1)
        inner["format"] = f"{what} -> {inner['format'] or 'text (' + inner['encoding'] + ')'}"
        return inner
    lines = [f"Not text: {what or 'binary data the game reads with its own code'} ({len(data):,} bytes)."]
    if what.startswith("ZIP"):
        try:
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
            lines += ["", f"{len(names)} file(s) inside:"] + [f"  {n}" for n in names[:500]]
        except Exception as e:
            lines.append(f"(couldn't list it: {e})")
    strings = [m.group().decode("ascii") for m in _STRINGS.finditer(data[:1 << 20])][:MAX_STRINGS]
    if strings:
        lines += ["", f"Readable strings ({len(strings)}{'+' if len(strings) == MAX_STRINGS else ''}):"]
        lines += [f"  {s}" for s in strings]
    lines += ["", "Hex dump:", hex_dump(data)]
    out["text"] = "\n".join(lines)
    return out
