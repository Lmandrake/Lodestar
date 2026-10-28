"""Tests for lib.duration — the short-duration parser, and its "unknown" sentinel.

T4 ruling, 2026-10-14 (question card "Yes, mark them 'unknown'",
docs/superpowers/specs/2026-10-14-project-tracking-system-design.md): `actual_spent` loops used
to seed `0m` for "never recorded", which read as a real zero to anyone who didn't already know
the convention. `duration.UNKNOWN` ("unknown") is the explicit replacement, and it must never be
treated as zero by anything that calls `minutes()`.
"""

import unittest

from lib import duration


class MinutesTest(unittest.TestCase):
    def test_ordinary_durations_still_parse(self):
        self.assertEqual(duration.minutes("2h"), 120)
        self.assertEqual(duration.minutes("90m"), 90)
        self.assertEqual(duration.minutes("1h30m"), 90)
        self.assertEqual(duration.minutes("45"), 45)

    def test_unknown_is_not_a_duration(self):
        self.assertIsNone(duration.minutes("unknown"))

    def test_unknown_is_case_and_whitespace_insensitive(self):
        for text in ("UNKNOWN", "Unknown", "  unknown  ", "\tunknown\n"):
            self.assertIsNone(duration.minutes(text), text)

    def test_unknown_is_never_read_as_zero(self):
        """The whole point: unknown must behave like "no value", not like a parsed 0."""
        self.assertIsNone(duration.minutes(duration.UNKNOWN))
        self.assertNotEqual(duration.minutes(duration.UNKNOWN), 0)

    def test_empty_and_missing_still_return_none(self):
        self.assertIsNone(duration.minutes(""))
        self.assertIsNone(duration.minutes(None))

    def test_unparseable_prose_still_returns_none(self):
        self.assertIsNone(duration.minutes("unset — pending scope decision"))


class IsUnknownTest(unittest.TestCase):
    def test_recognizes_the_sentinel(self):
        self.assertTrue(duration.is_unknown("unknown"))
        self.assertTrue(duration.is_unknown("  Unknown  "))

    def test_rejects_everything_else(self):
        for text in ("0m", "", None, "2h", "unknowable", "unset — pending scope decision"):
            self.assertFalse(duration.is_unknown(text), text)


class FormatMinutesTest(unittest.TestCase):
    def test_format_minutes_is_unaffected(self):
        """format_minutes is the inverse of a parsed duration, not of the sentinel — it is
        never asked to render "unknown" back out, so None still means "no value to show"."""
        self.assertEqual(duration.format_minutes(None), "")
        self.assertEqual(duration.format_minutes(90), "1h30m")


if __name__ == "__main__":
    unittest.main()
