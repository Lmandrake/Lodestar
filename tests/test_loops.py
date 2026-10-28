"""Tests for lib.loops — state files, the (now-removed) WIP guard, and journal derivation."""

import inspect
import os
import shutil
import tempfile
import unittest

from lib import frontmatter, loops, paths

MACHINES = """| Key | Role |
|-----|------|
| test-box | testbox |
"""


class LoopsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lodestar-loops-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "loops"))

        self.machines = os.path.join(self.root, "machines.md")
        with open(self.machines, "w", encoding="utf-8") as handle:
            handle.write(MACHINES)

        self._saved = {
            k: os.environ.get(k)
            for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE", "LODESTAR_RUNTIME")
        }
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = "test-box"
        os.environ["LODESTAR_RUNTIME"] = "cc"
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _upsert(self, loop_id, **updates):
        body = updates.pop("body", None)
        return loops.upsert(loop_id, updates, body=body, machines_path=self.machines)

    def _make_active(self, loop_id):
        return self._upsert(loop_id, **{"class": "active"})


class TestIds(LoopsTest):
    def test_accepts_ordinary_ids(self):
        for good in ("lodestar", "pillar-review", "heat_28", "a.b"):
            self.assertTrue(loops.valid_id(good), good)

    def test_rejects_ids_that_would_escape_the_loops_directory(self):
        for bad in ("", ".", "..", "../etc/passwd", "with/slash", "Upper", "has space"):
            self.assertFalse(loops.valid_id(bad), bad)

    def test_upsert_refuses_a_bad_id(self):
        with self.assertRaises(loops.InvalidLoop):
            self._upsert("../escape")


class TestUpsert(LoopsTest):
    def test_creates_a_file_with_spec_defaults(self):
        fields, body = self._upsert("lodestar")
        self.assertEqual(fields["id"], "lodestar")
        self.assertEqual(fields["state"], "open")
        self.assertEqual(fields["actual_spent"], "unknown")
        self.assertEqual(body, "")
        self.assertTrue(os.path.exists(paths.loop_file("lodestar")))

    def test_a_new_loop_defaults_to_background_not_active(self):
        # Defaulting to active would make every casual touch silently claim dominant-focus
        # status — true regardless of whether a numeric cap enforces anything on top of it.
        fields, _ = self._upsert("lodestar")
        self.assertEqual(fields["class"], "background")

    def test_leaves_est_close_empty_rather_than_inventing_one(self):
        fields, _ = self._upsert("lodestar")
        self.assertEqual(fields["est_close"], "")

    def test_stamps_last_touch_with_role_and_runtime(self):
        fields, _ = self._upsert("lodestar")
        self.assertTrue(fields["last_touch"].endswith(" testbox/cc"), fields["last_touch"])

    def test_fields_are_written_in_spec_order(self):
        self._upsert("lodestar")
        text = open(paths.loop_file("lodestar"), encoding="utf-8").read()
        self.assertLess(text.index("id:"), text.index("title:"))
        self.assertLess(text.index("title:"), text.index("class:"))
        self.assertLess(text.index("class:"), text.index("state:"))

    def test_updates_an_existing_loop_without_losing_its_body(self):
        self._upsert("lodestar", body="Next: build sync.py\n")
        fields, body = self._upsert("lodestar", est_close="90m")
        self.assertEqual(body, "Next: build sync.py\n")
        self.assertEqual(fields["est_close"], "90m")

    def test_preserves_unknown_fields_added_by_another_runtime(self):
        path = paths.loop_file("lodestar")
        frontmatter.write_file(
            path,
            {"id": "lodestar", "class": "background", "state": "open",
             "cowork_hint": "from the airlock"},
            "body\n",
        )
        fields, _ = self._upsert("lodestar", est_close="30m")
        self.assertEqual(fields["cowork_hint"], "from the airlock")

    def test_rejects_an_unknown_class(self):
        with self.assertRaises(loops.InvalidLoop):
            self._upsert("lodestar", **{"class": "urgent"})

    def test_rejects_an_unknown_state(self):
        with self.assertRaises(loops.InvalidLoop):
            self._upsert("lodestar", state="abandoned")

    def test_explicit_body_replaces_the_old_one(self):
        self._upsert("lodestar", body="first\n")
        _, body = self._upsert("lodestar", body="second\n")
        self.assertEqual(body, "second\n")


class TestWipGuard(LoopsTest):
    """No numeric cap since 2026-09-03 (D24 supersedes D3: Lukas — "it's just not
    practicable"). Kept as TestWipGuard, and kept non-trivial, because these tests now pin
    the *absence* of a cap rather than describe a guard that no longer exists — a future
    session must not read a missing refusal as an oversight and "restore" it.
    """

    def test_three_active_open_loops_are_allowed(self):
        for name in ("one", "two", "three"):
            self._make_active(name)
        self.assertEqual(len(loops.active_open_ids()), 3)

    def test_a_fourth_active_loop_is_allowed(self):
        for name in ("one", "two", "three", "four"):
            self._make_active(name)
        self.assertEqual(len(loops.active_open_ids()), 4)

    def test_the_fourth_active_loop_is_written(self):
        for name in ("one", "two", "three", "four"):
            self._make_active(name)
        self.assertTrue(os.path.exists(paths.loop_file("four")))

    def test_many_active_loops_are_all_allowed_at_once(self):
        names = ("one", "two", "three", "four", "five", "six", "seven")
        for name in names:
            self._make_active(name)
        self.assertEqual(loops.active_open_ids(), sorted(names))

    def test_retouching_an_already_open_active_loop_still_works(self):
        for name in ("one", "two", "three", "four"):
            self._make_active(name)
        self._make_active("two")  # must not raise, and must not change the count
        self.assertEqual(len(loops.active_open_ids()), 4)

    def test_background_loops_are_uncapped(self):
        for index in range(12):
            self._upsert("bg%d" % index)
        self.assertEqual(loops.active_open_ids(), [])

    def test_parking_an_active_loop_still_works_without_needing_to_free_room(self):
        for name in ("one", "two", "three", "four"):
            self._make_active(name)
        self._upsert("two", state="parked")
        self._make_active("five")  # never needed the park — there was no cap to free
        self.assertEqual(sorted(loops.active_open_ids()), ["five", "four", "one", "three"])

    def test_closing_an_active_loop_still_works(self):
        for name in ("one", "two", "three", "four"):
            self._make_active(name)
        self._upsert("three", state="closed")
        self._make_active("five")
        self.assertEqual(sorted(loops.active_open_ids()), ["five", "four", "one", "two"])

    def test_wip_exceeded_no_longer_exists(self):
        # Pins the removal itself: a future "fix" that reintroduces the guard would have
        # to reintroduce this attribute too, and this test would catch it doing so.
        self.assertFalse(hasattr(loops, "WipExceeded"))
        self.assertFalse(hasattr(loops, "check_wip"))

    def test_wip_cap_constant_no_longer_exists(self):
        # The enforcement went in the same commit as check_wip/WipExceeded, but the
        # denominator (WIP_CAP) lingered for display code. Once every renderer stopped
        # consuming it, it had no reason to stay — pinned here so it cannot creep back.
        self.assertFalse(hasattr(loops, "WIP_CAP"))

    def test_enforce_wip_parameter_no_longer_exists(self):
        # `enforce_wip` outlived the cap it used to gate as a documented no-op, which is
        # a trap: a future caller could pass enforce_wip=True expecting enforcement and
        # get none, silently. Pinned here so a reintroduction goes red.
        self.assertNotIn("enforce_wip", inspect.signature(loops.upsert).parameters)


class TestDerive(unittest.TestCase):
    def test_open_then_touch_yields_an_open_loop(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "machine": "desktop", "runtime": "cc"},
            {"ts": "2026-08-28T11:00:00-07:00", "loop": "x", "event": "touch",
             "machine": "desktop", "runtime": "cc"},
        ])
        self.assertEqual(derived["x"]["state"], "open")
        self.assertEqual(derived["x"]["opened"], "2026-08-28")
        # Offset retained, because upsert writes it and the two must agree exactly.
        self.assertEqual(derived["x"]["last_touch"], "2026-08-28T11:00-07:00 desktop/cc")

    def test_park_then_resume_returns_to_open(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "x", "event": "park"},
        ])
        self.assertEqual(derived["x"]["state"], "parked")
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "x", "event": "park"},
            {"ts": "2026-08-28T12:00:00-07:00", "loop": "x", "event": "resume"},
        ])
        self.assertEqual(derived["x"]["state"], "open")

    def test_open_then_touch_seeds_actual_spent_unknown_not_zero(self):
        """Before a close event ever sets `spent`, the seed must read as unrecorded (T4,
        2026-10-14) rather than as a real zero — the same contract `upsert` carries."""
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open"},
            {"ts": "2026-08-28T11:00:00-07:00", "loop": "x", "event": "touch"},
        ])
        self.assertEqual(derived["x"]["actual_spent"], "unknown")

    def test_close_records_state_and_spent(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open"},
            {"ts": "2026-08-28T17:00:00-07:00", "loop": "x", "event": "close",
             "spent": "75m"},
        ])
        self.assertEqual(derived["x"]["state"], "closed")
        self.assertEqual(derived["x"]["actual_spent"], "75m")

    def test_tracked_fields_carried_on_events_are_applied(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "class": "active", "title": "PILLAR quarterly review",
             "est_close": "90m", "deadline": "2026-09-15",
             "account": "ACCT-0000 / Alpha", "repo": "~/dev/pillar"},
        ])
        self.assertEqual(derived["x"]["class"], "active")
        self.assertEqual(derived["x"]["title"], "PILLAR quarterly review")
        self.assertEqual(derived["x"]["est_close"], "90m")
        self.assertEqual(derived["x"]["deadline"], "2026-09-15")
        self.assertEqual(derived["x"]["account"], "ACCT-0000 / Alpha")

    def test_a_later_event_overrides_an_earlier_field(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "class": "background"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "x", "event": "touch",
             "class": "active"},
        ])
        self.assertEqual(derived["x"]["class"], "active")

    def test_activity_after_a_close_reopens_the_loop(self):
        # Legacy fallback, kept for events written before state was journaled: an event
        # that carries no `state` of its own still gets the old type-only reading.
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "close"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "touch"},
        ])
        self.assertEqual(derived["x"]["state"], "open")


class TestDeriveHonoursJournaledState(unittest.TestCase):
    """An event's own `state` field wins over the reading implied by its type.

    This is the fix for the defect that broke the Phase 0 gate: `touch.py --state` wrote
    the state into the loop file and nowhere else, so a plain touch of a *closed* loop left
    the file saying `closed` while `derive` — seeing activity after a close — reconstructed
    it as `open`. `board.py --rebuild` then silently stopped round-tripping that loop.
    """

    def test_a_touch_carrying_closed_does_not_reopen_the_loop(self):
        # The exact live scenario, at the derive() level.
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "state": "open"},
            {"ts": "2026-08-28T17:00:00-07:00", "loop": "x", "event": "close",
             "spent": "75m"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "touch",
             "state": "closed"},
        ])
        self.assertEqual(derived["x"]["state"], "closed")
        self.assertEqual(derived["x"]["actual_spent"], "75m")

    def test_a_touch_can_park_and_a_touch_can_reopen(self):
        # Both directions of the new field, through the event type that carries no
        # state meaning of its own.
        parked = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "state": "open"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "x", "event": "touch",
             "state": "parked"},
        ])
        self.assertEqual(parked["x"]["state"], "parked")

        reopened = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "close"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "x", "event": "touch",
             "state": "closed"},
            {"ts": "2026-08-28T11:00:00-07:00", "loop": "x", "event": "touch",
             "state": "open"},
        ])
        self.assertEqual(reopened["x"]["state"], "open")

    def test_a_declared_state_overrides_a_contradicting_event_type(self):
        # A park event that says `open` is self-contradictory, but the declared value is
        # what the writer put in the loop file, so honouring it is what keeps the
        # round-trip byte-identical. Consistency beats tidiness here.
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "park",
             "state": "open"},
        ])
        self.assertEqual(derived["x"]["state"], "open")

    def test_a_close_event_declaring_closed_is_still_closed(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "close",
             "state": "closed", "spent": "30m"},
        ])
        self.assertEqual(derived["x"]["state"], "closed")
        self.assertEqual(derived["x"]["actual_spent"], "30m")

    def test_an_unreadable_state_falls_back_to_the_event_type(self):
        # Forgiving reads (lib/journal.py): a junk value must not poison a rebuild, it
        # just leaves the old type-only reading in place.
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "close"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "touch",
             "state": "abandoned"},
        ])
        self.assertEqual(derived["x"]["state"], "open")
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "park",
             "state": None},
        ])
        self.assertEqual(derived["x"]["state"], "parked")

    def test_a_handoff_alone_creates_no_loop_and_moves_no_last_touch(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "machine": "desktop", "runtime": "cc"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "handoff",
             "machine": "laptop", "runtime": "cc"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "ghost", "event": "handoff"},
        ])
        self.assertEqual(derived["x"]["last_touch"], "2026-08-28T09:00-07:00 desktop/cc")
        self.assertNotIn("ghost", derived)

    def test_backfill_sets_fields_without_touching_state_or_last_touch(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open",
             "machine": "desktop", "runtime": "cc"},
            {"ts": "2026-10-25T09:00:00-07:00", "loop": "x", "event": "backfill",
             "title": "Real title", "repo": "/r", "opened": "2026-08-01"},
        ])
        self.assertEqual(derived["x"]["title"], "Real title")
        self.assertEqual(derived["x"]["opened"], "2026-08-01")
        self.assertEqual(derived["x"]["state"], "open")
        self.assertEqual(derived["x"]["last_touch"], "2026-08-28T09:00-07:00 desktop/cc")

    def test_retire_removes_a_loop_until_it_is_touched_again(self):
        ev = [{"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "open"},
              {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "retire"}]
        self.assertNotIn("x", loops.derive(ev))
        ev.append({"ts": "2026-08-30T09:00:00-07:00", "loop": "x", "event": "touch"})
        self.assertIn("x", loops.derive(ev))

    def test_a_run_event_carries_no_state_and_changes_none(self):
        # The shadow runner's event must keep animating a loop without reclassifying it.
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "x", "event": "close"},
            {"ts": "2026-08-29T09:00:00-07:00", "loop": "x", "event": "run"},
        ])
        self.assertEqual(derived["x"]["state"], "closed")

    def test_derives_several_loops_independently(self):
        derived = loops.derive([
            {"ts": "2026-08-28T09:00:00-07:00", "loop": "a", "event": "open"},
            {"ts": "2026-08-28T09:30:00-07:00", "loop": "b", "event": "open"},
            {"ts": "2026-08-28T10:00:00-07:00", "loop": "a", "event": "close"},
        ])
        self.assertEqual(derived["a"]["state"], "closed")
        self.assertEqual(derived["b"]["state"], "open")

    def test_events_without_a_loop_id_are_ignored(self):
        self.assertEqual(loops.derive([{"ts": "2026-08-28T09:00:00-07:00",
                                        "event": "touch"}]), {})

    def test_derived_fields_come_back_in_spec_order(self):
        derived = loops.derive([{"ts": "2026-08-28T09:00:00-07:00", "loop": "x",
                                 "event": "open"}])
        self.assertEqual(list(derived["x"])[:4], ["id", "title", "class", "state"])


if __name__ == "__main__":
    unittest.main()
