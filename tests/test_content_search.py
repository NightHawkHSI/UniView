"""Search inside files: matching text in stored bytes."""

from uniview.content_search import find_in


def test_case_insensitive_utf8_and_utf16():
    data = b"\x00\x01header PlayerSpawn more " + "playerspawn here".encode("utf-16-le") + b"\xff"
    hits = find_in(data, "PLAYERSPAWN")
    assert len(hits) == 2
    assert "PlayerSpawn" in hits[0][1] and "playerspawn here" in hits[1][1]
    assert hits[0][0] == data.find(b"PlayerSpawn")


def test_limit_and_no_match():
    data = b"ab " * 100
    assert len(find_in(data, "ab", limit=3)) == 3
    assert find_in(data, "zz") == []


def test_snippet_marks_cut_context_and_hides_control_bytes():
    data = b"x" * 100 + b"\x01key=value\x02" + b"y" * 100
    (_off, text), = find_in(data, "key=value")
    assert text.startswith("\u2026") and text.endswith("\u2026") and ".key=value." in text
