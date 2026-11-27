# -*- coding: utf-8 -*-
"""The OpenClaw plugin's own tests (node --test), run with the rest so one command checks everything."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "integrations/openclaw/plugin"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PluginJs(unittest.TestCase):
    def test_the_plugin_suite_passes(self):
        done = subprocess.run(["node", "--test", "test/"], cwd=PLUGIN, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
