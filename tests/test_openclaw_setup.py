# -*- coding: utf-8 -*-
"""scripts/openclaw_setup.py with a fake openclaw CLI: what it patches, backs up and never touches."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import openclaw_setup as setup_module  # noqa: E402


class FakeOpenClaw:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.calls = []

    def __call__(self, argv, stdin=None):
        self.calls.append((argv, stdin))
        if argv[:3] == ["openclaw", "config", "file"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.config_path}\n", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def ran(self, *prefix):
        return [stdin for argv, stdin in self.calls if argv[: len(prefix)] == list(prefix)]


class OpenClawSetup(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.data, self.home = root / "docket", root / ".openclaw"
        (self.home / "workspace/skills").mkdir(parents=True)
        self.data.mkdir()
        (self.data / "config.json").write_text(json.dumps({"locale": "zh"}), encoding="utf-8")
        self.config = self.home / "openclaw.json"
        self.config.write_text(json.dumps({"plugins": {"allow": ["browser"]}, "agents": {"entries": {"main": {}}}}), encoding="utf-8")
        self.cli = FakeOpenClaw(self.config)
        self.lines = []
        setup_module.PLUGIN = Path("/repo/integrations/openclaw/plugin")  # never a worktree in the test

    def setup(self, dry_run=False, **kw):
        return setup_module.Setup(self.data, self.home, dry_run, run=self.cli, out=self.lines.append, **kw)

    def test_the_patch_adds_the_plugin_to_the_allowlist_and_the_pm_agent(self):
        patch = self.setup().patch(json.loads(self.config.read_text()))
        self.assertEqual(patch["plugins"]["allow"], ["browser", "docket"])
        self.assertEqual(patch["plugins"]["load"], {"paths": [str(setup_module.PLUGIN)]})  # arrays replace: written whole
        kept = self.setup().patch({"plugins": {"load": {"paths": ["/other/plugin"]}}})
        self.assertEqual(kept["plugins"]["load"]["paths"], ["/other/plugin", str(setup_module.PLUGIN)])
        linked = self.setup().patch({"plugins": {"load": {"paths": [str(setup_module.PLUGIN)]}}})
        self.assertNotIn("load", linked["plugins"])
        self.assertTrue(patch["plugins"]["entries"]["docket"]["hooks"]["allowConversationAccess"])
        agent = patch["agents"]["entries"]["docket-pm"]
        self.assertEqual(agent["workspace"], str(self.data / "pm-workspace"))
        self.assertNotIn("//", agent)
        self.assertIn("sessions_send", agent["tools"]["deny"])
        self.assertEqual(agent["tools"]["agentToAgent"], {"send": []})
        # No allowlist set: none is written, so nothing else gets switched off.
        self.assertNotIn("allow", self.setup().patch({})["plugins"])
        self.assertEqual(set(self.setup(plugin=False).patch({})), {"agents"})

    def test_a_reinstall_keeps_the_persons_own_choices(self):
        config = {"plugins": {"entries": {"docket": {"enabled": False}}},
                  "agents": {"entries": {"docket-pm": {"tools": {"allow": ["read"]}}}}}
        patch = self.setup().patch(config)
        self.assertNotIn("enabled", patch["plugins"]["entries"]["docket"])
        self.assertNotIn("tools", patch["agents"]["entries"]["docket-pm"])

    def test_a_dry_run_changes_nothing_and_validates_the_patch(self):
        before = (self.data / "config.json").read_text()
        setup = self.setup(dry_run=True)
        config = setup.config()
        setup.install_plugin(config)
        setup.pm_workspace()
        setup.add_agent(config)
        setup.apply_patch(config)
        setup.docket_config()
        setup.skill()
        self.assertEqual((self.data / "config.json").read_text(), before)
        self.assertFalse((self.data / "pm-workspace").exists())
        self.assertFalse((self.home / "workspace/skills/desk-dispatch").exists())
        self.assertEqual(self.cli.ran("openclaw", "plugins"), [])
        self.assertEqual(self.cli.ran("openclaw", "agents"), [])
        [argv] = [argv for argv, _ in self.cli.calls if argv[:3] == ["openclaw", "config", "patch"]]
        self.assertIn("--dry-run", argv)

    def test_a_real_run_writes_backs_up_and_is_idempotent(self):
        setup = self.setup()
        config = setup.config()
        setup.install_plugin(config)
        setup.pm_workspace()
        setup.add_agent(config)
        (self.data / "pm-workspace/BOOTSTRAP.md").write_text("hello, who am I?")
        setup.no_bootstrap()
        setup.apply_patch(config)
        setup.docket_config()
        self.assertFalse((self.data / "pm-workspace/BOOTSTRAP.md").exists())
        self.assertTrue((self.data / "pm-workspace/AGENTS.md").exists())
        self.assertEqual(json.loads((self.data / "config.json").read_text()), {"locale": "zh", "pm_agent": "docket-pm"})
        self.assertEqual(len(list(self.data.glob("config.json.bak-*"))), 1)
        self.assertEqual(len(self.cli.ran("openclaw", "agents", "add")), 1)
        # A second run with the agent present changes no file.
        (self.data / "pm-workspace/AGENTS.md").write_text("edited by hand")
        config = {"agents": {"entries": {"docket-pm": {}}}, "plugins": {"load": {"paths": [str(setup_module.PLUGIN)]}}}
        again = self.setup()
        again.install_plugin(config)
        again.pm_workspace()
        again.add_agent(config)
        again.docket_config()
        self.assertEqual(len(self.cli.ran("openclaw", "agents", "add")), 1)
        self.assertEqual(self.cli.ran("openclaw", "plugins", "install"), [])  # the Gateway applies load.paths live
        self.assertEqual(len(list((self.data / "pm-workspace").glob("AGENTS.md.bak-*"))), 1)  # the edit is kept aside

    def test_a_skill_changed_by_hand_is_backed_up_and_one_installed_by_docket_is_not(self):
        target = self.home / "workspace/skills/desk-dispatch"
        target.mkdir()
        (target / "SKILL.md").write_text("my own notes")
        self.setup().skill()
        self.assertEqual(len(list(target.parent.glob("desk-dispatch.bak-*"))), 1)
        self.assertEqual(setup_module.tree_hash(target), setup_module.tree_hash(setup_module.SKILL))
        self.setup().skill()  # current: nothing
        self.assertIn("skill desk-dispatch is current", self.lines)
        self.assertEqual(len(list(target.parent.glob("desk-dispatch.bak-*"))), 1)

    def test_remove_unlinks_the_plugin_and_keeps_everything_else(self):
        config = {"plugins": {"allow": ["browser", "docket"], "load": {"paths": ["/other/plugin", str(setup_module.PLUGIN)]},
                              "entries": {"docket": {"enabled": True}, "browser": {"enabled": True}}},
                  "agents": {"entries": {"docket-pm": {}}}}
        patch = self.setup().removal(config)["plugins"]
        self.assertEqual(patch["allow"], ["browser"])
        self.assertEqual(patch["load"]["paths"], ["/other/plugin"])
        self.assertEqual(patch["entries"], {"docket": None})  # null deletes the entry; others untouched
        self.setup(dry_run=True).remove(config)
        self.assertTrue(any("openclaw agents delete docket-pm" in line for line in self.lines))

    def test_it_never_links_the_plugin_from_a_worktree(self):
        setup_module.PLUGIN = Path("/repo/.worktrees/x/integrations/openclaw/plugin")
        with self.assertRaises(RuntimeError):
            self.setup().install_plugin({})
        self.setup(dry_run=True).install_plugin({})  # a dry run may say what it would do


if __name__ == "__main__":
    unittest.main()
