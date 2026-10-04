"""Duplicate finder: grouping identical assets."""

from engines.sdk import Asset
from uniview.duplicates import extra_copies, group_duplicates, wasted


def a(name, size):
    return Asset("texture", name, name, uid=name, size=size)


def test_groups_shared_hashes_biggest_waste_first():
    x1, x2, x3, y1, y2, z = a("x1", 10), a("x2", 10), a("x3", 10), a("y1", 100), a("y2", 100), a("z", 5)
    groups = group_duplicates([(x1, b"x"), (y1, b"y"), (x2, b"x"), (z, b"z"), (y2, b"y"), (x3, b"x"),
                               (a("none", 1), None)])
    assert [len(g) for g in groups.values()] == [2, 3]  # y wastes 100, x wastes 20
    assert wasted(groups[b"x"]) == 20
    assert extra_copies(groups) == {"x2", "x3", "y2"}
