# -*- coding: utf-8 -*-
"""docket doctor reads the OpenClaw config right and names the fix; it changes nothing."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOCKET_HOME", tempfile.mkdtemp(prefix="docket-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import doctor  # noqa: E402


class Doctor(unittest.TestCase):
    def openclaw(self, config, settings=None):
        with mock.patch.object(doctor.shutil, "which", lambda name: "/bin/" + name), \
                mock.patch.object(doctor, "openclaw_config", lambda: config):
            return doctor.openclaw(settings or {"pm_agent": "main"})

    def test_a_plugin_wired_by_the_installer_holds(self):
        config = {"plugins": {"allow": ["docket"], "load": {"paths": [str(doctor.PLUGIN)]},
                              "entries": {"docket": {"enabled": True, "hooks": {"allowConversationAccess": True}}}}}
        plugin, agent = self.openclaw(config)
        self.assertTrue(plugin.ok, plugin.detail)
        self.assertIsNone(agent.ok)  # PMs in the main agent: nothing to fix

    def test_each_missing_piece_is_named_with_the_fix(self):
        plugin, _ = self.openclaw({"plugins": {"allow": ["browser"]}})
        self.assertFalse(plugin.ok)
        for piece in ("not linked", "not enabled", "plugins.allow", "conversation access"):
            self.assertIn(piece, plugin.detail)
        self.assertIn("openclaw_setup.py", plugin.line())

    def test_the_pm_agent_must_exist_and_have_no_first_run_file(self):
        workspace = Path(tempfile.mkdtemp())
        (workspace / "AGENTS.md").write_text("contract")
        config = {"agents": {"entries": {"docket-pm": {"workspace": str(workspace)}}}}
        _, agent = self.openclaw(config, {"pm_agent": "docket-pm"})
        self.assertTrue(agent.ok)
        (workspace / "BOOTSTRAP.md").write_text("hi")
        _, agent = self.openclaw(config, {"pm_agent": "docket-pm"})
        self.assertFalse(agent.ok)
        _, agent = self.openclaw({}, {"pm_agent": "docket-pm"})
        self.assertFalse(agent.ok)

    def test_no_openclaw_is_one_failing_line(self):
        with mock.patch.object(doctor.shutil, "which", lambda name: None):
            [check] = doctor.openclaw({})
        self.assertFalse(check.ok)


if __name__ == "__main__":
    unittest.main()
