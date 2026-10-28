"""Tests for lib/claudesettings.py -- settings.json comparison and convergence.

The live ~/.claude is never touched: CLAUDE_HOME is redirected into a temp directory and
LODESTAR_ROOT/LODESTAR_MACHINE point at a temp board, the same way tests/test_claudeconf.py
does it. This module writes files, so a leaked path here would edit the real settings.json.
"""

import json
import os
import shutil
import tempfile
import unittest

from lib import claudesettings
from tests import needs_private, real_home_key  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DENY = [
    "Read(~/**/.env)",
    "Read(/**/.env)",
    "Read(~/**/.pem)",
    "Read(~/**/.key)",
    "Read(~/**/.ssh/**)",
    "Read(~/**/.auth_key)",
]


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-settings-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, relative, payload):
        path = os.path.join(self.tmp, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path


class TestDifferences(SettingsTest):
    def test_identical_settings_have_no_differences(self):
        payload = {"model": "opus", "env": {"A": "1"}}
        self.assertEqual(claudesettings.differences(payload, dict(payload)), [])

    def test_a_changed_top_level_key_is_reported(self):
        diffs = claudesettings.differences({"model": "opus"}, {"model": "sonnet"})
        self.assertIn("model", " ".join(diffs))

    def test_a_missing_deny_rule_is_reported(self):
        live = {"permissions": {"deny": ["Read(~/**/.env)"]}}
        other = {"permissions": {"deny": []}}
        self.assertTrue(claudesettings.differences(live, other))

    def test_base_url_is_never_reported(self):
        live = {"env": {"ANTHROPIC_BASE_URL": "https://a"}}
        other = {"env": {"ANTHROPIC_BASE_URL": "https://b"}}
        self.assertEqual(claudesettings.differences(live, other), [])

    def test_base_url_difference_does_not_mask_a_real_one(self):
        live = {"env": {"ANTHROPIC_BASE_URL": "https://a", "ANTHROPIC_MODEL": "opus"}}
        other = {"env": {"ANTHROPIC_BASE_URL": "https://b", "ANTHROPIC_MODEL": "sonnet"}}
        diffs = claudesettings.differences(live, other)
        self.assertIn("ANTHROPIC_MODEL", " ".join(diffs))
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(diffs))


class TestReport(SettingsTest):
    def test_missing_other_snapshot_is_stated_not_invented(self):
        live = self.write("live/settings.json", {"model": "opus"})
        lines, faults = claudesettings.report(live, os.path.join(self.tmp, "snap"), "me")
        self.assertEqual(faults, [])
        self.assertIn("no other machine", " ".join(lines).lower())

    def test_report_names_the_other_machine_and_its_freshness(self):
        live = self.write("live/settings.json", {"model": "opus"})
        self.write("snap/other/settings.json", {"model": "sonnet"})
        lines, _ = claudesettings.report(live, os.path.join(self.tmp, "snap"), "me")
        joined = " ".join(lines)
        self.assertIn("other", joined)
        self.assertIn("model", joined)

    def test_own_snapshot_is_not_compared_against_itself(self):
        live = self.write("live/settings.json", {"model": "opus"})
        self.write("snap/me/settings.json", {"model": "sonnet"})
        lines, _ = claudesettings.report(live, os.path.join(self.tmp, "snap"), "me")
        self.assertIn("no other machine", " ".join(lines).lower())


class TestClassification(unittest.TestCase):
    def test_auth_keys_are_never_synced(self):
        self.assertIn("apiKeyHelper", claudesettings.NEVER_SYNC)
        self.assertIn("env.ANTHROPIC_BASE_URL", claudesettings.NEVER_SYNC)

    def test_every_model_selecting_variable_is_never_synced(self):
        """Two machines can be two different Claude accounts, so a model id names a model
        that exists on ONE of them. Sharing one fleet-wide points the other machine at a
        model its endpoint will not serve -- the same lock-out NEVER_SYNC exists to prevent,
        arriving through the model name instead of the URL. All five, not just the three that
        happen to differ today: a value that coincides now is not a value that is shared."""
        for name in ("ANTHROPIC_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                     "ANTHROPIC_SMALL_FAST_MODEL"):
            self.assertIn("env.%s" % name, claudesettings.NEVER_SYNC)

    def test_a_render_cannot_move_this_machine_to_another_accounts_models(self):
        """The live failure this closes: base carried one provider's model ids while a machine
        runs another provider's, so the documented remedy would have silently repointed
        every model on this machine at an endpoint that does not serve them."""
        live = {"env": {"ANTHROPIC_MODEL": "acct-b.claude-sonnet-5"}}
        base = {"env": {"ANTHROPIC_MODEL": "acct-a.claude-opus-5[1m]"}}
        faults = []
        out = claudesettings.render(base, {}, live, faults)
        self.assertEqual(out["env"]["ANTHROPIC_MODEL"], "acct-b.claude-sonnet-5")
        self.assertEqual(faults, [])

    def test_seeding_cannot_publish_one_accounts_models_to_the_fleet(self):
        live = {"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "acct-b.claude-opus-5[1m]",
                        "KEEP": "1"}}
        base = claudesettings.seed_base(live, {})
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", base.get("env", {}))
        self.assertEqual(base["env"]["KEEP"], "1", "unrelated env must still be shared")

    def test_both_permission_lists_are_union_keys(self):
        self.assertIn("permissions.allow", claudesettings.UNION_KEYS)
        self.assertIn("permissions.deny", claudesettings.UNION_KEYS)

    def test_the_three_sets_do_not_overlap(self):
        # A key in two sets would make behaviour depend on which check ran first.
        self.assertFalse(claudesettings.NEVER_SYNC & claudesettings.UNION_KEYS)
        self.assertFalse(claudesettings.NEVER_SYNC & claudesettings.MACHINE_LOCAL_DEFAULT)
        self.assertFalse(claudesettings.UNION_KEYS & claudesettings.MACHINE_LOCAL_DEFAULT)
        self.assertFalse(claudesettings.NEVER_SHARE & claudesettings.UNION_KEYS)
        self.assertFalse(claudesettings.NEVER_SHARE & claudesettings.NEVER_SYNC)

    def test_permission_prompt_suppressors_are_never_shared(self):
        # The three keys that can switch off a permission prompt on a machine that never
        # asked for it. Sharing any of them fleet-wide is a security regression, not a
        # convenience.
        for path in ("permissions.defaultMode",
                     "permissions.disableBypassPermissionsMode",
                     "skipDangerousModePermissionPrompt"):
            self.assertIn(path, claudesettings.NEVER_SHARE)

    def test_bypass_mode_is_also_machine_local_for_seeding(self):
        self.assertIn("permissions.defaultMode", claudesettings.MACHINE_LOCAL_DEFAULT)
        self.assertIn("permissions.disableBypassPermissionsMode",
                      claudesettings.MACHINE_LOCAL_DEFAULT)

    def test_the_four_previously_replaced_permission_lists_are_unioned(self):
        # Every list-valued key Claude Code writes that means a *set of grants*. Omitting one
        # means a render replaces the live list wholesale and the user's entry disappears.
        for path in ("permissions.allow", "permissions.deny", "permissions.ask",
                     "permissions.additionalDirectories",
                     "disabledMcpjsonServers", "enabledMcpjsonServers"):
            self.assertIn(path, claudesettings.UNION_KEYS)


class TestSeedBase(unittest.TestCase):
    def test_union_keys_take_from_every_machine(self):
        live = {"permissions": {"allow": ["Read", "Bash"]}}
        peers = {"other": {"permissions": {"allow": ["Bash", "WebFetch"]}}}
        base = claudesettings.seed_base(live, peers)
        self.assertEqual(base["permissions"]["allow"], ["Bash", "Read", "WebFetch"])

    def test_every_deny_rule_survives_a_peer_that_has_none(self):
        # The literal reading of "most permissive" would drop these. It must not: they are
        # the rules blocking reads of .env, *.pem, *.key, .ssh/** and .auth_key.
        live = {"permissions": {"deny": list(DENY)}}
        peers = {"other": {"permissions": {"deny": []}}}
        base = claudesettings.seed_base(live, peers)
        for rule in DENY:
            self.assertIn(rule, base["permissions"]["deny"])

    def test_a_deny_rule_only_the_peer_has_is_also_kept(self):
        live = {"permissions": {"deny": ["Read(~/**/.env)"]}}
        peers = {"other": {"permissions": {"deny": ["Read(/**/.netrc)"]}}}
        base = claudesettings.seed_base(live, peers)
        self.assertEqual(base["permissions"]["deny"],
                         ["Read(/**/.netrc)", "Read(~/**/.env)"])

    def test_a_plugin_enabled_anywhere_is_enabled(self):
        live = {"enabledPlugins": {"a": False, "b": True}}
        peers = {"other": {"enabledPlugins": {"a": True, "c": True}}}
        base = claudesettings.seed_base(live, peers)
        self.assertEqual(base["enabledPlugins"], {"a": True, "b": True, "c": True})

    def test_a_marketplace_only_the_peer_knows_is_adopted(self):
        live = {}
        peers = {"other": {"extraKnownMarketplaces": {"m": {"source": {"repo": "x/y"}}}}}
        base = claudesettings.seed_base(live, peers)
        self.assertEqual(base["extraKnownMarketplaces"]["m"]["source"]["repo"], "x/y")

    def test_a_scalar_disagreement_takes_the_local_value(self):
        base = claudesettings.seed_base({"model": "opus"}, {"other": {"model": "sonnet"}})
        self.assertEqual(base["model"], "opus")

    def test_a_scalar_only_the_peer_has_is_not_adopted(self):
        # permissions.defaultMode: bypassPermissions is the real case. Hoovering it up would
        # turn off permission prompts fleet-wide on the strength of nobody's decision.
        peers = {"other": {"permissions": {"defaultMode": "bypassPermissions"}}}
        base = claudesettings.seed_base({"model": "opus"}, peers)
        self.assertNotIn("defaultMode", base.get("permissions", {}))

    def test_never_sync_keys_are_absent_from_the_base(self):
        live = {"apiKeyHelper": "/x.sh", "env": {"ANTHROPIC_BASE_URL": "https://a", "A": "1"}}
        base = claudesettings.seed_base(live, {})
        self.assertNotIn("apiKeyHelper", base)
        self.assertNotIn("ANTHROPIC_BASE_URL", base["env"])
        self.assertEqual(base["env"]["A"], "1")

    def test_machine_local_keys_are_absent_from_the_base(self):
        live = {"theme": "dark", "statusLine": {"padding": 2}, "model": "opus"}
        base = claudesettings.seed_base(live, {})
        for key in ("theme", "statusLine"):
            self.assertNotIn(key, base)
        self.assertEqual(base["model"], "opus")

    def test_hooks_are_seeded_from_the_machine_that_has_them(self):
        """Promoted 2026-09-01: `hooks` is shared, so a local hook reaches the base."""
        live = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                      "command": "/bin/true"}]}]}}
        base = claudesettings.seed_base(live, {})
        self.assertEqual(base["hooks"], live["hooks"])

    def test_a_peer_only_hook_is_not_seeded(self):
        """`hooks` is not a UNION key, so `seed_base` reads it from `live` only.

        The trap promotion does not fix: seeding on a machine with no hook yields a base with
        no hook, however shared the key is. Pinned because the fix looks like it should be
        "remove it from MACHINE_LOCAL_DEFAULT" and that alone silently does nothing.
        """
        peer = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                      "command": "/bin/true"}]}]}}
        base = claudesettings.seed_base({"model": "opus"}, {"other": peer})
        self.assertNotIn("hooks", base)

    def test_a_malformed_peer_is_skipped_not_fatal(self):
        base = claudesettings.seed_base({"model": "opus"}, {"other": "not-a-dict"})
        self.assertEqual(base["model"], "opus")

    def test_seeding_is_deterministic(self):
        live = {"permissions": {"allow": ["c", "a", "b"]}}
        peers = {"z": {"permissions": {"allow": ["d"]}},
                 "a": {"permissions": {"allow": ["e"]}}}
        first = claudesettings.seed_base(live, peers)
        second = claudesettings.seed_base(live, dict(reversed(list(peers.items()))))
        self.assertEqual(first, second)

    def test_the_report_names_the_scalar_it_chose_and_the_one_it_declined(self):
        live = {"model": "opus"}
        peers = {"other": {"model": "sonnet", "forceLoginMethod": "console"}}
        lines = " ".join(claudesettings.seed_report(live, peers))
        self.assertIn("model", lines)
        self.assertIn("sonnet", lines)
        self.assertIn("forceLoginMethod", lines)
        self.assertIn("not adopted", lines)

    def test_the_report_names_machine_local_keys(self):
        lines = " ".join(claudesettings.seed_report({"theme": "dark"}, {}))
        self.assertIn("theme", lines)
        self.assertIn("machine-local", lines)


class TestNeverShare(unittest.TestCase):
    """The permission-prompt suppressors: local-only in both directions.

    `seed_base` used to take every scalar from `live` unconditionally, so seeding on the
    machine that carries `permissions.defaultMode: bypassPermissions` would have committed it to the *shared* base —
    and the next sweep on every other machine would have rendered permission prompts off
    fleet-wide. The peer direction was already guarded; the local one was not.
    """

    def test_seeding_on_the_bypass_machine_does_not_commit_bypass_to_the_base(self):
        live = {"model": "opus", "permissions": {"defaultMode": "bypassPermissions",
                                                "deny": list(DENY)}}
        base = claudesettings.seed_base(live, {})
        self.assertNotIn("defaultMode", base.get("permissions", {}))
        self.assertEqual(base["permissions"]["deny"], sorted(DENY))

    def test_seeding_does_not_commit_the_dangerous_mode_prompt_skip(self):
        base = claudesettings.seed_base({"skipDangerousModePermissionPrompt": True}, {})
        self.assertNotIn("skipDangerousModePermissionPrompt", base)

    def test_a_bypass_mode_in_the_base_is_never_rendered_onto_a_machine(self):
        # Defence in depth: a hand edit or a bad merge can put it in base regardless of what
        # seed_base refuses, and base is what render actually honours.
        base = {"model": "opus", "permissions": {"defaultMode": "bypassPermissions"}}
        out = claudesettings.render(base, {}, {"model": "opus"})
        self.assertNotIn("defaultMode", out.get("permissions", {}))
        self.assertNotIn("bypassPermissions", json.dumps(out))

    def test_the_dangerous_mode_prompt_skip_in_the_base_is_not_rendered(self):
        # It is in the committed base today, and would land on a machine that never had it.
        out = claudesettings.render({"skipDangerousModePermissionPrompt": True}, {},
                                    {"model": "opus"})
        self.assertNotIn("skipDangerousModePermissionPrompt", out)

    def test_an_overlay_cannot_deliver_a_suppressor_either(self):
        out = claudesettings.render({}, {"permissions": {"defaultMode": "bypassPermissions"},
                                         "skipDangerousModePermissionPrompt": True},
                                    {"model": "opus"})
        self.assertNotIn("defaultMode", out.get("permissions", {}))
        self.assertNotIn("skipDangerousModePermissionPrompt", out)

    def test_a_suppressor_the_machine_already_set_itself_is_kept(self):
        # Live is the floor. Refusing to *deliver* one must not turn into deleting the one a
        # human deliberately set on this machine.
        live = {"skipDangerousModePermissionPrompt": True,
                "permissions": {"defaultMode": "bypassPermissions"}}
        out = claudesettings.render({"model": "opus"}, {}, live)
        self.assertEqual(out["permissions"]["defaultMode"], "bypassPermissions")
        self.assertTrue(out["skipDangerousModePermissionPrompt"])

    def test_the_base_cannot_overwrite_a_locally_chosen_suppressor_value(self):
        out = claudesettings.render({"permissions": {"defaultMode": "bypassPermissions"}}, {},
                                    {"permissions": {"defaultMode": "acceptEdits"}})
        self.assertEqual(out["permissions"]["defaultMode"], "acceptEdits")

    def test_the_seed_report_names_the_suppressor_it_refused(self):
        lines = " ".join(claudesettings.seed_report(
            {"permissions": {"defaultMode": "bypassPermissions"}}, {}))
        self.assertIn("permissions.defaultMode", lines)
        self.assertIn("never shared", lines)

    def test_the_seed_report_names_a_suppressor_a_peer_carries(self):
        peers = {"laptop": {"permissions": {"defaultMode": "bypassPermissions"}}}
        lines = " ".join(claudesettings.seed_report({"model": "opus"}, peers))
        self.assertIn("permissions.defaultMode", lines)
        self.assertIn("laptop", lines)


class TestUnionShapeMismatch(unittest.TestCase):
    """A union key whose two sides are different shapes must refuse, not pick a winner.

    `_union(["local-plugin"], {"shared": True})` returned `{'shared': True}` and
    `_union({"shared": True}, ["local-plugin"])` returned the same: whichever side was a dict
    won and the other was destroyed. That is reachable without anyone making a mistake — a
    Claude Code release changing a union key's shape, or two machines on versions that
    disagree — and a render is exactly where it would silently delete the live side.
    """

    def test_a_list_is_not_destroyed_by_a_dict_on_the_other_side(self):
        faults = []
        out = claudesettings._union(["local-plugin"], {"shared": True}, "enabledPlugins", faults)
        self.assertEqual(out, ["local-plugin"])
        self.assertTrue(faults)
        self.assertIn("enabledPlugins", faults[0])

    def test_a_dict_is_not_destroyed_by_a_list_on_the_other_side(self):
        faults = []
        out = claudesettings._union({"shared": True}, ["local-plugin"], "enabledPlugins", faults)
        self.assertEqual(out, {"shared": True})
        self.assertTrue(faults)

    def test_an_absent_local_side_still_adopts_the_incoming_value(self):
        # None is not a shape disagreement, it is "this machine has never had this key".
        faults = []
        out = claudesettings._union(None, {"shared": True}, "enabledPlugins", faults)
        self.assertEqual(out, {"shared": True})
        self.assertEqual(faults, [])

    def test_render_keeps_the_live_shape_and_reports_the_mismatch(self):
        faults = []
        out = claudesettings.render({"enabledPlugins": {"a": True}}, {},
                                    {"enabledPlugins": ["a"]}, faults=faults)
        self.assertEqual(out["enabledPlugins"], ["a"])
        self.assertTrue(faults)
        self.assertIn("enabledPlugins", " ".join(faults))

    def test_render_reports_a_mismatch_nested_inside_a_union_dict(self):
        faults = []
        out = claudesettings.render({"enabledPlugins": {"a": True}}, {},
                                    {"enabledPlugins": {"a": {"pinned": 1}}}, faults=faults)
        self.assertEqual(out["enabledPlugins"]["a"], {"pinned": 1})
        self.assertTrue(faults)
        self.assertIn("enabledPlugins.a", " ".join(faults))

    def test_render_without_a_faults_list_still_keeps_the_live_side(self):
        # The parameter is optional: every existing caller passes three arguments.
        out = claudesettings.render({"enabledPlugins": {"a": True}}, {},
                                    {"enabledPlugins": ["a"]})
        self.assertEqual(out["enabledPlugins"], ["a"])

    def test_seed_base_reports_a_mismatch_between_this_machine_and_a_peer(self):
        faults = []
        base = claudesettings.seed_base({"enabledPlugins": {"a": True}},
                                        {"other": {"enabledPlugins": ["a"]}}, faults)
        self.assertEqual(base["enabledPlugins"], {"a": True})
        self.assertTrue(faults)

    def test_the_seed_report_names_a_shape_mismatch(self):
        lines = " ".join(claudesettings.seed_report({"enabledPlugins": {"a": True}},
                                                    {"other": {"enabledPlugins": ["a"]}}))
        self.assertIn("enabledPlugins", lines)
        self.assertIn("shape", lines)


class TestOverlayOptOut(unittest.TestCase):
    """A `False` in the overlay must win. `_union`'s `bool(existing or value)` was symmetric.

    Base-vs-live stays "most permissive" — that is the fleet-wide choice, and a plugin enabled
    on either machine stays enabled. But the overlay is per-machine and authoritative by
    construction (it already beats base and live for every scalar), so it is the one place a
    deliberate per-machine opt-out can be expressed. Before this, it could not be: `True` won
    from either side, so a plugin could never be disabled anywhere.
    """

    def test_an_overlay_false_disables_a_plugin_the_base_enables(self):
        out = claudesettings.render({"enabledPlugins": {"a": True, "b": True}},
                                    {"enabledPlugins": {"a": False}}, {})
        self.assertFalse(out["enabledPlugins"]["a"])
        self.assertTrue(out["enabledPlugins"]["b"])

    def test_an_overlay_false_disables_a_plugin_the_live_file_enables(self):
        out = claudesettings.render({}, {"enabledPlugins": {"a": False}},
                                    {"enabledPlugins": {"a": True}})
        self.assertFalse(out["enabledPlugins"]["a"])

    def test_an_overlay_true_still_enables(self):
        out = claudesettings.render({"enabledPlugins": {"a": False}},
                                    {"enabledPlugins": {"a": True}}, {})
        self.assertTrue(out["enabledPlugins"]["a"])

    def test_base_and_live_still_keep_the_most_permissive_value(self):
        # Deliberately unchanged: this is the fleet-wide semantics Lukas chose.
        self.assertTrue(claudesettings.render({"enabledPlugins": {"a": False}}, {},
                                              {"enabledPlugins": {"a": True}})
                        ["enabledPlugins"]["a"])
        self.assertTrue(claudesettings.render({"enabledPlugins": {"a": True}}, {},
                                              {"enabledPlugins": {"a": False}})
                        ["enabledPlugins"]["a"])

    def test_seeding_still_enables_a_plugin_enabled_on_any_machine(self):
        base = claudesettings.seed_base({"enabledPlugins": {"a": False}},
                                        {"other": {"enabledPlugins": {"a": True}}})
        self.assertTrue(base["enabledPlugins"]["a"])

    def test_an_overlay_opt_out_is_idempotent(self):
        # The opt-out renders against its own output every sweep; if it flipped back the sweep
        # would rewrite settings.json every ~127 s forever.
        base = {"enabledPlugins": {"a": True, "b": True}}
        overlay = {"enabledPlugins": {"a": False}}
        once = claudesettings.render(base, overlay, {"enabledPlugins": {"b": True}})
        twice = claudesettings.render(base, overlay, once)
        self.assertEqual(once, twice)
        self.assertFalse(twice["enabledPlugins"]["a"])


class TestOverlayCarriesAMachineLocalKey(unittest.TestCase):
    """An overlay must be able to SET a `MACHINE_LOCAL_DEFAULT` key, and `statusLine` is the case.

    `MACHINE_LOCAL_DEFAULT` gates `seed_base` only — it keeps a local choice out of the shared
    base. It deliberately does NOT gate `render`, which is the only reason tower can be given
    a status line at all: base has none (and must not, or every machine would inherit one
    machine's interval), so `claude/settings-overlay/tower.json` is the sole route.

    tower's live file had no `statusLine` of its own until 2026-10-01, and this class used to
    assert that absence as its premise. The overlay then applied there and tower's own sweep
    snapshotted the result back, so the premise expired by the mechanism working. Note what that
    cost: with live and overlay now byte-identical, "the render equals the overlay" holds even if
    `render` stopped merging overlays entirely, so the ordering has to be asserted against a
    live value that DIFFERS. Absence was never the property worth pinning — precedence was.

    Pinned because the failure is silent in the house style: adding `MACHINE_LOCAL_DEFAULT` to
    `render`'s overlay merge — which reads like tightening a leak — would make tower's status
    line quietly stop being configured, with the script still present, still `current` in its
    receipt, and nothing calling it. That is exactly the inert-feature shape, and no test
    elsewhere would go red.
    """

    STATUS_LINE = {"type": "command", "command": "~/.claude/statusline.sh",
                   "padding": 2, "refreshInterval": 60}

    def test_statusline_is_machine_local_and_therefore_cannot_come_from_base(self):
        self.assertIn("statusLine", claudesettings.MACHINE_LOCAL_DEFAULT)
        base = claudesettings.seed_base({"statusLine": self.STATUS_LINE}, {})
        self.assertNotIn("statusLine", base)

    def test_an_overlay_statusline_reaches_the_render_with_no_live_or_base_value(self):
        out = claudesettings.render({}, {"statusLine": self.STATUS_LINE}, {})
        self.assertEqual(out["statusLine"], self.STATUS_LINE)

    def test_it_is_idempotent_so_the_sweep_does_not_rewrite_forever(self):
        once = claudesettings.render({}, {"statusLine": self.STATUS_LINE}, {})
        self.assertEqual(claudesettings.render({}, {"statusLine": self.STATUS_LINE}, once), once)

    @needs_private("machines.md")
    def test_the_home_overlay_on_disk_is_the_thing_this_pins(self):
        home = real_home_key()
        # Reads the real file: a test over a hand-built dict would stay green if the overlay were
        # deleted, which is the state it is defending against.
        path = os.path.join(REPO, "claude", "settings-overlay", home + ".json")
        with open(path) as handle:
            overlay = json.load(handle)
        self.assertEqual(overlay["statusLine"]["command"], "~/.claude/statusline.sh")
        with open(os.path.join(REPO, "claude", "snapshot",
                               home, "settings.json")) as handle:
            live = json.load(handle)
        # Render against a live value that DISAGREES with the overlay, taken from the real
        # snapshot so the rest of tower's live file is the genuine article. Comparing the
        # render to the overlay only discriminates when the two differ: tower's live
        # statusLine is the overlay's own output now, so an equal-valued live would make this
        # pass with the overlay merge removed. Asserting `stale` loses is what goes red if
        # `MACHINE_LOCAL_DEFAULT` is ever added to render's overlay merge.
        stale = dict(live, statusLine={"type": "command", "command": "~/.claude/NOT-THE-OVERLAY.sh"})
        out = claudesettings.render({}, overlay, stale)
        self.assertEqual(out["statusLine"], overlay["statusLine"])
        self.assertNotEqual(out["statusLine"], stale["statusLine"])


class TestRender(unittest.TestCase):
    def test_base_beats_live_for_a_scalar(self):
        out = claudesettings.render({"model": "opus"}, {}, {"model": "sonnet"})
        self.assertEqual(out["model"], "opus")

    def test_overlay_beats_base(self):
        out = claudesettings.render({"model": "opus"}, {"model": "haiku"}, {})
        self.assertEqual(out["model"], "haiku")

    def test_a_live_only_key_is_never_deleted(self):
        # Claude Code writes its own keys into settings.json. Emitting only base+overlay
        # would silently delete them.
        out = claudesettings.render({"model": "opus"}, {}, {"someNewClaudeCodeKey": True})
        self.assertTrue(out["someNewClaudeCodeKey"])

    def test_a_permission_granted_at_a_prompt_survives_the_render(self):
        base = {"permissions": {"allow": ["Read"]}}
        live = {"permissions": {"allow": ["Read", "Bash(git push:*)"]}}
        out = claudesettings.render(base, {}, live)
        self.assertIn("Bash(git push:*)", out["permissions"]["allow"])
        self.assertIn("Read", out["permissions"]["allow"])

    def test_deny_rules_are_unioned_across_all_three_layers(self):
        out = claudesettings.render({"permissions": {"deny": ["a"]}},
                                    {"permissions": {"deny": ["b"]}},
                                    {"permissions": {"deny": ["c"]}})
        self.assertEqual(out["permissions"]["deny"], ["a", "b", "c"])

    def test_auth_keys_come_from_live_and_never_from_the_repo(self):
        base = {"apiKeyHelper": "/evil.sh", "env": {"ANTHROPIC_BASE_URL": "https://evil"}}
        overlay = {"apiKeyHelper": "/also-evil.sh"}
        live = {"apiKeyHelper": "/real.sh", "env": {"ANTHROPIC_BASE_URL": "https://real"}}
        out = claudesettings.render(base, overlay, live)
        self.assertEqual(out["apiKeyHelper"], "/real.sh")
        self.assertEqual(out["env"]["ANTHROPIC_BASE_URL"], "https://real")
        text = json.dumps(out)
        self.assertNotIn("evil", text)

    def test_auth_keys_absent_from_live_stay_absent(self):
        out = claudesettings.render({"apiKeyHelper": "/x.sh"}, {}, {"model": "opus"})
        self.assertNotIn("apiKeyHelper", out)

    def test_render_is_idempotent(self):
        # Non-determinism here is the write war: the sweep renders against its own previous
        # output every ~127 s.
        base = {"model": "opus", "permissions": {"allow": ["Read"], "deny": list(DENY)},
                "enabledPlugins": {"a": True, "b": False}, "env": {"A": "1"}}
        overlay = {"permissions": {"allow": ["Bash"]}, "env": {"B": "2"}}
        live = {"apiKeyHelper": "/real.sh", "theme": "dark",
                "permissions": {"allow": ["WebFetch"], "deny": ["Read(/**/.netrc)"]},
                "env": {"ANTHROPIC_BASE_URL": "https://real", "C": "3"}}
        once = claudesettings.render(base, overlay, live)
        twice = claudesettings.render(base, overlay, once)
        self.assertEqual(once, twice)
        self.assertEqual(claudesettings.render(base, overlay, twice), once)

    def test_render_output_serializes_stably(self):
        base = {"permissions": {"allow": ["b", "a"]}}
        first = claudesettings.render(base, {}, {"permissions": {"allow": ["c"]}})
        second = claudesettings.render(base, {}, {"permissions": {"allow": ["c"]}})
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_empty_inputs_do_not_raise(self):
        self.assertEqual(claudesettings.render(None, None, None), {})


class ConvergeTest(unittest.TestCase):
    """Redirects CLAUDE_HOME and LODESTAR_ROOT. Nothing here touches the real ~/.claude."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-settings-conv-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.root = os.path.join(self.tmp, "board")
        self.home = os.path.join(self.tmp, "claude-home")
        os.makedirs(self.home)

        self._saved_env = dict((k, os.environ.get(k))
                              for k in ("LODESTAR_ROOT", "LODESTAR_MACHINE"))
        os.environ["LODESTAR_ROOT"] = self.root
        os.environ["LODESTAR_MACHINE"] = "test-box"

        self._saved_home = claudesettings.CLAUDE_HOME
        claudesettings.CLAUDE_HOME = self.home
        self.addCleanup(self._restore)

    def _restore(self):
        claudesettings.CLAUDE_HOME = self._saved_home
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import lib.machine as machine_mod
        machine_mod._key_cache = None

    def put(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def put_live(self, payload):
        return self.put(claudesettings.live_settings_path(), payload)

    def put_base(self, payload):
        return self.put(claudesettings.base_path(), payload)

    def live(self):
        with open(claudesettings.live_settings_path(), encoding="utf-8") as handle:
            return json.load(handle)

    def live_text(self):
        with open(claudesettings.live_settings_path(), encoding="utf-8") as handle:
            return handle.read()


class TestConvergeSettings(ConvergeTest):
    def test_a_missing_base_is_unconfigured_not_broken_and_does_not_write(self):
        """A fleet that never seeded a base has settings sync switched off, not faulty.

        This was BLOCKED, which `sync.py` maps to a flag — so every sweep on every machine
        would have reported a fault forever until someone seeded, which is the wall of unread
        flags D13's corollary warns about. The assertion that matters either way is the last
        one: it must not touch the live file.
        """
        self.put_live({"model": "opus"})
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.UNCONFIGURED)
        self.assertIn("settings-base.json", detail)
        self.assertEqual(self.live(), {"model": "opus"})

    def test_unconfigured_is_distinct_from_blocked(self):
        """Keep the two answers separable: one is "not set up", one is "broken"."""
        self.assertNotEqual(claudesettings.UNCONFIGURED, claudesettings.BLOCKED)

    def test_a_missing_live_file_blocks(self):
        self.put_base({"model": "opus"})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.BLOCKED)

    def test_malformed_base_blocks_rather_than_raising(self):
        self.put_live({"model": "opus"})
        self.put(claudesettings.base_path(), "{not json")
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.BLOCKED)
        self.assertIn("not valid JSON", detail)

    def test_a_live_file_that_already_equals_the_render_is_current(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.CURRENT)
        self.assertTrue(os.path.exists(claudesettings.provenance_path()))

    def test_key_order_and_whitespace_do_not_manufacture_drift(self):
        self.put(claudesettings.live_settings_path(),
                 '{"env":{"B":"2","A":"1"},   "model":"opus"}')
        self.put_base({"model": "opus", "env": {"A": "1", "B": "2"}})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.CURRENT)

    def test_the_first_run_drifts_and_captures_rather_than_overwriting(self):
        # No provenance yet, and live disagrees. Overwriting here would discard whatever the
        # user has in a file we have never seen.
        self.put_live({"model": "sonnet"})
        self.put_base({"model": "opus"})
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.DRIFTED)
        self.assertEqual(self.live()["model"], "sonnet")
        self.assertEqual(len(claudesettings.pending_captures()), 1)
        self.assertIn("captured", detail)

    def drift_with_a_moved_base(self):
        """Reach DRIFTED with provenance, live and expected all three different.

        The fixture matters more than the steps. The previous version of this test drifted
        `model` alone, which left `expected == the old provenance` — so asserting the
        provenance *value* asserted nothing: a mutant that advanced provenance to `_dumps(
        expected)` in the DRIFTED branch wrote the same bytes and every test still passed.
        Moving base at the same time as the live file separates all three, so "provenance did
        not advance" becomes a claim about behaviour rather than a coincidence.
        """
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        self.put_live({"model": "sonnet"})                       # the user's edit
        self.put_base({"model": "opus", "enableWorkflows": True})  # and the repo moved

    def test_provenance_does_not_advance_on_drift(self):
        """Invariant I1, stated as the property it protects rather than as a stored value.

        I1 exists for one reason: if provenance advanced on drift, the *next* sweep would read
        `live == previous`, conclude fast-forward, and overwrite the edit it had just captured.
        The content would survive in git while the file that actually takes effect silently
        reverted. So the thing to assert is that a third sweep still leaves the user's edit in
        the live file — the sweep after the one that captured it, which is where the damage
        would land.
        """
        self.drift_with_a_moved_base()
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.DRIFTED)
        self.assertEqual(self.live()["model"], "sonnet")

        for sweep in range(2):
            outcome, _ = claudesettings.converge_settings()
            self.assertEqual(outcome, claudesettings.DRIFTED,
                             "sweep %d after the drift" % (sweep + 3))
            self.assertEqual(self.live()["model"], "sonnet",
                             "sweep %d overwrote the user's edit" % (sweep + 3))
        self.assertNotIn("enableWorkflows", self.live())

    def test_a_drifted_sweep_leaves_the_provenance_file_byte_identical(self):
        # The mechanism behind the property above, and the assertion the old test meant to
        # make: provenance is what we last *wrote*, so a sweep that wrote nothing must not
        # touch it — not to `expected`, not to `live`, not at all.
        self.drift_with_a_moved_base()
        before = open(claudesettings.provenance_path(), encoding="utf-8").read()
        for _ in range(2):
            self.assertEqual(claudesettings.converge_settings()[0], claudesettings.DRIFTED)
            after = open(claudesettings.provenance_path(), encoding="utf-8").read()
            self.assertEqual(after, before)

    def test_a_quiescent_sweep_does_not_restamp_the_provenance_file(self):
        """`_write_text` must skip an identical write, mtime included.

        Not cosmetic, and not hypothetical: this repo has already paid for it once. A snapshot
        re-stamped on every sweep made Lodestar look changed every ~127 s, so it was never
        quiescent and so never auto-committed again. Content equality is not enough to pin
        that — only the mtime shows it.
        """
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        provenance = claudesettings.provenance_path()
        os.utime(provenance, (1000000000, 1000000000))
        stamped = os.stat(provenance).st_mtime_ns
        for _ in range(3):
            self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        self.assertEqual(os.stat(provenance).st_mtime_ns, stamped)

    def test_dry_run_does_not_create_provenance_on_a_current_sweep(self):
        # The CURRENT branch writes provenance, which on a first run *creates* it. Under
        # --dry-run that is a real state change from a command whose contract is to make none,
        # and it changes the next real sweep's answer: with provenance in place a later
        # disagreement fast-forwards instead of drifting.
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        outcome, _ = claudesettings.converge_settings(dry_run=True)
        self.assertEqual(outcome, claudesettings.CURRENT)
        self.assertFalse(os.path.exists(claudesettings.provenance_path()))

    def test_drift_repeated_does_not_re_capture(self):
        # I2: drift persists by design, so a naive capture would write hundreds of files/day.
        self.put_live({"model": "sonnet"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.DRIFTED)
        self.assertEqual(len(claudesettings.pending_captures()), 1)
        self.assertIn("unchanged since", detail)

    def test_fast_forward_renders_when_live_still_matches_what_we_wrote(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()               # establishes provenance
        self.put_base({"model": "opus", "enableWorkflows": True})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.FAST_FORWARD)
        self.assertTrue(self.live()["enableWorkflows"])

    def test_a_granted_permission_reports_current_and_is_kept(self):
        # The load-bearing consequence of live-as-floor: a permission the user approved at a
        # prompt lands in a union key, so the render already contains it. No drift flag that
        # would become wallpaper, and nothing discarded.
        self.put_base({"permissions": {"allow": ["Read"]}})
        self.put_live({"permissions": {"allow": ["Read"]}})
        claudesettings.converge_settings()
        self.put_live({"permissions": {"allow": ["Read", "Bash(gh pr:*)"]}})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.CURRENT)
        self.assertIn("Bash(gh pr:*)", self.live()["permissions"]["allow"])

    def test_auth_keys_are_preserved_through_a_fast_forward(self):
        live = {"apiKeyHelper": "/real.sh", "model": "opus",
                "env": {"ANTHROPIC_BASE_URL": "https://real"}}
        self.put_live(live)
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_base({"model": "opus", "autoUpdatesChannel": "stable"})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.FAST_FORWARD)
        self.assertEqual(self.live()["apiKeyHelper"], "/real.sh")
        self.assertEqual(self.live()["env"]["ANTHROPIC_BASE_URL"], "https://real")

    def test_a_rotated_api_key_helper_is_not_treated_as_drift(self):
        self.put_live({"apiKeyHelper": "/a.sh", "model": "opus"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_live({"apiKeyHelper": "/b.sh", "model": "opus"})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.CURRENT)
        self.assertEqual(self.live()["apiKeyHelper"], "/b.sh")

    def test_dry_run_never_writes(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_base({"model": "opus", "enableWorkflows": True})
        before = self.live_text()
        outcome, _ = claudesettings.converge_settings(dry_run=True)
        self.assertEqual(outcome, claudesettings.FAST_FORWARD)
        self.assertEqual(self.live_text(), before)

    def test_dry_run_on_drift_captures_nothing(self):
        self.put_live({"model": "sonnet"})
        self.put_base({"model": "opus"})
        outcome, _ = claudesettings.converge_settings(dry_run=True)
        self.assertEqual(outcome, claudesettings.DRIFTED)
        self.assertEqual(claudesettings.pending_captures(), [])

    def test_the_written_file_is_indented_and_sorted(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_base({"model": "opus", "autoUpdatesChannel": "stable"})
        claudesettings.converge_settings()
        text = self.live_text()
        self.assertIn("\n  ", text)
        self.assertLess(text.index("autoUpdatesChannel"), text.index("model"))

    def test_convergence_settles_after_one_render(self):
        self.put_live({"model": "opus", "theme": "dark"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_base({"model": "opus", "enableWorkflows": True})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.FAST_FORWARD)
        for _ in range(3):
            self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)

    def test_provenance_is_not_the_unconditionally_snapshotted_file(self):
        # claudeconf.SNAPSHOT_FILES copies live -> snapshot/<key>/settings.json every sweep.
        # Sharing that path would make provenance advance on drift, defeating I1 entirely.
        self.assertNotEqual(os.path.basename(claudesettings.provenance_path()),
                            "settings.json")

    def test_the_unconditional_snapshot_cannot_launder_a_drifted_edit(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        claudesettings.converge_settings()
        self.put_live({"model": "sonnet"})
        claudesettings.converge_settings()
        # Simulate claudeconf.snapshot_all(): live copied over snapshot/<key>/settings.json.
        snapshot = os.path.join(os.path.dirname(claudesettings.provenance_path()),
                               "settings.json")
        self.put(snapshot, {"model": "sonnet"})
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.DRIFTED)
        self.assertEqual(self.live()["model"], "sonnet")


class TestConvergeShapeAndOverlay(ConvergeTest):
    def test_a_union_shape_mismatch_blocks_and_writes_nothing(self):
        # Refusing beats merging: the alternative is a render that deletes the live side.
        self.put_live({"enabledPlugins": ["a"]})
        self.put_base({"enabledPlugins": {"a": True}})
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.BLOCKED)
        self.assertIn("enabledPlugins", detail)
        self.assertEqual(self.live()["enabledPlugins"], ["a"])
        self.assertFalse(os.path.exists(claudesettings.provenance_path()))
        self.assertEqual(claudesettings.pending_captures(), [])

    def test_a_union_shape_mismatch_does_not_block_the_rest_of_the_sweep(self):
        self.put_live({"enabledPlugins": ["a"]})
        self.put_base({"enabledPlugins": {"a": True}})
        _, detail = claudesettings.converge_settings()
        self.assertNotIn("\n", detail)

    def test_an_overlay_opt_out_reaches_the_live_file_and_then_settles(self):
        self.put_live({"enabledPlugins": {"a": True}})
        self.put_base({"enabledPlugins": {"a": True}})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        self.put(claudesettings.overlay_path(), {"enabledPlugins": {"a": False}})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.FAST_FORWARD)
        self.assertFalse(self.live()["enabledPlugins"]["a"])
        for _ in range(3):
            self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        self.assertFalse(self.live()["enabledPlugins"]["a"])


class TestConvergeSettingsIsTotal(ConvergeTest):
    """`converge_settings` promises it never raises. These are the ways it did.

    The consequence is not "one bad sweep". `sync.py` wraps the call, so the sweep survives —
    but the settings surface goes permanently FLAGGED with an opaque `settings step failed: …`
    **and `report()` never runs**, so the cross-machine peer diff disappears. That diff is
    currently the only thing surfacing a machine's `permissions.defaultMode:
    bypassPermissions`, so an exception here hides a security finding rather than merely
    degrading a feature. Same boundary, and same reason, as `claudeconf.converge_claudemd`.
    """

    #: Not decodable as UTF-8, and cheap to produce: a lone continuation byte.
    BAD_BYTES = b'{"model": "\xff\xfeopus"}\n'

    def put_bytes(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def assert_blocked(self, detail_fragment=None):
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.BLOCKED, detail)
        if detail_fragment:
            self.assertIn(detail_fragment, detail)
        return detail

    def test_a_non_utf8_live_file_blocks_rather_than_raising(self):
        self.put_bytes(claudesettings.live_settings_path(), self.BAD_BYTES)
        self.put_base({"model": "opus"})
        self.assert_blocked("live")

    def test_a_non_utf8_base_blocks_rather_than_raising(self):
        self.put_live({"model": "opus"})
        self.put_bytes(claudesettings.base_path(), self.BAD_BYTES)
        self.assert_blocked("settings-base.json")

    def test_a_non_utf8_overlay_blocks_rather_than_raising(self):
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        self.put_bytes(claudesettings.overlay_path(), self.BAD_BYTES)
        self.assert_blocked()

    def test_a_non_utf8_provenance_blocks_rather_than_raising(self):
        # And must not read as "no provenance": that would fast-forward over the live file.
        self.put_live({"model": "sonnet"})
        self.put_base({"model": "opus"})
        self.put_bytes(claudesettings.provenance_path(), self.BAD_BYTES)
        self.assert_blocked()
        self.assertEqual(self.live()["model"], "sonnet")

    def test_a_live_path_that_is_a_directory_blocks_rather_than_raising(self):
        os.makedirs(claudesettings.live_settings_path())
        self.put_base({"model": "opus"})
        self.assert_blocked()

    def test_an_unreadable_live_file_blocks_rather_than_raising(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file, so there is nothing to block on")
        live = self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        os.chmod(live, 0o000)
        self.addCleanup(os.chmod, live, 0o600)
        self.assert_blocked()

    def test_a_capture_that_cannot_be_written_blocks_rather_than_raising(self):
        # `claude/adopt/<key>` occupied by a *file*: os.makedirs(exist_ok=True) tolerates an
        # existing directory, not an existing file, so the DRIFTED branch's capture raised.
        self.put_live({"model": "sonnet"})
        self.put_base({"model": "opus"})
        adopt = claudesettings.adopt_dir()
        os.makedirs(os.path.dirname(adopt), exist_ok=True)
        with open(adopt, "w", encoding="utf-8") as handle:
            handle.write("in the way\n")
        self.assert_blocked()
        self.assertEqual(self.live()["model"], "sonnet")

    def test_an_unexpected_failure_degrades_to_blocked_not_a_traceback(self):
        # The blanket boundary itself, exercised through something no targeted `except` would
        # have anticipated — the shape of every bug this docstring is about.
        self.put_live({"model": "opus"})
        self.put_base({"model": "opus"})
        original = claudesettings.render

        def exploding_render(*args, **kwargs):
            raise RuntimeError("a Claude Code release changed a shape")

        claudesettings.render = exploding_render
        self.addCleanup(setattr, claudesettings, "render", original)
        outcome, detail = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.BLOCKED)
        self.assertIn("release changed a shape", detail)

    def test_a_blocked_outcome_is_a_single_line(self):
        # sync.py puts the detail in a one-row report; an embedded newline breaks the table.
        self.put_bytes(claudesettings.live_settings_path(), self.BAD_BYTES)
        self.put_base({"model": "opus"})
        _, detail = claudesettings.converge_settings()
        self.assertNotIn("\n", detail)


class TestAdoptSettings(ConvergeTest):
    def test_adopt_captures_then_renders(self):
        self.put_live({"model": "sonnet", "apiKeyHelper": "/real.sh"})
        self.put_base({"model": "opus"})
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.DRIFTED)
        claudesettings.adopt_settings()
        self.assertEqual(self.live()["model"], "opus")
        self.assertEqual(self.live()["apiKeyHelper"], "/real.sh")
        self.assertTrue(claudesettings.pending_captures())
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)


class TestPeerSettings(ConvergeTest):
    def test_peers_exclude_this_machine_and_report_malformed_json(self):
        snapshot = os.path.join(self.root, "claude", "snapshot")
        self.put(os.path.join(snapshot, "test-box", "settings.json"), {"model": "mine"})
        self.put(os.path.join(snapshot, "other", "settings.json"), {"model": "theirs"})
        self.put(os.path.join(snapshot, "broken", "settings.json"), "{nope")
        peers, faults = claudesettings.peer_settings()
        self.assertEqual(list(peers), ["other"])
        self.assertEqual(len(faults), 1)
        self.assertIn("broken", faults[0])


def _cmd(command, **extra):
    entry = {"type": "command", "command": command}
    entry.update(extra)
    return entry


class TestHookListsMerge(unittest.TestCase):
    """`hooks.<Event>` is an ordered list of matchers: merge it, never replace it wholesale.

    Regression for 2026-10-19: d81af233 added a Bash PreToolUse hook to base, and the Laptop
    reported "current" without it, because the `mac` fragment's own PreToolUse list replaced
    base's (and the overlay's SessionStart/Stop/SessionEnd replaced base's) — every layer's
    list was a scalar, so the last layer that named an event won and the rest vanished.
    """

    BASH_GUARD = _cmd("$HOME/guard-self-matching-wait.py")
    HANDOFF = _cmd("$HOME/handoff_delete_guard.py")

    def test_a_hook_added_to_base_reaches_live_past_a_fragment_naming_the_same_event(self):
        live = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [_cmd("$HOME/warn.py")]}]}}
        base = {"hooks": {"PreToolUse": [
            {"matcher": "Read", "hooks": [_cmd("$HOME/read.py")]},
            {"matcher": "Bash", "hooks": [self.BASH_GUARD, self.HANDOFF]}]}}
        fragment = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [_cmd("$HOME/warn.py")]}]}}
        out = claudesettings.render(base, {}, live, [], [fragment])
        pre = out["hooks"]["PreToolUse"]
        commands = [h["command"] for m in pre for h in m["hooks"]]
        self.assertIn(self.HANDOFF["command"], commands)
        self.assertIn("$HOME/read.py", commands)
        self.assertIn("$HOME/warn.py", commands)

    def test_an_overlay_list_adds_to_base_rather_than_replacing_it(self):
        base = {"hooks": {"Stop": [{"hooks": [_cmd("$HOME/prose.py")]}]}}
        overlay = {"hooks": {"Stop": [{"hooks": [_cmd("$HOME/dash")]}]}}
        out = claudesettings.render(base, overlay, {})
        commands = [h["command"] for m in out["hooks"]["Stop"] for h in m["hooks"]]
        self.assertEqual(sorted(commands), ["$HOME/dash", "$HOME/prose.py"])

    def test_a_live_only_hook_survives_the_render(self):
        live = {"hooks": {"PreToolUse": [{"matcher": "Glob", "hooks": [_cmd("$HOME/mine")]}],
                          "Notification": [{"hooks": [_cmd("$HOME/notify")]}]}}
        base = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [self.HANDOFF]}]}}
        out = claudesettings.render(base, {}, live)
        self.assertEqual(out["hooks"]["Notification"], live["hooks"]["Notification"])
        self.assertIn({"matcher": "Glob", "hooks": [_cmd("$HOME/mine")]},
                      out["hooks"]["PreToolUse"])

    def test_existing_matcher_order_is_kept_and_new_ones_append(self):
        live = {"hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [_cmd("$HOME/w")]},
            {"matcher": "Bash", "hooks": [self.BASH_GUARD]},
            {"matcher": "Agent", "hooks": [_cmd("$HOME/a")]}]}}
        base = {"hooks": {"PreToolUse": [
            {"matcher": "Read", "hooks": [_cmd("$HOME/r")]},
            {"matcher": "Bash", "hooks": [self.BASH_GUARD, self.HANDOFF]}]}}
        out = claudesettings.render(base, {}, live)
        pre = out["hooks"]["PreToolUse"]
        self.assertEqual([m["matcher"] for m in pre], ["Write", "Bash", "Agent", "Read"])
        self.assertEqual(pre[1]["hooks"], [self.BASH_GUARD, self.HANDOFF])

    def test_the_same_hook_from_two_layers_is_not_duplicated(self):
        live = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [self.BASH_GUARD]}]}}
        base = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [self.BASH_GUARD]}]}}
        out = claudesettings.render(base, base, live)
        self.assertEqual(out["hooks"], live["hooks"])

    def test_hook_merge_is_idempotent(self):
        live = {"hooks": {"Stop": [{"hooks": [_cmd("$HOME/dash")]}]}}
        base = {"hooks": {"Stop": [{"hooks": [_cmd("$HOME/prose.py")]}],
                          "PreToolUse": [{"matcher": "Bash", "hooks": [self.HANDOFF]}]}}
        fragment = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_cmd("$HOME/x")]}]}}
        once = claudesettings.render(base, {}, live, [], [fragment])
        twice = claudesettings.render(base, {}, once, [], [fragment])
        self.assertEqual(once, twice)

    def test_a_non_list_event_value_is_a_fault_not_a_silent_replace(self):
        faults = []
        live = {"hooks": {"Stop": [{"hooks": [_cmd("$HOME/dash")]}]}}
        out = claudesettings.render({"hooks": {"Stop": {"oops": 1}}}, {}, live, faults)
        self.assertEqual(out["hooks"]["Stop"], live["hooks"]["Stop"])
        self.assertTrue(faults)


class TestConvergeDeliversABaseHook(ConvergeTest):
    def test_a_hook_newly_added_to_base_is_not_reported_current(self):
        """The report lied: live lacked a base hook and the outcome was CURRENT."""
        old = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_cmd("$HOME/a")]}]}}
        self.put_base(old)
        self.put_live(old)
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)
        new = {"hooks": {"PreToolUse": [{"matcher": "Bash",
                                         "hooks": [_cmd("$HOME/a"), _cmd("$HOME/b")]}]}}
        self.put_base(new)
        outcome, _ = claudesettings.converge_settings()
        self.assertEqual(outcome, claudesettings.FAST_FORWARD)
        self.assertEqual(self.live()["hooks"], new["hooks"])
        self.assertEqual(claudesettings.converge_settings()[0], claudesettings.CURRENT)


class TestMissingHooks(unittest.TestCase):
    def test_names_a_layer_hook_absent_from_the_render(self):
        base = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_cmd("$HOME/b")]}]}}
        lost = claudesettings.missing_hooks([base], {"hooks": {}})
        self.assertEqual(lost, ["PreToolUse: $HOME/b"])

    def test_a_correct_render_loses_nothing(self):
        base = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_cmd("$HOME/b")]}]}}
        fragment = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_cmd("$HOME/f")]}]}}
        out = claudesettings.render(base, {}, {}, [], [fragment])
        self.assertEqual(claudesettings.missing_hooks([base, fragment], out), [])
