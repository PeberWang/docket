# -*- coding: utf-8 -*-
"""Docket's side of opencode-permission-notify: the heartbeat, the ledger it reads, the runs waiting
on the popup, and the PM reviewing what was allowed on its behalf."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["DOCKET_HOME"] = tempfile.mkdtemp(prefix="docket-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import daemon, engines, ledger, permissions, pm  # noqa: E402

CARD = {"kind": "report", "progress": "p", "decisions": [], "decided_for_you": []}


def line(session, tier, cls="4", effect="allow", **extra):
    return json.dumps({"v": 2, "ts": "2026-11-27T23:00:00Z", "session": session, "tool": "edit", "resources": ["src/app.py"],
                       "class": cls, "class_name": "项目内写入", "tier": tier, "effect": effect, **extra}) + "\n"


class Permissions(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.patch = {name: getattr(permissions, name) for name in ("HEARTBEAT", "LEDGER", "PENDING")}
        permissions.HEARTBEAT, permissions.LEDGER, permissions.PENDING = root / "pm-online", root / "ledger.jsonl", root / "pending"
        self.addCleanup(lambda: [setattr(permissions, k, v) for k, v in self.patch.items()])
        self.messages = []

        class FakeService:
            def prompt(self, sid, text):
                pass

            def last_reply(self, sid):
                return "done"

        daemon.Service = engines.Service = FakeService
        engines.OpenCode.resume = lambda engine, handoff, text, model=None, **sent_as: None
        real_ask = pm.ask
        self.addCleanup(setattr, pm, "ask", real_ask)
        pm.ask = lambda key, message, validate, record=None: self.messages.append(message) or {"action": "close", "conclusion": "done"}
        daemon.ledger.DB = Path(tempfile.mkdtemp()) / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.docket = daemon.Docket()
        self.docket.submit = lambda did, job, *args: job(*args)
        self.db = self.docket.db()
        ledger.add_delegation(self.db, id="ses_x", title="t", directory=None, source_key=None, dispatch_seq=1, dispatched_at=0, brief="", context=[])

    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.db.execute("select detail from events where kind = ? order by id", (kind,))]

    def test_no_permission_notify_no_heartbeat(self):
        permissions.HEARTBEAT = Path(tempfile.mkdtemp()) / "missing" / "pm-online"
        self.assertFalse(permissions.heartbeat())
        self.docket.permission_pass()  # and nothing else happens
        self.assertEqual(self.db.execute("select count(*) from events where kind like 'permission:%'").fetchone()[0], 0)

    def test_the_heartbeat_keeps_docket_online(self):
        self.assertTrue(permissions.heartbeat())
        self.assertTrue(permissions.HEARTBEAT.exists())

    def test_the_ledger_is_read_from_where_docket_left_it(self):
        permissions.LEDGER.write_text(line("ses_x", "pm"))
        entries, offset = permissions.read_ledger(None)
        self.assertEqual(entries, [])  # a first start never replays history
        with permissions.LEDGER.open("a") as handle:
            handle.write(line("ses_x", "pm") + '{"half": ')  # the last line is still being written
        entries, offset = permissions.read_ledger(offset)
        self.assertEqual(len(entries), 1)
        with permissions.LEDGER.open("a") as handle:
            handle.write('1}\n')
        entries, offset = permissions.read_ledger(offset)
        self.assertEqual(entries, [{"half": 1}])
        permissions.LEDGER.write_text(line("ses_x", "human"))  # rotated: shorter than before
        entries, _ = permissions.read_ledger(offset)
        self.assertEqual(entries[0]["tier"], "human")

    def test_decisions_in_handoff_sessions_are_recorded_and_silent_ones_are_not(self):
        permissions.LEDGER.write_text("")
        self.docket.permission_pass()  # starts at the end
        with permissions.LEDGER.open("a") as handle:
            handle.write(line("ses_x", "pm"))
            handle.write(line("ses_x", "silent", cls="5"))
            handle.write(line("ses_x", "human", cls="1", effect="allow", by="popup", answer="once"))
            handle.write(line("ses_x", "human", cls="1", effect="deny", by="timeout", answer="timeout"))
            handle.write(line("ses_other", "pm"))  # not a handoff of Docket's
        self.docket.permission_pass()
        self.assertEqual(len(self.events("permission:pm")), 1)
        self.assertEqual(self.events("permission:person")[0]["answer"], "once")
        self.assertEqual(self.events("permission:denied")[0]["by"], "timeout")
        self.assertEqual(self.db.execute("select count(*) from events where kind like 'permission:%'").fetchone()[0], 3)
        self.docket.permission_pass()  # read once
        self.assertEqual(len(self.events("permission:pm")), 1)

    def test_a_run_waiting_on_the_popup_is_active_not_stuck(self):
        turn = self.docket.track("ses_x", "ses_x", "build it", "dispatch")
        self.db.execute("update turns set confirmed_at = 1, last_activity = 1 where id = ?", (turn,))
        permissions.PENDING.mkdir()
        (permissions.PENDING / "b1.json").write_text(json.dumps({"batch": "b1", "session": "ses_x", "class": "1", "what": ["rm -rf build"]}))
        self.docket.permission_pass()
        self.docket.permission_pass()
        self.assertGreater(self.db.execute("select last_activity from turns where id = ?", (turn,)).fetchone()[0], 1)
        self.assertEqual(len(self.events("permission:waiting")), 1)  # once per batch

    def test_the_pm_reviews_what_was_allowed_on_its_behalf(self):
        self.docket.track("ses_x", "ses_x", "build it", "dispatch")
        self.docket.on_permission(json.loads(line("ses_x", "pm", resources=["git commit -am x"], tool="shell", class_name="项目内执行", cls="3")))
        self.docket.write_card("ses_x", "succeeded", "done")
        self.assertIn("on your behalf", self.messages[-1])
        self.assertIn("git commit -am x", self.messages[-1])
        self.assertIn('<untrusted source="permission-notify">', self.messages[-1])

    def test_with_permission_notify_docket_never_approves_over_the_persons_classes(self):
        answered = []
        engines.OpenCode.answer_permission = lambda engine, handoff, request_id, decision: answered.append(decision)
        self.docket.unattended = lambda row: True
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "read", "resources": ["src/a.py"]})
        self.assertEqual(answered, [])
        self.assertEqual(self.events("permission:ask")[0]["reason"], "permission-notify decides")

    def test_answering_the_agents_question_counts_as_the_run_going_on(self):
        engines.OpenCode.resume = lambda engine, handoff, text, model=None, **kw: {"message": None, "transport": "form", "forms": ["frm_1"]}
        self.docket.settings["engines"] = {"opencode": {"transport": "http"}}
        turn = self.docket.send("ses_x", "中文", via="decision")
        row = self.db.execute("select confirmed_at, transport from turns where id = ?", (turn,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "form")
        self.assertEqual(self.events("turn:answered-question")[0]["forms"], ["frm_1"])

    def test_without_delegated_requests_the_wake_is_unchanged(self):
        self.docket.track("ses_x", "ses_x", "build it", "dispatch")
        self.docket.write_card("ses_x", "succeeded", "done")
        self.assertNotIn("on your behalf", self.messages[-1])



class InboxConvention(unittest.TestCase):
    """A handoff's session is "🦞 …" and archived, also after each of Docket's instructions: OpenCode
    unarchives a session that takes a prompt (seen on 2.2.7), and the inbox would read it as taken over."""

    def service(self):
        from docket import opencode

        calls = []
        service = opencode.Service.__new__(opencode.Service)
        service._request = lambda method, path, body=None, timeout=30: calls.append((method, path, body)) or {"data": {}}
        return service, calls

    def test_docket_instructions_keep_the_session_archived_and_others_do_not(self):
        service, calls = self.service()
        service.prompt("ses_x", "go", message_id="msg_1", metadata={"docket": {"turn": 1}})
        self.assertEqual(calls[-1], ("PATCH", "/api/session/ses_x", {"archived": True}))
        calls.clear()
        service.prompt("ses_x", "go")  # not Docket's (no docket metadata): left as it is
        self.assertEqual([c[0] for c in calls], ["POST"])

    def test_a_new_session_is_titled_for_the_inbox(self):
        from docket import opencode

        made = []
        real = opencode.Service
        fake = type("S", (), {"__init__": lambda self: None, "create_session": lambda self, *a, **k: made.append(a)})
        engines.Service = fake
        self.addCleanup(setattr, engines, "Service", real)
        engine = engines.OpenCode({})
        engine.open_session({"id": "ses_x", "directory": None}, "list view")
        engine.open_session({"id": "ses_y", "directory": None}, "🦞 already")
        self.assertEqual([m[1] for m in made], ["🦞 list view", "🦞 already"])



class AgentQuestions(unittest.TestCase):
    """The agent asked with its question tool and waits: the person's answer goes to that question,
    not into an instruction queued behind it forever (seen on 2026-11-27, six minutes stuck)."""

    FORM = {"id": "frm_1", "fields": [{"key": "q0", "type": "string", "options": [{"value": "中文"}], "custom": True}]}

    def service(self, forms):
        from docket import opencode

        calls = []
        service = opencode.Service.__new__(opencode.Service)

        def request(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return {"data": forms if path.endswith("/form") else {"id": "msg_1"}}

        service._request = request
        service.get = lambda path: request("GET", path)
        return service, calls

    def test_free_text_questions_are_answered_and_others_withdrawn(self):
        service, calls = self.service([self.FORM])
        self.assertTrue(service.answer_forms("ses_x", [self.FORM], "中文，并带上日期"))
        self.assertEqual(calls[-1], ("POST", "/api/session/ses_x/form/frm_1/reply", {"answer": {"q0": "中文，并带上日期"}}))
        closed = {"id": "frm_2", "fields": [{"key": "ok", "type": "boolean"}]}
        self.assertFalse(service.answer_forms("ses_x", [closed], "yes"))
        self.assertEqual(calls[-1][:2], ("DELETE", "/api/session/ses_x/form/frm_2"))

    def test_an_instruction_to_a_waiting_agent_answers_its_question(self):
        service, calls = self.service([self.FORM])
        real = engines.Service
        engines.Service = lambda: service
        self.addCleanup(setattr, engines, "Service", real)
        engine = engines.OpenCode({"engines": {"opencode": {"transport": "http"}}})
        run = engine.resume({"id": "ses_x", "engine_session": "ses_x"}, "中文", message_id="msg_d", metadata={"docket": {}})
        self.assertEqual((run["transport"], run["message"]), ("form", None))
        self.assertFalse(any(path.endswith("/prompt") for _, path, _ in calls))  # no instruction queued behind it
        service, calls = self.service([])
        engines.Service = lambda: service
        run = engine.resume({"id": "ses_x", "engine_session": "ses_x"}, "go on", message_id="msg_d", metadata={"docket": {}})
        self.assertEqual(run["transport"], "http")
        self.assertTrue(any(path.endswith("/prompt") for _, path, _ in calls))


if __name__ == "__main__":
    unittest.main()
