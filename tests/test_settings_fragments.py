"""Label-scoped settings.json fragments (claude/settings-fragments/<label>.json)."""

import json
import os
import tempfile
import unittest

from lib import claudesettings


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class TestSettingsFragments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lodestar-frag-")

    def test_fragments_layer_between_base_and_overlay(self):
        base = {"theme": "dark", "env": {"A": "base"}}
        fragments = [{"env": {"A": "frag", "B": "frag"}}]
        overlay = {"env": {"B": "overlay"}}
        out = claudesettings.render(base, overlay, {}, fragments=fragments)
        self.assertEqual(out["env"], {"A": "frag", "B": "overlay"})
        self.assertEqual(out["theme"], "dark")

    def test_no_fragments_is_the_old_render(self):
        base = {"theme": "dark"}
        live = {"x": 1}
        self.assertEqual(claudesettings.render(base, {}, live),
                         claudesettings.render(base, {}, live, fragments=[]))

    def test_fragments_are_selected_by_label(self):
        root = os.path.join(self.tmp, "frags")
        _write(os.path.join(root, "mac.json"), json.dumps({"k": "mac"}))
        _write(os.path.join(root, "windows-wsl.json"), json.dumps({"k": "wsl"}))
        loaded, faults = claudesettings.load_fragments(["mac", "work"], root)
        self.assertEqual(faults, [])
        self.assertEqual([name for name, _ in loaded], ["mac"])

    def test_fragments_apply_in_declared_label_order(self):
        root = os.path.join(self.tmp, "frags")
        _write(os.path.join(root, "mac.json"), json.dumps({"k": "mac"}))
        _write(os.path.join(root, "work.json"), json.dumps({"k": "work"}))
        loaded, _ = claudesettings.load_fragments(["work", "mac"], root)
        self.assertEqual([name for name, _ in loaded], ["work", "mac"])

    def test_an_unreadable_fragment_is_a_fault_not_a_skip(self):
        root = os.path.join(self.tmp, "frags")
        _write(os.path.join(root, "mac.json"), "{not json")
        loaded, faults = claudesettings.load_fragments(["mac"], root)
        self.assertEqual(loaded, [])
        self.assertEqual(len(faults), 1)

    def test_never_sync_keys_in_a_fragment_never_reach_live(self):
        out = claudesettings.render({}, {}, {"apiKeyHelper": "live"},
                                    fragments=[{"apiKeyHelper": "leaked"}])
        self.assertEqual(out["apiKeyHelper"], "live")


if __name__ == "__main__":
    unittest.main()
