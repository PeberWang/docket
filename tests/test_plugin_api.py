# -*- coding: utf-8 -*-
"""The service side of the OpenClaw plugin: what it reads (GET /api/openclaw), what it reports
(delivered conclusions, refused tool calls, arriving chat messages), the PM agent setting, and
desk-dispatch --supersedes.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

os.environ["DOCKET_HOME"] = tempfile.mkdtemp(prefix="docket-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import config as docket_config  # noqa: E402
from docket import daemon, engines, ledger, pm  # noqa: E402

CARD = {"kind": "report", "progress": "p", "decisions": [], "decided_for_you": []}


class PluginApi(unittest.TestCase):
    def setUp(self):
        self.sent, self.answers = [], []
        sent = self.sent

        class FakeService:
            def prompt(self, sid, text):
                sent.append(text)

            def last_reply(self, sid):
                return ""

            def session(self, sid):
                return {"time": {"busy": 1, "idle": 2}}

        daemon.Service = engines.Service = FakeService
        engines.OpenCode.resume = lambda engine, handoff, text, model=None, **sent_as: sent.append(text)
        engines.Cli.takeover = engines.OpenCode.takeover = lambda engine, handoff: None
        self.real_ask = pm.ask
        pm.ask = lambda key, message, validate, record=None: self.answers.pop(0)
        self.addCleanup(setattr, pm, "ask", self.real_ask)
        daemon.ledger.DB = Path(tempfile.mkdtemp()) / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.docket = daemon.Docket()
        self.docket.submit = lambda did, job, *args: job(*args)
        self.db = self.docket.db()
        ledger.add_delegation(self.db, id="ses_x", title="list view", directory=None, source_key="agent:main:main",
                              dispatch_seq=1, dispatched_at=0, brief="", context=[])

    def state(self, handoff="ses_x"):
        return self.docket.delegation(handoff)["state"]

    # ---------- the PM agent ----------

    def test_the_pm_runs_in_its_own_agent_once_that_is_set(self):
        self.assertEqual(self.docket.delegation("ses_x")["pm_key"], "agent:main:docket-x")
        self.assertEqual(ledger.pm_key("ses_ab_c", "docket-pm"), "agent:docket-pm:ab-c")
        # The PM's own agent is named on the command line; the main agent's PMs run as before.
        self.assertEqual(pm.agent_args("agent:docket-pm:ab-c"), ["--agent", "docket-pm"])
        self.assertEqual(pm.agent_args("agent:main:docket-x"), [])

    def test_a_new_handoff_takes_the_pm_agent_from_the_settings(self):
        self.docket.settings["pm_agent"] = "docket-pm"
        self.docket.dispatch_cli = daemon.Docket.dispatch_cli.__get__(self.docket)
        launched = []
        self.docket.launch = lambda handoff, prompt, first: launched.append(handoff)
        self.docket.attach_context = lambda *a: None
        handoff = self.docket.dispatch_cli("claude", "t", str(Path.home()), "do it", "", None)
        self.assertTrue(self.docket.delegation(handoff)["pm_key"].startswith("agent:docket-pm:"))
        self.assertEqual(launched, [handoff])

    def test_a_pm_agent_that_is_not_an_agent_id_falls_back_to_main(self):
        path = docket_config.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        self.addCleanup(lambda: path.write_text(old, encoding="utf-8") if old is not None else path.unlink())
        for value, expected in (("docket-pm", "docket-pm"), ("Docket PM", "main"), ("../x", "main"), (3, "main")):
            path.write_text(json.dumps({"pm_agent": value}), encoding="utf-8")
            self.assertEqual(docket_config.load()["pm_agent"], expected, value)

    # ---------- what the plugin reads ----------

    def test_the_view_lists_open_handoffs_their_sessions_and_pm_prefixes(self):
        ledger.add_card(self.db, "ses_x", "question", dict(CARD, kind="question", progress="Sort by date or by name?"))
        ledger.set_state(self.db, "ses_x", "awaiting")
        self.docket.settings["pm_agent"] = "docket-pm"
        view = self.docket.openclaw_view()
        self.assertEqual(view["version"], 1)
        self.assertEqual(view["sessions"]["ses_x"], "ses_x")
        self.assertEqual(view["pm_agent"], "docket-pm")
        self.assertIn("agent:docket-pm:", view["pm_prefixes"])
        self.assertIn("agent:main:docket-", view["pm_prefixes"])  # handoffs registered before the PM agent
        self.assertEqual(view["pm_sessions"]["agent:main:docket-x"], "ses_x")
        [item] = view["handoffs"]
        self.assertEqual((item["id"], item["state"], item["needs_you"], item["source"]), ("ses_x", "awaiting", True, "agent:main:main"))
        self.assertIn("Sort by date", item["card"])
        json.dumps(view)  # the plugin reads it as JSON

    def test_a_closed_handoff_leaves_the_list_but_its_session_stays_guarded(self):
        self.docket.finish("ses_x", "List view done.")
        view = self.docket.openclaw_view()
        self.assertEqual(view["handoffs"], [])
        self.assertIn("ses_x", view["sessions"])  # a note may open it again: writes still go through Docket

    def test_a_conclusion_is_delivered_once(self):
        card = self.docket.finish("ses_x", "List view done.")
        [item] = self.docket.openclaw_view()["conclusions"]
        self.assertEqual((item["card"], item["session"]), (card, "agent:main:main"))
        self.assertIn("List view done.", item["text"])
        self.assertEqual(self.docket.conclusions_delivered([card, card]), 1)
        self.assertEqual(self.docket.conclusions_delivered([card]), 0)
        self.assertEqual(self.docket.openclaw_view()["conclusions"], [])
        self.assertEqual(self.db.execute("select count(*) from events where kind = 'conclusion:delivered'").fetchone()[0], 1)

    def test_a_conclusion_still_being_written_waits_for_the_pm_sentence(self):
        self.answers.append({"action": "close", "conclusion": "The list view is done; export is left."})
        self.docket.submit = lambda did, job, *args: None  # the PM's conclusion has not come yet
        self.docket.close("ses_x", "")
        self.assertEqual(self.docket.openclaw_view()["conclusions"], [])
        self.db.execute("update cards set created_at = ? where kind = 'closed'", (ledger.now() - 6 * 60_000,))
        [item] = self.docket.openclaw_view()["conclusions"]  # a PM that never answers: the plain one goes
        self.assertTrue(item["text"])

    def test_no_conclusion_goes_to_a_chat_nobody_dispatched_from(self):
        self.db.execute("update delegations set source_key = null where id = 'ses_x'")
        self.docket.finish("ses_x", "done")
        self.assertEqual(self.docket.openclaw_view()["conclusions"], [])

    def test_a_card_that_is_not_a_conclusion_cannot_be_marked_delivered(self):
        card = ledger.add_card(self.db, "ses_x", "report", CARD)
        self.assertEqual(self.docket.conclusions_delivered([card]), 0)

    # ---------- what the plugin reports ----------

    def test_a_refused_tool_call_lands_on_its_handoff(self):
        self.docket.plugin_refused({"agent": "main", "session": "agent:main:main", "tool": "exec", "rule": "write-into-handoff",
                                    "command": "opencode-dispatch -s ses_x 'go on'", "target": "ses_x"})
        self.docket.plugin_refused({"agent": "main", "tool": "sessions_send", "rule": "send-to-pm", "target": "agent:main:docket-x"})
        self.docket.plugin_refused({"agent": "docket-pm", "tool": "exec", "rule": "pm-dispatch", "command": "opencode run x" * 100})
        rows = self.db.execute("select delegation_id, detail from events where kind = 'plugin:refused' order by id").fetchall()
        self.assertEqual([r[0] for r in rows], ["ses_x", "ses_x", None])
        self.assertLessEqual(len(json.loads(rows[2][1])["command"]), 300)

    def test_a_chat_message_wakes_the_transcript_scan(self):
        self.assertFalse(self.docket.chat_due.is_set())
        self.docket.chat_arrived("agent:main:main")
        self.assertTrue(self.docket.chat_due.is_set())

    # ---------- --supersedes ----------

    def test_a_newer_handoff_closes_the_one_it_replaces(self):
        ledger.add_delegation(self.db, id="ses_y", title="list view, again", directory=None, source_key="agent:main:main",
                              dispatch_seq=2, dispatched_at=0, brief="", context=[])
        turn = self.docket.track("ses_x", "ses_x", "build it", "dispatch")
        stopped = []
        self.docket.stop_turn = lambda t: stopped.append(t["id"])
        self.assertEqual(self.docket.supersede("x", "ses_y", "list view, again"), "ses_x")  # a prefix is enough
        self.assertEqual(self.state("ses_x"), "closed")
        body = json.loads(self.db.execute("select body from cards where delegation_id = 'ses_x' and kind = 'closed'").fetchone()[0])
        self.assertEqual(body["superseded_by"], "ses_y")
        self.assertIn("list view, again", body["conclusion"])
        self.assertEqual(stopped, [turn])  # stopped first, so two sessions never work on the same thing
        self.assertNotEqual(self.state("ses_y"), "closed")
        # Twice is harmless; replacing itself is refused.
        self.assertIsNone(self.docket.supersede("ses_x", "ses_y", "list view, again"))
        with self.assertRaises(ValueError):
            self.docket.supersede("ses_y", "ses_y", "x")

    # ---------- over HTTP ----------

    def serve(self) -> dict:
        if daemon.DISCOVERY.exists():
            daemon.DISCOVERY.unlink()
        threading.Thread(target=daemon.serve, args=(self.docket,), daemon=True).start()
        for _ in range(250):
            if daemon.DISCOVERY.exists():
                return json.loads(daemon.DISCOVERY.read_text(encoding="utf-8"))
            time.sleep(0.02)
        self.fail("the service did not start")

    def desk_dispatch(self, *args, **env):
        script = Path(__file__).resolve().parent.parent / "integrations/openclaw/desk-dispatch/scripts/desk-dispatch"
        return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                                   "DOCKET_HOME": str(ledger.HOME), **env})

    def test_desk_dispatch_supersedes_through_the_service(self):
        self.serve()
        started = []
        self.docket.dispatch_opencode = lambda title, directory, instruction, brief, model: started.append(title) or "ses_new"
        done = self.desk_dispatch("--title", "list view, again", "--supersedes", "ses_nope", "redo it")
        self.assertEqual((done.returncode, started), (1, []))  # an unknown handoff: nothing starts
        self.assertIn("Docket refused", done.stderr)
        done = self.desk_dispatch("--title", "list view, again", "--supersedes", "ses_x", "redo it")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("replacing ses_x", done.stdout)
        self.assertEqual(self.state("ses_x"), "closed")

    def test_desk_dispatch_supersedes_when_opencode_dispatch_starts_the_work(self):
        self.serve()

        def old_opencode(*args):
            raise LookupError("this OpenCode is driven through opencode-dispatch")

        self.docket.dispatch_opencode = old_opencode
        fake = Path(tempfile.mkdtemp()) / "opencode-dispatch"
        fake.write_text("#!/bin/sh\necho started \"$@\"\n", encoding="utf-8")
        fake.chmod(0o755)
        done = self.desk_dispatch("--title", "list view, again", "--supersedes", "x", "redo it", OPENCODE_DISPATCH=str(fake))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("started --detach --title list view, again", done.stdout)
        self.assertEqual(self.state("ses_x"), "closed")

    def test_desk_dispatch_refuses_follow_ups(self):
        done = self.desk_dispatch("--title", "t", "-s", "ses_x", "go on")
        self.assertEqual(done.returncode, 2)
        self.assertIn("follow-ups go through Docket", done.stderr)

    def test_the_routes_answer_with_the_token_only(self):
        service = self.serve()

        def call(path, body=None, token=service["token"]):
            request = urllib.request.Request(f"{service['url']}{path}", data=json.dumps(body).encode() if body is not None else None,
                                             method="POST" if body is not None else "GET",
                                             headers={"Content-Type": "application/json", "X-Docket-Token": token})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read() or b"{}")

        self.assertEqual(call("/api/openclaw", token="wrong")[0], 401)
        status, view = call("/api/openclaw")
        self.assertEqual((status, view["version"]), (200, 1))
        card = self.docket.finish("ses_x", "done")
        self.assertEqual(call("/api/openclaw/delivered", {"cards": [card]}), (200, {"ok": True, "delivered": 1}))
        self.assertEqual(call("/api/openclaw/refused", {"tool": "exec", "rule": "r"})[0], 202)
        self.assertEqual(call("/api/openclaw/chat", {"session": "agent:main:main"})[0], 202)
        self.assertEqual(call("/api/dispatch", {"title": "t", "instruction": "i", "supersedes": "ses_nope"})[0], 409)


if __name__ == "__main__":
    unittest.main()
