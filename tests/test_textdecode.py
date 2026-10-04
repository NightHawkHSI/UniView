"""Text assets: encodings, known formats inside, and binary data."""

import gzip
import io
import zipfile

from PIL import Image

from uniview.textdecode import decode_text, inspect_bytes


def test_encodings():
    assert decode_text("héllo".encode("utf-8")) == ("héllo", "utf-8")
    assert decode_text(b"\xef\xbb\xbfhi")[0] == "hi"
    assert decode_text("hi there".encode("utf-16"))[0] == "hi there"
    assert decode_text("plain words".encode("utf-16-le")) == ("plain words", "utf-16-le")
    assert decode_text("Joël Carrouché - free font licence".encode("cp1252")) == (
        "Joël Carrouché - free font licence", "cp1252")
    jp = "日本語のテキストです。ゲームの説明。" * 3
    assert decode_text(jp.encode("cp932")) == (jp, "cp932")
    assert decode_text(bytes(range(256)) * 4)[0] is None


def test_binary_gets_strings_and_hex():
    data = b"\xc5\x71\xb0\x40\x00\x00\x00\x02" + b"PlayerSpawnPoint" + bytes(range(40))
    found = inspect_bytes(data)
    assert found["image"] is None and found["format"] == "binary data"
    assert "PlayerSpawnPoint" in found["text"] and "00000000  c5 71 b0 40" in found["text"]


def test_known_formats():
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buf, "PNG")
    assert inspect_bytes(buf.getvalue())["image"].size == (3, 2)
    packed = inspect_bytes(gzip.compress(b"{\"a\": 1}"))
    assert packed["text"] == "{\"a\": 1}" and packed["format"].startswith("gzip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("levels/one.json", "{}")
    assert "levels/one.json" in inspect_bytes(buf.getvalue())["text"]
