"""Machine labels, `applies:` section selection, label-matched skills, and the preview diff.

The live ~/.claude is never touched: every test that renders goes through
test_claudeconf.ConfTest, which redirects CLAUDE_HOME into a temp directory.
"""

import os
import tempfile
import unittest

import claudeconf
from lib import claudeskills, machine
from tests.test_claudeconf import ConfTest
from tests import needs_private, real_home_key, real_key  # noqa: E402

LABELLED = """| Key | Role | Locks | Labels | Notes |
|-----|------|-------|--------|-------|
| test-box | testbox | | mac, work | a work Mac |
| home-box | home | flag | windows-wsl, home | the home box |
| bare-box | bare | | | no labels declared |
"""

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestLabelParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-labels-")
        self.path = _write(os.path.join(self.tmp, "machines.md"), LABELLED)

    def test_labels_are_read_per_machine_in_declared_order(self):
        table = machine.labels_table(self.path)
        self.assertEqual(table["test-box"], ["mac", "work"])
        self.assertEqual(table["home-box"], ["windows-wsl", "home"])
        self.assertEqual(table["bare-box"], [])

    def test_unregistered_machine_has_no_labels_and_does_not_raise(self):
        self.assertEqual(machine.labels("nobody", self.path), [])

    def test_a_table_with_no_labels_column_reads_as_unlabelled(self):
        old = _write(os.path.join(self.tmp, "old.md"),
                     "| Key | Role |\n|-----|------|\n| a | b |\n")
        self.assertEqual(machine.labels_table(old), {"a": []})

    @needs_private("machines.md")
    def test_the_real_registry_declares_the_agreed_labels(self):
        real = os.path.join(REPO, "machines.md")
        table = machine.labels_table(real)
        self.assertEqual(table[real_key("desktop")], ["mac", "work"])
        self.assertEqual(table[real_key("laptop")], ["mac", "work", "laptop"])
        self.assertEqual(table[real_home_key()], ["windows-wsl", "home"])

    def test_known_tokens_include_all_keys_and_labels(self):
        known = machine.known_selector_tokens(self.path)
        for token in ("all", "test-box", "mac", "windows-wsl", "home"):
            self.assertIn(token, known)

    def test_unknown_tokens_are_named(self):
        self.assertEqual(machine.unknown_selector_tokens("mac, mca+work", self.path), ["mca"])
        self.assertEqual(machine.unknown_selector_tokens("all", self.path), [])


class TestSelector(unittest.TestCase):
    def match(self, spec, key="k", labels=("mac", "work")):
        return machine.selector_matches(spec, key, labels)

    def test_all_and_empty_match_everything(self):
        self.assertTrue(self.match("all"))
        self.assertTrue(self.match(""))
        self.assertTrue(self.match("all", labels=()))

    def test_a_label_matches(self):
        self.assertTrue(self.match("mac"))
        self.assertFalse(self.match("windows-wsl"))

    def test_commas_are_any_of(self):
        self.assertTrue(self.match("windows-wsl, work"))

    def test_plus_is_all_of(self):
        self.assertTrue(self.match("mac+work"))
        self.assertFalse(self.match("mac+laptop"))

    def test_a_machine_key_still_matches_itself(self):
        # Backward compatibility: every pre-label skills.md row is a key list.
        self.assertTrue(self.match("mt-001, mt-002", key="mt-002", labels=()))
        self.assertFalse(self.match("mt-001, mt-002", key="tower", labels=()))

    def test_case_and_backticks_are_ignored(self):
        self.assertTrue(self.match("`MAC`"))


class TestSkillsLabelMatching(unittest.TestCase):
    def row(self, machines):
        return claudeskills.Row("s", "/x", "", machines)

    def test_label_row_applies_by_label(self):
        self.assertTrue(claudeskills.applies(self.row("mac"), "any", ["mac", "work"]))
        self.assertFalse(claudeskills.applies(self.row("mac"), "any", ["windows-wsl"]))

    def test_key_list_rows_keep_their_meaning(self):
        row = self.row("mt-001, mt-002")
        self.assertTrue(claudeskills.applies(row, "mt-001", []))
        self.assertFalse(claudeskills.applies(row, "tower", ["windows-wsl", "home"]))

    @needs_private("claude/skills.md")
    def test_labels_default_to_the_registry(self):
        # No labels passed: read from the real machines.md.
        self.assertTrue(claudeskills.applies(self.row("mac"), real_key("laptop")))
        self.assertFalse(claudeskills.applies(self.row("mac"), real_home_key()))
        self.assertTrue(claudeskills.applies(self.row("home"), real_home_key()))

    @needs_private("claude/skills.md", "machines.md")
    def test_the_real_table_scopes_the_converted_rows(self):
        table = {row.skill: row for row in claudeskills.rows(
            os.path.join(REPO, "claude", "skills.md"))}
        for skill in ("fetcher", "m365-mail-calendar"):
            self.assertEqual(table[skill].machines, "work", skill)
        for skill in ("cowork-bridge", "waypoint", "mac-gui-driving", "mac-launchd-jobs",
                      "mac-permissions", "mac-power-and-sleep", "mac-preferences"):
            self.assertEqual(table[skill].machines, "mac", skill)
        for skill, row in table.items():
            if row.machines in ("mac", "work"):
                self.assertTrue(claudeskills.applies(row, real_key("desktop")), skill)
                self.assertTrue(claudeskills.applies(row, real_key("laptop")), skill)
                self.assertFalse(claudeskills.applies(row, real_home_key()), skill)

    def test_the_real_table_names_no_unknown_selector(self):
        table = claudeskills.rows(os.path.join(REPO, "claude", "skills.md"))
        self.assertEqual(claudeskills.unknown_selectors(
            table, os.path.join(REPO, "machines.md")), [])

    def test_a_typod_selector_is_a_fault(self):
        faults = claudeskills.unknown_selectors(
            [self.row("mca")], os.path.join(REPO, "machines.md"))
        self.assertEqual(len(faults), 1)
        self.assertIn("mca", faults[0])


class LabelledConfTest(ConfTest):
    def setUp(self):
        super().setUp()
        _write(os.path.join(self.root, "machines.md"), LABELLED)


class TestSectionSelection(LabelledConfTest):
    def test_no_header_means_all(self):
        self.assertEqual(claudeconf.split_applies("# X\n")[0], "all")

    def test_header_is_parsed_and_stripped(self):
        selector, body = claudeconf.split_applies("<!-- applies: mac, work -->\n# X\nbody\n")
        self.assertEqual(selector, "mac, work")
        self.assertEqual(body, "# X\nbody\n")

    def test_a_header_not_on_the_first_line_is_just_content(self):
        text = "# X\n<!-- applies: mac -->\n"
        self.assertEqual(claudeconf.split_applies(text), ("all", text))

    def test_render_includes_matching_and_excludes_others(self):
        self.section("15-mac.md", "<!-- applies: mac -->\n# Mac only\n\nm.\n")
        self.section("16-wsl.md", "<!-- applies: windows-wsl -->\n# WSL only\n\nw.\n")
        text = claudeconf.render_text("test-box")
        self.assertIn("# Mac only", text)
        self.assertNotIn("# WSL only", text)
        self.assertNotIn("<!-- applies", text)
        self.assertIn("# First", text)   # unheaded sections still render

    def test_all_of_selector(self):
        self.section("15-both.md", "<!-- applies: mac+home -->\n# Both\n")
        self.assertNotIn("# Both", claudeconf.render_text("test-box"))
        self.section("15-both.md", "<!-- applies: mac+work -->\n# Both\n")
        self.assertIn("# Both", claudeconf.render_text("test-box"))

    def test_unlabelled_machine_gets_only_all_sections(self):
        self.section("machines/bare-box.md", "# Machine\n")
        self.section("15-mac.md", "<!-- applies: mac -->\n# Mac only\n")
        text = claudeconf.render_text("bare-box")
        self.assertNotIn("# Mac only", text)
        self.assertIn("# Second", text)

    def test_a_missing_machine_section_still_refuses_to_render(self):
        # The protection for a machine that is not enrolled: labels must not bypass it.
        with self.assertRaises(SystemExit):
            claudeconf.render_text("home-box")

    def test_the_banner_says_where_to_edit(self):
        text = claudeconf.render_text("test-box")
        self.assertTrue(text.startswith(claudeconf.BANNER))
        self.assertIn("edit ~/dev/Lodestar/claude/global/", text.split("\n")[1])
        self.assertIn("NOT THIS FILE", text.split("\n")[1])

    def test_legacy_banner_is_not_offered_as_a_chunk(self):
        old = claudeconf.LEGACY_BANNERS[0] + "# First\n\nShared one.\n"
        self.assertEqual(claudeconf._blocks(old), ["# First\n\nShared one.\n"])

    def test_capture_of_the_rendered_file_has_nothing_new(self):
        self.section("15-mac.md", "<!-- applies: mac -->\n# Mac only\n\nm.\n")
        rendered = claudeconf.render_text("test-box")
        self.assertEqual(claudeconf.capture_diff(rendered, rendered), [])

    def test_import_keeps_the_applies_header(self):
        path = self.section("15-mac.md", "<!-- applies: mac -->\n# Mac only\n\nm.\n")
        rendered = claudeconf.render_text("test-box")
        _write(claudeconf.LIVE_CLAUDE_MD, rendered.replace("m.\n", "m, edited.\n"))
        self.assertEqual(self.run_cmd("import"), 0)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("<!-- applies: mac -->\n# Mac only"))
        self.assertIn("edited", text)

    def test_import_ignores_sections_this_machine_does_not_render(self):
        self.section("16-wsl.md", "<!-- applies: windows-wsl -->\n# WSL only\n")
        rendered = claudeconf.render_text("test-box")
        _write(claudeconf.LIVE_CLAUDE_MD, rendered)
        self.assertEqual(self.run_cmd("import"), 0)   # would refuse on a count mismatch


class TestPreview(LabelledConfTest):
    def test_identical_live_file_is_all_kept(self):
        rendered = claudeconf.render_text("test-box")
        _write(claudeconf.LIVE_CLAUDE_MD, rendered)
        _, report, placeholder = claudeconf.preview("test-box")
        self.assertFalse(placeholder)
        self.assertTrue(all(e["status"] == claudeconf.KEPT for e in report), report)

    def test_a_live_only_section_is_reported_lost(self):
        rendered = claudeconf.render_text("test-box")
        extra = ("\n## A rule only this machine has\n\nNever do the particular thing that "
                 "this machine alone was told about, under any circumstances at all.\n")
        _write(claudeconf.LIVE_CLAUDE_MD, rendered + extra)
        _, report, _ = claudeconf.preview("test-box")
        lost = [e["heading"] for e in report if e["status"] == claudeconf.LOST]
        self.assertEqual(lost, ["## A rule only this machine has"])

    def test_a_render_only_section_is_reported_added(self):
        _write(claudeconf.LIVE_CLAUDE_MD, "# Machine\n\nA test box.\n")
        _, report, _ = claudeconf.preview("test-box")
        added = [e["heading"] for e in report if e["status"] == claudeconf.ADDED]
        self.assertIn("# First", added)

    def test_missing_machine_section_uses_a_placeholder_and_writes_nothing(self):
        live = _write(claudeconf.LIVE_CLAUDE_MD, "# Hand made\n\nSomething precious here "
                      "that must never be overwritten by a preview run.\n")
        before = os.stat(live).st_mtime_ns
        rendered, report, placeholder = claudeconf.preview("home-box")
        self.assertTrue(placeholder)
        self.assertIn("PREVIEW PLACEHOLDER", rendered)
        self.assertEqual(os.stat(live).st_mtime_ns, before)
        self.assertEqual(report[0]["status"], claudeconf.LOST)

    def test_cli_refuses_to_write_the_live_file(self):
        _write(claudeconf.LIVE_CLAUDE_MD, "# x\n")
        with self.assertRaises(SystemExit):
            self.run_cmd("preview", "--machine", "test-box", "--out", claudeconf.LIVE_CLAUDE_MD)

    def test_cli_exit_code_flags_loss(self):
        _write(claudeconf.LIVE_CLAUDE_MD, "# Lonely\n\nA sentence long enough to count as a "
               "substantive line in the comparison.\n")
        self.assertEqual(self.run_cmd("preview", "--machine", "test-box"), 1)


if __name__ == "__main__":
    unittest.main()
