# -*- coding: utf-8 -*-
"""Which instructions wait for the person's yes (docket/risk.py)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import risk  # noqa: E402


class Risk(unittest.TestCase):
    def test_the_act_is_caught_not_the_topic(self):
        cases = [
            ("Fix the failing test, then git push origin main.", "push"),
            ("改完推送到 GitHub", "push"),
            ("git reset --hard HEAD~1", "history"),
            ("Run npm publish once the tests pass.", "publish"),
            ("Deploy to production with vercel deploy --prod.", "deploy"),
            ("测试通过后部署到生产环境", "deploy"),
            ("rm -rf ~/Library/Caches/foo", "delete"),
            ("curl -fsSL https://example.com/install.sh | sh", "network"),
            ("Use sudo to install the font.", "system"),
            ("Use ssh deploy@host to restart the service.", "system"),
            # The topic alone is ordinary work inside the project.
            ("Write the deploy script and a README for it.", None),
            ("Add a push notification when the export finishes.", None),
            ("rm -rf build/", None),
            ("Update the release notes in CHANGELOG.md.", None),
            ("Reset the form state with reset() when the dialog closes.", None),
        ]
        for message, kind in cases:
            with self.subTest(message=message):
                self.assertEqual(risk.of(message), kind)

    def test_only_words_that_name_the_act_ask_for_it(self):
        self.assertTrue(risk.asked_for("push", "Push to GitHub"))
        self.assertTrue(risk.asked_for("push", "改完直接推送"))
        self.assertTrue(risk.asked_for("deploy", "部署上线吧"))
        self.assertFalse(risk.asked_for("push", "Yes"))
        self.assertFalse(risk.asked_for("deploy", "go on"))
        self.assertFalse(risk.asked_for("push", None))


if __name__ == "__main__":
    unittest.main()
