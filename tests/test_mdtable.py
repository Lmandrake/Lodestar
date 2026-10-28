r"""Tests for lib/mdtable.py — the markdown table parser the whole board reads through.

This file did not exist until 2026-09-30, which is why the escaped-pipe bug below survived:
`sync/registry.md`, `machines.md`, `claude/skills.md` and `sync/tombstones.md` are all parsed
here, and nothing tested the parser itself.

Two properties are load-bearing and easy to "fix" into a bug:

1. **`parse` reads only the FIRST contiguous table.** That is deliberate per its own docstring
   — an old reader must not accidentally pick up a second table appended below. `tables()` is
   the newer accessor for getting at the rest. A session that "fixed" `parse` to read every
   table would silently change what every existing caller sees.
2. **An escaped pipe is not a column break.** Restored here after being measured wrong.
"""

import unittest

from lib import mdtable


SIMPLE = """# A document

| Name | Value |
|------|-------|
| alpha | one |
| beta | two |
"""


class TestParse(unittest.TestCase):
    def test_headers_are_lowercased_and_rows_are_dicts(self):
        rows = mdtable.parse(SIMPLE)
        self.assertEqual(rows, [{"name": "alpha", "value": "one"},
                                {"name": "beta", "value": "two"}])

    def test_no_table_gives_no_rows(self):
        self.assertEqual(mdtable.parse("nothing here\n"), [])

    def test_a_short_row_pads_missing_cells_rather_than_dropping_the_row(self):
        rows = mdtable.parse("| a | b |\n|---|---|\n| only |\n")
        self.assertEqual(rows, [{"a": "only", "b": ""}])


class TestOnlyTheFirstTable(unittest.TestCase):
    """🔴 Load-bearing, per `parse`'s own docstring — do not "fix" this into reading both.

    `CLAUDE.md` records the consequence of the same shape in `claude/skills.md`: a row added
    after the table's trailing blank line is invisible, with no fault raised. That invisibility
    is what `tables()` exists to work around, and changing `parse` would move the surprise
    into every existing caller instead.
    """

    two_tables = ("| a |\n|---|\n| first |\n\n"
                  "## Later\n\n| b |\n|---|\n| second |\n")

    def test_parse_sees_only_the_first(self):
        self.assertEqual(mdtable.parse(self.two_tables), [{"a": "first"}])

    def test_tables_sees_both(self):
        found = mdtable.tables(self.two_tables)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0][1], [{"a": "first"}])
        self.assertEqual(found[1][1], [{"b": "second"}])


class TestEscapedPipeIsNotAColumnBreak(unittest.TestCase):
    r"""🔴 Measured broken 2026-09-30, and it had already cost data once before.

    Every writer in this repo — `tombstones._escape`, `plans._escape`, and the same
    convention in `render.py`, `brief.py` and `maildump.py` — faithfully writes `\|` for a
    literal pipe. The parser used a plain `.split("|")` and ignored the escape, so the cell
    was truncated at the pipe and everything after it was promoted into the NEXT column.

    Measured through `tombstones.append`: writing the reason
    `a reason with a | pipe and more after it` read back as `a reason with a \`, with the
    remainder silently landing in `superseded by`.

    `lib/items.py` had already hit this in production on a subject line
    (`... REF-1 \| X \| 1/1/2026` turned a 3-column row into 5) and grew its own
    escape-aware splitter. The shared parser was never brought along, so every other caller
    kept the bug.
    """

    def test_an_escaped_pipe_stays_inside_its_cell(self):
        rows = mdtable.parse("| a | b |\n|---|---|\n| one \\| two | three |\n")
        self.assertEqual(rows, [{"a": "one | two", "b": "three"}])

    def test_the_escape_does_not_shift_later_columns(self):
        """The actual damage: not just a mangled cell, but every later cell moved left."""
        rows = mdtable.parse("| a | b | c |\n|---|---|---|\n| x \\| y | mid | last |\n")
        self.assertEqual(rows, [{"a": "x | y", "b": "mid", "c": "last"}])

    def test_several_escaped_pipes_in_one_cell(self):
        rows = mdtable.parse("| a | b |\n|---|---|\n| p \\| q \\| r | z |\n")
        self.assertEqual(rows, [{"a": "p | q | r", "b": "z"}])

    def test_an_unescaped_pipe_is_still_a_column_break(self):
        """The fix must not go the other way and swallow real delimiters."""
        rows = mdtable.parse("| a | b |\n|---|---|\n| one | two |\n")
        self.assertEqual(rows, [{"a": "one", "b": "two"}])

    def test_a_cell_ending_in_an_escaped_pipe_keeps_it(self):
        """The trailing-delimiter strip must not eat the backslash of a real escape."""
        rows = mdtable.parse("| a | b |\n|---|---|\n| one | two \\||\n")
        self.assertEqual(rows, [{"a": "one", "b": "two |"}])

    def test_unescaping_happens_after_splitting_not_before(self):
        r"""Order matters: a `replace("\\|", "|")` before splitting turns `\|` into a bare
        `|` and then splits ON it, reintroducing the exact bug. `items.py` documents this."""
        rows = mdtable.parse("| a | b |\n|---|---|\n| left \\| right | tail |\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["a"], "left | right")
        self.assertEqual(rows[0]["b"], "tail")


class TestRealStoresStillParse(unittest.TestCase):
    """The fix must not change what the live board files parse to.

    These are the four tables the fleet actually depends on, read through the real paths.
    """

    def test_the_registry_machines_skills_and_tombstones_all_still_parse(self):
        from lib import paths

        import os
        checks = [
            ("registry", paths.registry_file(), "name"),
            ("machines", paths.machines_file(), None),
            ("tombstones", paths.tombstones_file(), "id"),
        ]
        for label, path, expect_key in checks:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                rows = mdtable.parse(handle.read())
            self.assertTrue(rows, "%s parsed to zero rows" % label)
            if expect_key:
                self.assertIn(expect_key, rows[0],
                              "%s lost its %r column" % (label, expect_key))


if __name__ == "__main__":
    unittest.main()
