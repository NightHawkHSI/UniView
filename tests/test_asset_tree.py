"""Name wrapping in the asset list."""

from uniview.ui.asset_tree import wrap_name


def fits(width):
    return lambda s: len(s) <= width


def test_short_name_one_line():
    assert wrap_name("Arm", fits(10)) == (["Arm"], True)


def test_wraps_after_underscore():
    lines, complete = wrap_name("RC_Laser_mk4_V_arrows", fits(14))
    assert lines == ["RC_Laser_mk4_", "V_arrows"] and complete


def test_wraps_anywhere_without_separators():
    lines, complete = wrap_name("ABCDEFGHIJKLMNOP", fits(10))
    assert lines == ["ABCDEFGHIJ", "KLMNOP"] and complete


def test_too_long_reports_incomplete():
    lines, complete = wrap_name("a_very_long_mesh_name_that_goes_on_LOD0", fits(12))
    assert len(lines) == 2 and not complete and "".join(lines) == "a_very_long_mesh_name_that_goes_on_LOD0"


def test_separator_too_early_is_ignored():
    lines, _ = wrap_name("a_bcdefghijklmnop", fits(10))  # breaking after "a_" would waste the line
    assert lines[0] == "a_bcdefghi"


def test_one_line_mode():
    assert wrap_name("ABCDEFGHIJKLMNOP", fits(10), max_lines=1) == (["ABCDEFGHIJKLMNOP"], False)
