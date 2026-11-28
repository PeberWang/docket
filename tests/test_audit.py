# -*- coding: utf-8 -*-
"""docket audit: a report from a copy of a ledger, read-only, every section present."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DOCKET_HOME"] = tempfile.mkdtemp(prefix="docket-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import audit, ledger  # noqa: E402


class Audit(unittest.TestCase):
    def test_a_report_from_a_small_ledger(self):
        path = Path(tempfile.mkdtemp()) / "docket.sqlite"
        db = ledger.connect(path)
        ledger.add_delegation(db, id="ses_a", title="list view", directory=None, source_key="agent:main:main",
                              dispatch_seq=1, dispatched_at=ledger.now(), brief="", context=[])
        ledger.log(db, "ses_a", "superseded", {"by": "ses_b"})
        ledger.log(db, "ses_a", "plugin:refused", {"agent": "main", "rule": "write-into-handoff", "command": "opencode run -s ses_a x"})
        ledger.log(db, "ses_a", "permission:pm-decided", {"decision": "once", "what": ["rm -rf build"], "reason": "build output"})
        ledger.set_state(db, "ses_a", "closed")
        db.close()
        text = audit.Audit(path).report(False, False)
        for heading in ("## 1. Overview", "## 2. How handoffs ended", "## 9. Ledger invariants", "## 11. OpenClaw plugin and permission classes"):
            self.assertIn(heading, text)
        self.assertIn("replaced by a newer handoff", text)
        self.assertIn("rm -rf build", text)


if __name__ == "__main__":
    unittest.main()
