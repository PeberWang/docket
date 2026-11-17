# -*- coding: utf-8 -*-
"""Offline checks of the desk's decision paths (fake PM, fake OpenCode).

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DESK_HOME"] = tempfile.mkdtemp(prefix="desk-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import daemon, engines, ledger, pm  # noqa: E402

CARD = {"kind": "report", "progress": "p", "decisions": [], "decided_for_you": []}


class Flows(unittest.TestCase):
    def setUp(self):
        self.sent, self.answers = [], []
        sent = self.sent

        class FakeService:
            def prompt(self, sid, text):
                sent.append(text)

        daemon.Service = engines.Service = FakeService
        # Never start real agents from a test: every engine's follow-up is recorded instead.
        engines.OpenCode.resume = lambda engine, handoff, text, model=None: sent.append(text)
        engines.Cli.launch = lambda engine, handoff, prompt, first: (_ for _ in ()).throw(AssertionError("no CLI runs in tests"))
        engines.Cli.takeover = engines.OpenCode.takeover = lambda engine, handoff: None
        pm.ask = lambda key, message, validate: self.answers.pop(0)
        daemon.ledger.DB = Path(tempfile.mkdtemp()) / "desk.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.desk = daemon.Desk()
        self.desk.submit = lambda did, job, *args: job(*args)
        self.db = self.desk.db()
        ledger.add_delegation(self.db, id="ses_x", title="t", directory=None, source_key="k", dispatch_seq=1, dispatched_at=0, brief="", context=[])

    def awaiting_card(self):
        card_id = ledger.add_card(self.db, "ses_x", "report", CARD)
        ledger.set_state(self.db, "ses_x", "awaiting")
        return card_id

    def test_mention_relay_needs_confirmation(self):
        self.answers.append({"action": "relay", "message": "do more"})
        self.desk.act("ses_x", "m", "a chat message")
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(card["body"])["pending_relay"], "do more")
        self.assertEqual(self.sent, [])
        self.desk.decide(card["id"], {"relay": "yes"}, "")
        self.assertEqual(self.sent, ["do more"])
        self.assertEqual(self.desk.delegation("ses_x")["rounds"], 1)

    def test_mention_relay_declined_sends_nothing(self):
        self.answers.append({"action": "relay", "message": "do more"})
        self.desk.act("ses_x", "m", "a chat message")
        self.desk.decide(ledger.open_card(self.db, "ses_x")["id"], {"relay": "no"}, "")
        self.assertEqual(self.sent, [])

    def test_round_limit(self):
        for i in range(3):
            self.answers.append({"action": "relay", "message": f"r{i}"})
            self.desk.decide(self.awaiting_card(), {}, "go")
        self.assertEqual(self.desk.delegation("ses_x")["rounds"], 3)
        self.desk.decide(self.awaiting_card(), {}, "one more")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "limit")

    def test_card_and_close_actions(self):
        self.answers.append({"action": "card", "card": dict(CARD, kind="question")})
        self.desk.decide(self.awaiting_card(), {}, "先问一下")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "question")
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.answers.append({"action": "close", "conclusion": "办完了"})
        self.desk.decide(ledger.open_card(self.db, "ses_x")["id"], {}, "可以了")
        self.assertEqual(self.desk.delegation("ses_x")["state"], "closed")
        self.assertIn("办完了", next(daemon.MEMORY_DIR.glob("*.md")).read_text(encoding="utf-8"))

    def test_permission_request_becomes_a_card(self):
        answered = []
        engines.OpenCode.answer_permission = lambda engine, handoff, request, decision: answered.append((request, decision))
        self.desk.permission_card("ses_x", {"id": "per_1", "action": "external_directory", "resources": ["/tmp/*"]})
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(card["body"])["pending_permission"], "per_1")
        self.assertNotIn("pending_permission", ledger.snapshot(self.db)[0]["card"])  # never shown to the app
        self.desk.decide(card["id"], {"permission": "once"}, "")
        self.assertEqual(answered, [("per_1", "once")])
        self.assertEqual(self.desk.delegation("ses_x")["state"], "running")
        self.assertEqual(self.sent, [])  # answering a permission is not a round

    def test_usage_limit_retries_once_on_the_fallback(self):
        calls = []
        engines.OpenCode.resume = lambda engine, handoff, text, model=None: calls.append((text, model))
        import json as _json
        self.db.execute("insert into events (delegation_id, at, kind, detail) values (?, ?, 'relayed', ?)",
                        ("ses_x", ledger.now(), _json.dumps({"message": "do more"})))
        self.assertTrue(self.desk.retry_with_fallback("ses_x", "You've hit your session limit · resets 6:40pm"))
        self.assertEqual(calls, [("do more", self.desk.settings["fallback_model"])])
        self.assertFalse(self.desk.retry_with_fallback("ses_x", "session limit again"))  # only once
        self.assertFalse(self.desk.retry_with_fallback("ses_x", "some other failure"))

    def test_a_new_card_never_replaces_a_waiting_one(self):
        self.desk.show_card("ses_x", "question", dict(CARD, progress="first"))
        self.desk.show_card("ses_x", "report", dict(CARD, progress="second"))
        first = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(first["body"])["progress"], "first")
        queued = self.db.execute("select count(*) from cards where delegation_id = 'ses_x' and queued = 1").fetchone()[0]
        self.assertEqual(queued, 1)
        ledger.resolve_card(self.db, first["id"], {"reply": "answer one"})
        self.desk.advance("ses_x")
        second = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(second["body"])["progress"], "second")

    def test_card_validation(self):
        self.assertEqual(pm.validate_card(dict(CARD)), [])
        bad = {"kind": "x", "progress": "", "decisions": [{"id": "d1", "question": "q", "options": [{"id": "a", "label": "a"}]}]}
        self.assertEqual(len(pm.validate_card(bad)), 3)
        self.assertEqual(pm.extract_json('好\n```json\n{"action": "close", "conclusion": "ok"}\n```')["action"], "close")


if __name__ == "__main__":
    unittest.main()
