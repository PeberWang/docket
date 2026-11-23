# -*- coding: utf-8 -*-
"""Offline checks of the menu-bar app's card dimming and single-instance setup.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
SOURCES = APP / "Sources" / "Docket"


def swift_block(path: Path, opener: str) -> str:
    """The source of one top-level declaration, from its opening line to the matching brace."""
    text = path.read_text(encoding="utf-8")
    start = text.index(opener)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
    raise AssertionError(f"unbalanced braces after {opener!r}")


class Dimming(unittest.TestCase):
    card = (SOURCES / "CardView.swift").read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("swift"), "no Swift toolchain")
    def test_taken_over_dims_the_content_but_not_the_actions(self):
        # Run the real CardDimming against the real Phase enum, outside SwiftUI.
        program = "\n\n".join([
            swift_block(SOURCES / "Models.swift", "enum Phase"),
            swift_block(SOURCES / "CardView.swift", "enum CardDimming"),
            'for p in [Phase.running, .pending, .awaiting, .limit, .takenOver, .closed, .unknown] {\n'
            '    print("\\(p.rawValue) \\(CardDimming.content(p)) \\(CardDimming.footer(p))")\n'
            '}\n',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "main.swift"
            source.write_text(program, encoding="utf-8")
            run = subprocess.run(["swift", str(source)], capture_output=True, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stderr)
        got = {name: (float(c), float(f)) for name, c, f in (line.split() for line in run.stdout.splitlines())}
        self.assertEqual(got["taken_over"], (0.55, 1.0))
        self.assertEqual(got["closed"][0], 0.55)
        for phase in ("running", "pending", "awaiting", "limit", "unknown"):
            self.assertEqual(got[phase], (1.0, 1.0), phase)
        for phase, (_, footer) in got.items():
            self.assertEqual(footer, 1.0, f"the action row must never dim ({phase})")

    def test_body_dims_only_the_content(self):
        body = swift_block(SOURCES / "CardView.swift", "var body: some View")
        self.assertIn("content\n                .opacity(CardDimming.content(phase))", body)
        self.assertIn("footer\n                .opacity(CardDimming.footer(phase))", body)
        # No opacity on the card as a whole: the only ones in body are the two above and the fill/stroke tints.
        self.assertEqual(len(re.findall(r"^\s*\.opacity\(", body, re.M)), 2)

    def test_actions_live_in_the_footer(self):
        footer = swift_block(SOURCES / "CardView.swift", "private var footer: some View")
        content = swift_block(SOURCES / "CardView.swift", "private var content: some View")
        self.assertIn("actionRow", footer)
        self.assertIn("SectionDivider()", footer)
        self.assertNotIn("actionRow", content)

    def test_hand_back_outweighs_open(self):
        row = swift_block(SOURCES / "CardView.swift", "private var actionRow: some View")
        taken = row[row.index("case .takenOver:"):row.index("default:")]
        self.assertRegex(taken, r"s\(\.handBack\)\) \{[^}]*\}\s*\.buttonStyle\(DocketButtonStyle\(prominent: true\)\)")
        self.assertRegex(taken, r"openInOpenCode\)\) \{[^}]*\}\s*\.buttonStyle\(DocketButtonStyle\(prominent: false\)\)")


class SingleInstance(unittest.TestCase):
    def info_plist(self) -> dict:
        script = (APP / "build.sh").read_text(encoding="utf-8")
        body = re.search(r"<<'PLIST'\n(.*?)\nPLIST\n", script, re.S).group(1)
        return plistlib.loads(body.encode("utf-8"))

    def test_info_plist_prohibits_a_second_instance(self):
        plist = self.info_plist()
        self.assertIs(plist.get("LSMultipleInstancesProhibited"), True)
        self.assertEqual(plist["CFBundleIdentifier"], "io.github.peberwang.docket")

    def test_launch_hands_over_to_a_running_copy(self):
        main = (SOURCES / "main.swift").read_text(encoding="utf-8")
        check = main.index("SingleInstance.otherInstance()")
        self.assertLess(check, main.index("NSApplication.shared"), "check before anything starts")
        self.assertIn("other.activate()", main)
        self.assertIn("exit(0)", main)
        finder = (SOURCES / "SingleInstance.swift").read_text(encoding="utf-8")
        self.assertIn("runningApplications(withBundleIdentifier:", finder)
        self.assertIn("processIdentifier != me", finder)


class Icon(unittest.TestCase):
    def test_build_ships_the_checked_in_icon(self):
        script = (APP / "build.sh").read_text(encoding="utf-8")
        self.assertIn('cp ../assets/Docket.icns "$APP/Contents/Resources/Docket.icns"', script)
        code = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
        self.assertFalse(any("make-icns" in line for line in code), "copy the icns, never re-render it")
        self.assertEqual(SingleInstance.info_plist(self)["CFBundleIconFile"], "Docket")
        self.assertTrue((APP.parent / "assets" / "Docket.icns").is_file())


if __name__ == "__main__":
    unittest.main()
