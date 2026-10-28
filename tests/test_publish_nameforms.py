"""Name forms derived from names.txt (bin/lib/publish/nameforms.py). Every name is synthetic."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))

from lib.publish import nameforms  # noqa: E402
from lib.publish import scan as scan_mod  # noqa: E402

NAMES = ["Quentin Arbuckle", "Patricia Wexford = Pat, Patty", "William Young", "Grace Holloway",
         "Xiomara Delacroix Jr.", "Li Wu", "Zed Quill",
         # first names / surnames that are also ordinary words (all synthetic pairings)
         "Read Ostrander", "Cam Lindqvist", "Hang Pemberton", "Will Thornbury", "Mark Vellacott",
         "Sam Okonkwo", "Ysolde Hallworth", "Brisa Jarrow", "Ottoline Farrant", "Hope Fenwick",
         "Ruben Line", "Mona Read", "Thessaly Cotterill", "Corwin Vantongeren", "Bexley Brannigan",
         "Isaura Holtzclaw", "Ivo Speed", "Wen Stack", "Lars Constant"]
FORMS = nameforms.NameForms(NAMES)


def hits(text):
    return [m for _p, m in FORMS.find(text)]


class RareNames(unittest.TestCase):
    def test_first_last_and_nickname_alone(self):
        self.assertEqual(hits("Ask Quentin about it."), ["Quentin"])
        self.assertEqual(hits("Arbuckle's team at the centre"), ["Arbuckle's"[:8]])
        self.assertEqual(hits("Patty said so"), ["Patty"])
        self.assertEqual(hits("xiomara replied"), ["xiomara"])

    def test_possessive_and_case(self):
        self.assertTrue(hits("QUENTIN'S meeting"))
        self.assertTrue(hits("quentin's meeting"))

    def test_first_initial_and_initial_last(self):
        self.assertTrue(hits("sent by Zed Q. yesterday"))
        self.assertTrue(hits("Zed Q"))
        self.assertTrue(hits("from Q. Arbuckle"))
        self.assertTrue(hits("QArbuckle"))

    def test_last_first_order(self):
        self.assertIn("Arbuckle, Quentin", hits("Arbuckle, Quentin (GSFC)"))

    def test_handles(self):
        for text in ("@qarbuckle", "quentin.arbuckle", "quentin_arbuckle", "arbuckle-quentin",
                     "ping qarbuckle now", "@xdelacroix", "email xdelacroix today"):
            self.assertTrue(hits(text), text)

    def test_unrelated_text_is_clean(self):
        self.assertEqual(hits("the xdelacroix module"), [])      # flast alone is a word-like token
        for text in ("The quick brown fox", "A pattern of patterns",
                     "arbitrary values", "Patrick went home"):
            self.assertEqual(hits(text), [], text)


class AmbiguousWords(unittest.TestCase):
    def test_common_words_alone_do_not_hit(self):
        for text in ("Will you come?", "The young engineer will go", "Grace period ends",
                     "Mark the box. Young people", "a grace note", "Thank Grace"[:0] or "grace"):
            self.assertEqual(hits(text), [], text)

    def test_context_makes_them_hit(self):
        self.assertTrue(hits("Meeting with Grace on Tuesday"))
        self.assertTrue(hits("Grace from the GSFC lab sent an email"))
        self.assertTrue(hits("our colleague Young"))

    def test_mid_sentence_capital_after_lowercase_word_hits(self):
        self.assertTrue(hits("then tell Grace the plan"))
        self.assertEqual(hits("Grace is here"), [])        # sentence-initial, no work word

    def test_title_case_run_is_not_mid_sentence(self):
        self.assertEqual(hits("How Models Use Young Contexts"), [])
        self.assertEqual(hits('for x in (("Grace", 1)):'), [])
        self.assertEqual(hits("GRACE_PERIOD and GRACE0"), [])

    def test_context_must_be_on_the_same_line(self):
        self.assertEqual(hits("Grace\n\nthe meeting is later"), [])

    def test_short_names_need_capital_and_context(self):
        self.assertEqual(hits("li and wu"), [])
        self.assertTrue(hits("Li joined the team call"))

    def test_nickname_syntax_does_not_leak_into_full_name_matching(self):
        lists = scan_mod.Lists(NAMES, ["ZORBLAX"], ["acct-x-1"], {})
        self.assertIn("name", {c for c, _o, _m in scan_mod.text_hits("note: Patricia Wexford", lists)})
        self.assertNotIn("=", " ".join(m for _c, _o, m in scan_mod.text_hits("Patricia Wexford", lists)))


class CommonWordFalsePositives(unittest.TestCase):
    """Measured on the four prepared exports, 2026-10-26: each of these hit before."""

    def test_common_words_in_docs_and_code_do_not_hit(self):
        for text in (
            "none of them can stop a Read. Layer 4 is the only one",
            "the Read is refused and the reason names the digest",
            "class ReadResult:", "return ReadResult(\"ok\", body=body)",
            "Permission is hereby granted, free of charge, to any person",
            "state, leave the existing sidecar intact, not hang, not write",
            "The constant offset between a printed page number",
            "`T extureCam` -> `TextureCam`", "PM/CAM L. approved",
            "into the user's folders. Do it when asked",
            "and figures. An **escalation contract** says",
            "Needs `soffice` on PATH for the rendered originals.",
            "A mismatch is a HALT. Do not proceed.",
            "print(m.line())      # carries the reason", "p_read = sub.add_parser(\"read\")",
            "forward, a C-speed scan, to land on a record boundary",
            "const undoStack = [], redoStack = [];", "frozen = true; paintLink();",
            "## WSL: Windows Git Credential Manager from a linked worktree",
            "Draft in markdown first when the user asks for speed",
            "strings. Read the call site before flagging.",
            "Hand-written summary that must survive.",
            "Duck-types the one shape it must handle",
            "if platform.system() == \"Darwin\" else \"xdg-open\"",
            "assert rd.station == \"Mt Wilson\"", "Europe/London,1,GBP",
        ):
            self.assertEqual(hits(text), [], text)

    def test_bare_initial_pairs_do_not_hit(self):
        for text in ("y w G q", "h a R z", "x S. b", "k I m"):
            self.assertEqual(hits(text), [], text)


class PersonContext(unittest.TestCase):
    """Common-word names DO hit when the context marks a person."""

    def test_markers(self):
        cases = {
            "Meeting with Read tomorrow": "Read",
            "I asked Cam about the run": "Cam",
            "Hang's draft is ready": "Hang's"[:4],
            "Will said the data are late": "Will",
            "thanks to Mark (GSFC) for the files": "Mark",
            "Hi Ysolde, thanks for sending this": "Ysolde",
            "Draft an email to Patty asking her to send the harness code.": "Patty",
            "an ideogram that asks Patty to hand off the eval harness": "Patty",
            "Cam L. sent it": "Cam L.",
            "Read from GSFC joined": "Read",
            "Mark at GSFC wrote back": "Mark",
            "write to will@example.org": "will",
            "Hope's line: if there are no changes, you're missing something": "Hope",
            "the colleague Speed agreed": "Speed",
            "Read Ostrander reviewed it": "Read",
        }
        for text, want in cases.items():
            self.assertIn(want, hits(text), text)

    def test_initial_with_real_surname_still_hits(self):
        self.assertTrue(hits("from Q. Arbuckle"))
        self.assertTrue(hits("M. Read approved"))


class SecondModelCounterexamples(unittest.TestCase):
    """Strings an adversarial GPT review (2026-10-26) showed the first precision pass let through."""

    def test_each_names_the_person(self):
        cases = [
            ("Assigned to Cam for review.", "Cam"), ("Reviewed by Grace.", "Grace"),
            ("Mark approved the release.", "Mark"), ("Cam owns the eval harness.", "Cam"),
            ("Grace will review the plan at the team sync.", "Grace"),
            ("Review Mark's meeting notes.", "Mark"), ("Use Hope's line for the demo.", "Hope"),
            ("Quentin tests the scanner.", "Quentin"), ("Quentin scores the responses.", "Quentin"),
            ("Dr. Arbuckle's model needs review.", "Arbuckle"), ("Patty's test failed.", "Patty"),
            ("assignees: [Mark, Grace]", "Grace"), ("| reviewer | Cam |", "Cam"),
            ("Please contact @mark.", "mark"), ("assignee: cam.l", "cam.l"),
            ("Contact: quentin.dev@example.org", "quentin"),
            ("Contact: build.quentin@example.org", "quentin"),
            ("Contact: quentin2@example.org", "quentin2"[:7]),
            ("Meeting with Mark(GSFC)", "Mark"), ("CC: SAM", "SAM"),
            ("Hi CAM, thanks for sending this.", "CAM"), ("Attendee: Cam L", "Cam"),
            ("Li is on the team call.", "Li"), ("Quentin and Will", "Will"),
        ]
        for text, want in cases:
            self.assertIn(want, " ".join(hits(text)), text)

    def test_eponyms_stay_silent(self):
        for text in ("90% Wilson interval", "Goodhart's law applies", "a Bradley-Terry fit"):
            forms = nameforms.NameForms(["Ada Wilson", "Ike Goodhart", "Ola Bradley"])
            self.assertEqual([m for _p, m in forms.find(text)], [], text)


class ColdReadLeaks(unittest.TestCase):
    """The sentence shapes a cold read caught after a 0-hit scan (2026-10-26), rewritten as
    invented sentences on synthetic names. Every one must still be caught."""

    def test_colleague_first_names_in_prose(self):
        for text in (
            "This is Thessaly's, Corwin's and Bexley's own garden.",
            "  Thessaly offered, or (later, with a ladder) a second coat of paint.",
            "which supports Thessaly's offer of spare chairs.",
            "Should we write down the recipe before Isaura moves away?",
            "Write a note that asks Patty to hand over the spare keys.",
            "Hi Ysolde, thanks for the kite, but no.",
            "The whole point of the picnic is Hope's line: if there is no rain",
        ):
            self.assertTrue(hits(text), text)

    def test_cited_authors_are_still_found(self):
        # a public citation is allowed by a manifest allowance, never by the scanner
        self.assertTrue(hits("UX critics of surveys (Ysolde Hallworth, Brisa Jarrow)"))
        self.assertTrue(hits("Co-I: Dr. Farrant"))


class ScannerIntegration(unittest.TestCase):
    def setUp(self):
        self.lists = scan_mod.Lists(NAMES, ["ZORBLAX"], ["acct-x-1"], {})

    def classes(self, text):
        return {c for c, _o, _m in scan_mod.text_hits(scan_mod.normalise(text), self.lists)}

    def test_first_name_with_centre_is_a_name_form_hit(self):
        self.assertIn("name_form", self.classes("Quentin at the Pasadena centre"))

    def test_camel_case_forms(self):
        self.assertIn("name_form", self.classes("ping QuentinArbuckle"))

    def test_staffing_levels(self):
        self.assertIn("staffing", self.classes("allocated 0.5 FTE and 2 work-years"))
        self.assertNotIn("staffing", self.classes("the fte module"))

    def test_clean_prose_is_clean(self):
        self.assertEqual(self.classes("A workflow engine. Run `make test` to start the demo."),
                         set())


if __name__ == "__main__":
    unittest.main()
