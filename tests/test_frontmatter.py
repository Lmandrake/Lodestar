"""Tests for lib.frontmatter — the flat-scalar YAML subset used by loop state files."""

import unittest

from lib import frontmatter


LOOP_FILE = """---
id: pillar-review
title: PILLAR quarterly review
class: active
state: open
account: "ACCT-0000 / Alpha"
repo: ~/dev/pillar
est_close: 90m
actual_spent: 0m
opened: 2026-08-28
last_touch: 2026-08-28T09:12-07:00 desktop/cc
deadline: 2026-09-15
---
Freeform notes / next action.
"""


class TestParse(unittest.TestCase):
    def test_parses_every_field_of_the_spec_example(self):
        fields, body = frontmatter.parse(LOOP_FILE)
        self.assertEqual(fields["id"], "pillar-review")
        self.assertEqual(fields["title"], "PILLAR quarterly review")
        self.assertEqual(fields["class"], "active")
        self.assertEqual(fields["state"], "open")
        self.assertEqual(fields["est_close"], "90m")
        self.assertEqual(body, "Freeform notes / next action.\n")

    def test_strips_quotes_from_a_quoted_value(self):
        fields, _ = frontmatter.parse(LOOP_FILE)
        self.assertEqual(fields["account"], "ACCT-0000 / Alpha")

    def test_splits_on_the_first_colon_only(self):
        # last_touch's value contains colons of its own; they must survive.
        fields, _ = frontmatter.parse(LOOP_FILE)
        self.assertEqual(fields["last_touch"], "2026-08-28T09:12-07:00 desktop/cc")

    def test_preserves_key_order_including_unknown_keys(self):
        text = "---\nid: x\nzzz_custom: 1\nstate: open\n---\n"
        fields, _ = frontmatter.parse(text)
        self.assertEqual(list(fields), ["id", "zzz_custom", "state"])

    def test_no_frontmatter_means_empty_fields_and_all_body(self):
        fields, body = frontmatter.parse("just prose\n")
        self.assertEqual(fields, {})
        self.assertEqual(body, "just prose\n")

    def test_empty_value_parses_as_empty_string(self):
        fields, _ = frontmatter.parse("---\ndeadline:\n---\n")
        self.assertEqual(fields["deadline"], "")

    def test_blank_lines_and_comments_in_frontmatter_are_ignored(self):
        fields, _ = frontmatter.parse("---\nid: x\n\n# a comment\nstate: open\n---\n")
        self.assertEqual(fields, {"id": "x", "state": "open"})

    def test_unterminated_frontmatter_raises(self):
        # A truncated state file must fail loudly; silently treating it as body
        # would let board.py overwrite real state with nothing.
        with self.assertRaises(ValueError):
            frontmatter.parse("---\nid: x\nstate: open\n")


class TestDump(unittest.TestCase):
    def test_round_trip_is_stable(self):
        fields, body = frontmatter.parse(LOOP_FILE)
        once = frontmatter.dump(fields, body)
        twice = frontmatter.dump(*frontmatter.parse(once))
        self.assertEqual(once, twice)

    def test_round_trip_preserves_semantics(self):
        fields, body = frontmatter.parse(LOOP_FILE)
        reparsed, rebody = frontmatter.parse(frontmatter.dump(fields, body))
        self.assertEqual(reparsed, fields)
        self.assertEqual(rebody, body)

    def test_quotes_a_value_containing_colon_space(self):
        out = frontmatter.dump({"title": "PILLAR: quarterly review"}, "")
        self.assertIn('title: "PILLAR: quarterly review"', out)

    def test_quotes_a_value_with_surrounding_whitespace(self):
        out = frontmatter.dump({"note": " padded "}, "")
        self.assertIn('note: " padded "', out)

    def test_leaves_an_ordinary_value_bare(self):
        out = frontmatter.dump({"account": "ACCT-0000 / Alpha"}, "")
        self.assertIn("account: ACCT-0000 / Alpha", out)

    def test_body_with_no_trailing_newline_round_trips(self):
        fields, body = frontmatter.parse("---\nid: x\n---\nno trailing newline")
        self.assertEqual(body, "no trailing newline")
        reparsed, rebody = frontmatter.parse(frontmatter.dump(fields, body))
        self.assertEqual(rebody, body)

    def test_empty_body_round_trips(self):
        fields, body = frontmatter.parse("---\nid: x\n---\n")
        self.assertEqual(body, "")
        reparsed, rebody = frontmatter.parse(frontmatter.dump(fields, body))
        self.assertEqual(rebody, "")

    def test_escapes_a_double_quote_inside_a_quoted_value(self):
        out = frontmatter.dump({"title": 'say: "hi"'}, "")
        reparsed, _ = frontmatter.parse(out)
        self.assertEqual(reparsed["title"], 'say: "hi"')


if __name__ == "__main__":
    unittest.main()
