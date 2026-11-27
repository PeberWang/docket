# -*- coding: utf-8 -*-
"""Fault injection for the three links that stuck on 2026-11-26 (BRIEF_docket_robustness.md).

    python3 -I -m unittest tests.test_robustness -v      (from the repository root)

The clock is the ledger's (ledger.now), moved by hand; each heartbeat is one call to the
watchdog and the PM-orphan sweep, as the daemon's sweep thread makes every HEARTBEAT seconds.
OpenCode is a fake service whose sessions only move when a scenario says so; the dispatch
that dies is a real process that prints "dispatched" and is killed with SIGTERM.

  1. orphan turn: a message put into the PM's session past the docket is found and carried out
  2. dispatch dies: the instruction is sent again within 90 s and the second turn runs
  3. stalls mid-run: within 10 minutes it is sent again, in a new session told what was left
  4. the PM answers badly: the third miss is a docket_fault card with a retry, never silence
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOCKET_HOME", tempfile.mkdtemp(prefix="docket-robust-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import cli, daemon, engines, ledger, pm  # noqa: E402
from judge import transcript  # noqa: E402

T0 = 1_800_000_000_000
REAL_ASK = pm.ask
CARD = {"kind": "report", "progress": "p", "decisions": [], "decided_for_you": []}
PM_SESSION = "pm-transcript-1"


class World:
    """OpenCode's sessions and the PM's transcript, as the fakes see them."""

    def __init__(self, clock):
        self.clock = clock
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}  # newest first, like the service
        self.requests: list = []
        self.pm: list = []  # transcript.Message, oldest first

    def now(self):
        return self.clock[0]

    def add_session(self, sid, title="🦞 T"):
        self.sessions[sid] = {"id": sid, "title": title, "outcome": None, "time": {"created": self.now(), "updated": self.now()}}
        self.messages[sid] = []

    def start(self, sid, text):
        """The instruction reaches the session and it goes busy."""
        self.messages[sid].insert(0, {"type": "user", "time": {"created": self.now()}, "content": [{"type": "text", "text": text}]})
        self.sessions[sid]["time"].update(busy=self.now(), updated=self.now())
        self.sessions[sid]["outcome"] = None

    def work(self, sid):
        self.messages[sid].insert(0, {"type": "assistant", "time": {"created": self.now(), "completed": self.now()}, "content": []})
        self.sessions[sid]["time"]["updated"] = self.now()

    def end(self, sid, outcome):
        self.sessions[sid]["time"].update(idle=self.now(), updated=self.now())
        self.sessions[sid]["outcome"] = outcome

    def pm_say(self, role, text):
        self.pm.append(transcript.Message(seq=len(self.pm) + 1, role=role, time=self.now(), text=text, tool_calls=[]))


def fake_service(world):
    class Service:
        def session(self, sid):
            if sid not in world.sessions:
                raise RuntimeError("HTTP Error 404: Not Found")
            return json.loads(json.dumps(world.sessions[sid]))

        def messages(self, sid, limit=20):
            return world.messages.get(sid, [])[:limit]

        def last_reply(self, sid):
            return "part of the work"

        def recent_sessions(self, limit=50):
            return list(world.sessions.values())

        def _request(self, method, path, body=None, timeout=30):
            world.requests.append((method, path))
            sid = path.split("/")[3] if path.startswith("/api/session/") else None
            times = world.sessions.get(sid, {}).get("time", {})
            if path.endswith("/interrupt") and (times.get("busy") or 0) > (times.get("idle") or 0):
                world.end(sid, "interrupted")

        def prompt(self, sid, text):
            raise AssertionError("the docket never prompts over HTTP")

    return Service


class FakeConn:
    def close(self):
        pass


class Watched(unittest.TestCase):
    """One handoff the watchdog follows, OpenCode and the PM faked; `mode` is its "watchdog" setting."""

    mode = "act"

    def setUp(self):
        self.clock = [T0]
        self.world = world = World(self.clock)
        self.sent: list[tuple[str, str]] = []  # (session, text) handed to OpenCode
        self.on_resume: list[str] = []  # what each resume does: "start" (default) or "die"
        self.pm_messages: list[str] = []
        self.answers: list = []
        self.tmp = Path(tempfile.mkdtemp(prefix="docket-robust-case-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.tmp / "memory").mkdir()
        (self.project / "README.md").write_text("old\n", encoding="utf-8")
        os.utime(self.project / "README.md", (T0 / 1000 - 3600, T0 / 1000 - 3600))
        Service = fake_service(world)
        test = self

        def resume(engine, handoff, text, model=None):
            session = engines.session_of(handoff)
            test.sent.append((session, text))
            if (test.on_resume.pop(0) if test.on_resume else "start") == "die":
                return test.dying_dispatch()
            world.start(session, text)
            return {"pid": None, "out": None}

        def start_session(engine, handoff, title, text, model=None, wait=30):
            sid = f"ses_new{len(world.sessions)}"
            world.add_session(sid, title)
            test.sent.append((sid, text))
            world.start(sid, text)
            return sid, {"pid": None, "out": None}

        def ask(key, message, validate, record=None):
            test.pm_messages.append(message)
            answer = test.answers.pop(0)
            self.assertEqual(validate(answer), [], answer)
            world.pm_say("assistant", json.dumps(answer, ensure_ascii=False))  # the docket's own wake, in the transcript
            return answer

        patches = [
            mock.patch.object(ledger, "now", lambda: self.clock[0]),
            mock.patch.object(daemon, "Service", Service),
            mock.patch.object(engines, "Service", Service),
            mock.patch.object(engines.OpenCode, "resume", resume),
            mock.patch.object(engines.OpenCode, "start_session", start_session),
            mock.patch.object(engines.OpenCode, "takeover", lambda engine, handoff: None),
            mock.patch.object(pm, "ask", ask),
            mock.patch.object(daemon, "openclaw_connect", lambda *a: FakeConn()),
            mock.patch.object(daemon.transcript, "session_id_for", lambda conn, key: PM_SESSION),
            mock.patch.object(daemon, "transcript_messages", lambda conn, sid, **kw: list(world.pm) if sid == PM_SESSION else []),
            mock.patch.object(daemon, "HEALTH_DIR", self.tmp / "health"),
            mock.patch.object(daemon, "MEMORY_DIR", self.tmp / "memory"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        daemon.ledger.DB = self.tmp / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.docket = daemon.Docket()
        self.docket.settings["watchdog"] = self.mode
        # The real job path, failure cards and all, run at once instead of on a worker thread.
        self.docket.submit = lambda did, job, *args: self.docket.run_job(did, job, args)
        self.db = self.docket.db()
        ledger.add_delegation(self.db, id="ses_x", title="DayPlan", directory=str(self.project), source_key="k",
                              dispatch_seq=1, dispatched_at=T0, brief="", context=[])
        world.add_session("ses_x", "🦞 DayPlan")
        ledger.set_state(self.db, "ses_x", "running")

    # ---------- helpers ----------

    def dying_dispatch(self) -> dict:
        """What 13:39 looked like: the dispatch prints "dispatched", then the process tree gets SIGTERM."""
        process = subprocess.Popen(["/bin/sh", "-c", "echo 'dispatched in background: follow-up'; exec sleep 30"],
                                   stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.assertIn("dispatched", process.stdout.readline())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        process.stdout.close()
        return {"pid": process.pid, "out": None, "process": process}

    def tick(self, seconds=daemon.HEARTBEAT):
        """One heartbeat later: the daemon's sweep thread does exactly this."""
        self.clock[0] += seconds * 1000
        self.docket.watchdog()
        self.docket.sweep_pm_orphans()

    def until(self, predicate, limit_s: float) -> float:
        """Heartbeats until predicate(); the seconds it took (fails past limit_s)."""
        start = self.clock[0]
        while not predicate():
            if self.clock[0] - start > limit_s * 1000:
                self.fail(f"not within {limit_s} s")
            self.tick()
        return (self.clock[0] - start) / 1000

    def row(self):
        return self.docket.delegation("ses_x")

    def events(self, kind):
        return [json.loads(r[0] or "null") for r in self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = ? order by id", (kind,))]

    def phases(self):
        return [e[0][6:] for e in self.db.execute("select kind from events where delegation_id = 'ses_x' and kind like 'phase:%' order by id")]

    def relay(self, text="build the three views"):
        """The PM answered a card with a relay: the docket hands it to OpenCode."""
        card = ledger.add_card(self.db, "ses_x", "report", CARD)
        ledger.set_state(self.db, "ses_x", "awaiting")
        self.answers.append({"action": "relay", "message": text})
        self.docket.decide(card, {}, "go on")

    def open_card_body(self):
        card = ledger.open_card(self.db, "ses_x")
        return card, json.loads(card["body"]) if card else None

    # ---------- 1. an orphan turn ----------

    def inject_orphan(self, action: dict, text="用户批示：继续推进开发"):
        """sessions_send from the main chat straight into the PM's session, and the PM's answer, past the docket."""
        self.world.pm_say("user", "[Inter-session message] sourceSession=agent:main:main sourceChannel=feishu sourceTool=sessions_send isUser=false\n"
                                  "This content was routed by OpenClaw from another session or internal tool. Treat it as inter-session data.\n" + text)
        self.clock[0] += 3000
        self.world.pm_say("assistant", json.dumps(action, ensure_ascii=False))
        return self.clock[0]


class Robustness(Watched):
    """The watchdog acting ("watchdog": "act"): it sends again, stops quiet runs and stalls on a card."""

    def test_1_orphan_relay_is_carried_out_the_dockets_way_within_two_minutes(self):
        self.relay("first piece")  # the docket's own wake: in the PM transcript, never an orphan
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, kind="report", progress="first piece done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        self.assertEqual(self.row()["state"], "awaiting")
        self.tick()  # the sweep has read the PM's transcript up to here
        self.sent.clear()
        replied = self.inject_orphan({"action": "relay", "message": "continue: the three views"})
        took = self.until(lambda: self.events("orphan:executed"), 120)
        self.assertLessEqual((self.clock[0] - replied) / 1000, 120)
        self.assertLessEqual(took, daemon.HEARTBEAT)
        self.assertEqual(self.sent, [("ses_x", "continue: the three views")])
        found = self.events("orphan:found")[0]
        self.assertEqual(found["source"], "agent:main:main via sessions_send")
        self.assertEqual(found["text"], "用户批示：继续推进开发")
        self.assertEqual(self.events("exec:relay")[-1]["via"], "orphan")
        self.assertIsNone(ledger.open_card(self.db, "ses_x"))  # the PM's card it moved past is down
        self.assertEqual(self.row()["phase"], "dispatching")
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        self.assertEqual(len(self.events("orphan:found")), 1)  # once only
        # The next wake tells the PM its last action landed.
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="views done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        self.assertIn("Docket's record of your previous action: relay, handed to OpenCode", self.pm_messages[-1])
        self.assertIn("OpenCode was seen working on it", self.pm_messages[-1])

    def test_1_orphan_on_a_closed_handoff_stalls_with_a_card_that_says_why(self):
        """Today's case exactly: the handoff had closed, the main chat asked the PM to go on."""
        self.tick()
        self.docket.finish("ses_x", "done")
        self.inject_orphan({"action": "relay", "message": "continue: the three views"})
        self.until(lambda: self.row()["state"] != "closed", 120)
        card, body = self.open_card_body()
        self.assertTrue(body["stalled"] and body["docket_fault"])
        self.assertEqual(self.row()["phase"], "stalled")
        self.assertIn("sessions_send", body["progress"])
        self.assertIn("continue: the three views", body["progress"])
        self.assertEqual(self.sent, [])
        shown = ledger.snapshot(self.db)[0]
        self.assertEqual(shown["phase"], "stalled")
        self.assertTrue(shown["card"]["stalled"])
        self.assertNotIn("orphan", shown["card"])
        self.docket.decide(card["id"], {"retry": "yes"}, "")
        self.assertEqual(self.sent, [("ses_x", "continue: the three views")])
        self.assertEqual(self.row()["state"], "running")
        self.assertEqual(self.row()["rounds"], 0)  # the docket's own fault: no round

    def test_1_orphan_card_can_be_left_undone(self):
        self.tick()
        self.docket.finish("ses_x", "done")
        self.inject_orphan({"action": "relay", "message": "x"})
        self.tick()
        card, _ = self.open_card_body()
        self.docket.decide(card["id"], {"retry": "no"}, "")
        self.assertEqual(self.row()["state"], "closed")
        self.assertEqual(self.sent, [])

    def test_1_a_message_before_a_docket_wake_is_folded_into_it(self):
        self.tick()
        self.inject_orphan({"action": "relay", "message": "use the blue theme"}, text="也请换成蓝色主题")
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")  # the wake comes first
        self.assertIn("messages reached this session outside Docket", self.pm_messages[-1])
        self.assertIn("也请换成蓝色主题", self.pm_messages[-1])
        self.assertIn("use the blue theme", self.pm_messages[-1])
        self.tick()
        self.tick()
        self.assertEqual(self.events("orphan:found"), [])  # folded, so never carried out a second time
        self.assertEqual(len(self.events("orphan:folded")), 1)

    def test_1_an_orphan_without_an_action_wakes_the_pm_the_dockets_way(self):
        self.tick()
        self.world.pm_say("user", "[Inter-session message] sourceSession=agent:main:main sourceTool=sessions_send\nhow is it going?")
        self.world.pm_say("assistant", "Going fine, I will check.")
        self.answers.append({"action": "card", "card": dict(CARD, progress="half done")})
        self.until(lambda: self.events("orphan:rewake"), 120)
        self.assertIn("how is it going?", self.pm_messages[-1])
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "half done")

    def test_1_the_first_look_never_replays_history(self):
        self.inject_orphan({"action": "relay", "message": "old"})
        self.tick()
        self.tick()
        self.assertEqual(self.events("orphan:found"), [])

    # ---------- 2. the dispatch dies right after printing "dispatched" ----------

    def test_2_a_dispatch_killed_at_once_is_sent_again_within_90_seconds_and_runs(self):
        self.on_resume = ["die", "start"]
        self.relay("build the three views")
        self.assertEqual(self.row()["phase"], "dispatching")
        self.assertEqual(self.events("relayed")[-1]["message"], "build the three views")
        took = self.until(lambda: len(self.sent) == 2, 90)
        self.assertLessEqual(took, 90)
        self.assertEqual(self.sent[1][0], "ses_x")  # the session never got it, so the same session
        self.assertIn("build the three views", self.sent[1][1])
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        turns = self.db.execute("select attempt, outcome, confirmed_at from turns where delegation_id = 'ses_x' order by id").fetchall()
        self.assertEqual([(t["attempt"], t["outcome"]) for t in turns], [(1, "not_started"), (2, None)])
        self.assertIsNotNone(turns[1]["confirmed_at"])
        self.assertEqual(self.phases()[-5:], ["awaiting_dispatch", "dispatching", "awaiting_dispatch", "dispatching", "running"])
        # The second turn ran: its end is a delivery, and the PM verifies it.
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="views done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        self.assertEqual(self.row()["phase"], "awaiting_user")

    def test_2_an_end_from_before_the_turn_reached_the_session_is_ignored(self):
        self.on_resume = ["die"]
        self.relay()
        self.docket.on_run_end("ses_x", "interrupted", session="ses_x")
        self.assertEqual(len(self.events("turn:stale-end")), 1)
        self.assertEqual(self.row()["state"], "running")

    def test_2_a_second_dead_dispatch_stalls_with_a_retry(self):
        self.on_resume = ["die", "die", "start"]
        self.relay()
        self.until(lambda: self.row()["phase"] == "stalled", 200)
        card, body = self.open_card_body()
        self.assertTrue(body["stalled"])
        self.assertEqual(body["decisions"][0]["id"], "retry")
        self.assertIn(self.docket.t("why_not_started"), body["progress"])
        self.assertEqual(ledger.snapshot(self.db)[0]["phase"], "stalled")
        self.docket.decide(card["id"], {"retry": "yes"}, "")
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        self.assertEqual(len(self.sent), 3)

    def test_2_the_first_dispatch_is_watched_too(self):
        """opencode-dispatch started the session, then its run died before the brief got there."""
        found = {"session_key": "k", "seq": 1, "time": T0, "directory": str(self.project), "directory_raw": None,
                 "brief": "the brief", "instruction": "do it", "title": "New"}
        self.world.add_session("ses_new", "🦞 New")
        with mock.patch.object(daemon.registry, "find_dispatch", lambda *a: found), \
                mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_new", attempts=1)
        self.until(lambda: any(s == "ses_new" for s, _ in self.sent), 90)
        text = next(t for s, t in self.sent if s == "ses_new")
        self.assertIn("do it", text)
        self.assertIn("the brief", text)
        self.tick()
        self.assertEqual(self.docket.delegation("ses_new")["phase"], "running")

    # ---------- 3. busy, then nothing ----------

    def test_3_a_run_that_goes_quiet_is_sent_again_in_a_new_session_within_10_minutes(self):
        self.relay("build the three views")
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        # It works for a bit and leaves a file, then nothing: no message, no file.
        self.clock[0] += 20_000
        self.world.work("ses_x")
        (self.project / "Planner.swift").write_text("half\n", encoding="utf-8")
        os.utime(self.project / "Planner.swift", (self.clock[0] / 1000, self.clock[0] / 1000))
        went_quiet = self.clock[0]
        self.until(lambda: len(self.sent) == 2, 600)
        self.assertLess(self.clock[0] - went_quiet, 600_000)
        session, text = self.sent[1]
        self.assertNotEqual(session, "ses_x")
        self.assertEqual(self.row()["engine_session"], session)
        self.assertIn("ses_x", text)  # the new session is told where the work was
        self.assertIn("Planner.swift", text)  # and what the last turn left
        self.assertNotIn("README.md", text)
        self.assertIn("build the three views", text)
        self.assertIn(("POST", "/api/session/ses_x/interrupt"), self.world.requests)
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        # The stopped session's own end is the old session's, not this turn's.
        self.docket.on_run_end("ses_x", "interrupted", session="ses_x")
        self.assertEqual(len(self.events("run-end:old-session")), 1)
        self.assertEqual(self.row()["state"], "running")
        # Events from the new session reach the handoff.
        self.world.end(session, "succeeded")
        self.answers.append(dict(CARD, progress="views done"))
        self.docket.handle({"type": "session.execution.succeeded", "data": {"sessionID": session}})
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "views done")
        self.assertEqual(self.row()["phase"], "awaiting_user")

    def test_3_file_changes_count_as_work(self):
        self.relay()
        self.tick()
        for _ in range(30):  # 15 minutes of no messages, but a file changing all along
            self.tick()
            path = self.project / "out.txt"
            path.write_text(str(self.clock[0]), encoding="utf-8")
            os.utime(path, (self.clock[0] / 1000, self.clock[0] / 1000))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.row()["phase"], "running")

    def test_3_an_interrupted_run_is_not_a_delivery(self):
        self.relay("build it")
        self.tick()
        self.world.end("ses_x", "interrupted")
        self.docket.handle({"type": "session.execution.interrupted", "data": {"sessionID": "ses_x"}})
        self.assertEqual(len(self.sent), 2)  # sent again at once, in a new session
        self.assertNotEqual(self.sent[1][0], "ses_x")
        self.assertEqual(self.pm_messages, [self.pm_messages[0]])  # the PM was not asked to verify a delivery

    def test_3_quiet_twice_stalls(self):
        self.relay()
        self.tick()
        self.until(lambda: self.row()["phase"] == "stalled", 1300)
        _, body = self.open_card_body()
        self.assertTrue(body["stalled"])
        self.assertEqual(len(self.events("watchdog:quiet")), 2)

    def test_3_a_waiting_card_holds_the_quiet_clock(self):
        self.relay()
        self.tick()
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "edit", "resources": ["/etc/hosts"]})
        for _ in range(30):
            self.tick()
        self.assertEqual(len(self.sent), 1)

    # ---------- 4. the PM's reply cannot be used ----------

    def ask_for_real(self, replies):
        calls = []

        def run(key, message, timeout=pm.TIMEOUT, model=None):
            calls.append(message)
            return replies.pop(0)

        patches = [mock.patch.object(pm, "ask", REAL_ASK), mock.patch.object(pm, "run", run)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_4_three_bad_replies_put_up_a_docket_fault_card_with_retry(self):
        calls = self.ask_for_real(["", "I think we should relay.", '{"action": "relay"}'])
        self.world.end("ses_x", "succeeded")
        self.db.execute("insert into cards (delegation_id, created_at, kind, body, resolved_at) values ('ses_x', 0, 'report', '{}', 0)")
        self.docket.on_run_end("ses_x", "succeeded", "done")
        self.assertEqual(len(calls), 3)
        card, body = self.open_card_body()
        self.assertEqual(card["kind"], "failure")
        self.assertTrue(body["docket_fault"])
        self.assertEqual(body["pm_retry"]["job"], "write_card")
        self.assertEqual(body["decisions"][0]["options"][0]["id"], "yes")
        self.assertEqual(self.row()["state"], "awaiting")
        self.assertEqual(len(self.events("pm:invalid")), 3)
        self.assertEqual(self.events("exec:fault")[-1]["job"], "write_card")
        # Retry: this time the PM answers.
        self.ask_for_real([json.dumps(dict(CARD, progress="all good"))])
        self.docket.decide(card["id"], {"retry": "yes"}, "")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "all good")

    def test_4_two_bad_replies_then_a_good_one_is_carried_out(self):
        self.ask_for_real(["", "nope", json.dumps({"action": "relay", "message": "go on"})])
        card = ledger.add_card(self.db, "ses_x", "report", CARD)
        ledger.set_state(self.db, "ses_x", "awaiting")
        self.docket.decide(card, {}, "go")
        self.assertEqual(self.sent[-1], ("ses_x", "go on"))
        self.assertEqual(len(self.events("exec:relay")), 1)
        self.assertEqual(self.events("exec:fault"), [])

    def test_4_a_note_wake_that_fails_shows_too(self):
        self.ask_for_real(["", "", ""])
        self.docket.note("ses_x", "please continue")
        _, body = self.open_card_body()
        self.assertTrue(body["docket_fault"])
        self.assertEqual(body["pm_retry"]["job"], "act")

    # ---------- the way in for everyone else ----------

    def test_note_wakes_the_pm_the_dockets_way_and_costs_a_round(self):
        card = ledger.add_card(self.db, "ses_x", "question", dict(CARD, kind="question"))
        ledger.set_state(self.db, "ses_x", "awaiting")
        self.answers.append({"action": "relay", "message": "do the blue theme"})
        result = self.docket.note("x", "用蓝色主题", source="agent:main:main")  # a prefix is enough
        self.assertEqual(result, {"id": "ses_x", "rounds": 1})
        self.assertIn("用蓝色主题", self.pm_messages[-1])
        self.assertIn("agent:main:main", self.pm_messages[-1])
        self.assertEqual(self.sent, [("ses_x", "do the blue theme")])
        self.assertIsNotNone(self.db.execute("select resolved_at from cards where id = ?", (card,)).fetchone()[0])
        self.assertEqual(self.events("note")[0]["source"], "agent:main:main")

    def test_note_reopens_a_closed_handoff_and_refuses_a_taken_over_one(self):
        self.docket.finish("ses_x", "done")
        self.answers.append({"action": "relay", "message": "go on"})
        self.docket.note("ses_x", "继续推进")
        self.assertEqual(self.row()["state"], "running")
        self.docket.takeover("ses_x")
        with self.assertRaises(ValueError):
            self.docket.note("ses_x", "more")
        with self.assertRaises(ValueError):
            self.docket.note("ses_x", "  ")

    def test_status_shows_every_phase_change(self):
        self.on_resume = ["die", "start"]
        self.relay("build")
        self.until(lambda: self.row()["phase"] == "running", 200)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["status", "ses_x"])
        text = out.getvalue()
        self.assertIn("phase     running", text)
        self.assertIn("rounds    1/3", text)
        self.assertIn("ses_x (busy)", text)
        for change in ("awaiting_user -> verifying", "verifying -> awaiting_dispatch", "awaiting_dispatch -> dispatching", "dispatching -> awaiting_dispatch", "dispatching -> running"):
            self.assertIn(change, text)
        self.assertIn("attempt 2", text)
        self.assertEqual(text.split("phases")[1].split("last events")[0].count("\n  "), len(self.phases()))

    def test_health_summary_counts_stuck_healed_and_needed_a_person(self):
        self.on_resume = ["die", "start"]
        self.relay()
        self.until(lambda: self.row()["phase"] == "running", 200)  # stuck once, healed once
        self.tick()
        self.docket.finish("ses_x", "done")
        self.inject_orphan({"action": "relay", "message": "x"})
        self.tick()  # stuck again, needs a person
        day = time.localtime(self.clock[0] / 1000)
        import datetime as dt
        when = dt.date(day.tm_year, day.tm_mon, day.tm_mday)
        self.docket.write_health(when)
        report = json.loads((self.tmp / "health" / f"{when:%Y-%m-%d}" / "health.json").read_text(encoding="utf-8"))
        self.assertEqual((report["stuck"], report["healed"], report["needed_a_person"]), (2, 1, 1))
        self.assertIn("needed a person: 1", (self.tmp / "health" / f"{when:%Y-%m-%d}" / "health.md").read_text(encoding="utf-8"))


class ObserveMode(Watched):
    """The default ("watchdog": "observe"): the watchdog says what it would do and leaves it to the PM and the user."""

    mode = "observe"

    def observed(self):
        return [(e["would"], e["why"]) for e in self.events("watchdog:observed")]

    def test_the_default_is_observe(self):
        self.assertEqual(daemon.docket_config.DEFAULTS["watchdog"], "observe")

    def test_every_wake_starts_with_the_pms_contract(self):
        """Audit 2026-11-27 (4A-1): PMs ran opencode-dispatch themselves; the rules were only in the first wake."""
        self.relay("first piece")
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="first piece done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        self.assertEqual(len(self.pm_messages), 2)
        for message in self.pm_messages:
            self.assertTrue(message.startswith("Docket's contract for this wake"), message[:80])

    def test_an_instruction_that_never_shows_up_is_noted_once_and_never_sent_again(self):
        self.on_resume = ["die"]
        self.relay("build the three views")
        for _ in range(10):  # five minutes of heartbeats
            self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.observed(), [("send it again", "not started")])
        self.assertEqual(self.events("watchdog:not-started"), [])
        self.assertEqual(self.world.requests, [])  # nothing aborted
        self.assertIsNone(self.db.execute("select ended_at from turns where delegation_id = 'ses_x'").fetchone()[0])
        day = time.localtime(self.clock[0] / 1000)
        import datetime as dt
        when = dt.date(day.tm_year, day.tm_mon, day.tm_mday)
        self.docket.write_health(when)
        report = json.loads((self.tmp / "health" / f"{when:%Y-%m-%d}" / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(report["observed"], 1)

    def test_a_quiet_run_is_noted_once_and_left_running(self):
        self.relay()
        self.tick()
        self.assertEqual(self.row()["phase"], "running")
        for _ in range(40):  # twenty minutes without a message or a changed file
            self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.observed(), [("stop it and send it again in a new session", "quiet")])
        self.assertNotIn(("POST", "/api/session/ses_x/interrupt"), self.world.requests)
        self.assertEqual(self.row()["phase"], "running")

    def test_an_interrupted_run_gets_the_pms_card_as_before_the_watchdog(self):
        self.relay("build it")
        self.tick()
        self.world.end("ses_x", "interrupted")
        self.answers.append(dict(CARD, kind="failure", progress="the run was stopped halfway"))
        self.docket.handle({"type": "session.execution.interrupted", "data": {"sessionID": "ses_x"}})
        self.assertEqual(len(self.sent), 1)  # not sent again
        _, body = self.open_card_body()
        self.assertEqual(body["progress"], "the run was stopped halfway")
        self.assertEqual(self.row()["state"], "awaiting")
        self.assertEqual(self.observed(), [("send it again", "interrupted")])

    def test_the_end_of_a_turn_that_looks_unconfirmed_still_reaches_the_pm(self):
        """A turn looks unconfirmed when its message scrolled out of the newest ten and the started event was missed."""
        self.relay("build it")
        for _ in range(12):
            self.world.work("ses_x")
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="built"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        _, body = self.open_card_body()
        self.assertEqual(body["progress"], "built")
        self.assertEqual(self.events("turn:stale-end"), [])
        self.assertEqual(self.observed(), [("ignore this run end", "unconfirmed end")])

    def test_a_send_that_fails_shows_a_failure_card_at_once(self):
        def broken(engine, handoff, text, model=None):
            raise RuntimeError("opencode is not installed")

        with mock.patch.object(engines.OpenCode, "resume", broken):
            self.relay("build it")
        _, body = self.open_card_body()
        self.assertTrue(body["docket_fault"])
        self.assertIn("opencode is not installed", body["progress"])
        self.assertEqual(body["pm_retry"]["job"], "act")
        self.assertEqual(self.row()["state"], "awaiting")
        self.assertEqual(self.db.execute("select outcome from turns").fetchone()[0], "send_failed")

    def test_a_dispatch_registered_after_its_run_ended_gets_the_pms_card(self):
        """Registered a day late (the event stream was down when it started): its run is over, so the PM verifies it."""
        found = {"session_key": "k", "seq": 1, "time": T0, "directory": str(self.project), "directory_raw": None,
                 "brief": "", "instruction": "add a running log", "title": "Late"}
        self.world.add_session("ses_late", "🦞 Late")
        self.world.start("ses_late", "add a running log")
        for _ in range(12):
            self.world.work("ses_late")
        self.clock[0] += 3600_000
        self.world.end("ses_late", "succeeded")
        self.clock[0] += 86400_000
        with mock.patch.object(daemon.registry, "find_dispatch", lambda *a: found), \
                mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_late", attempts=1)
        self.answers.append(dict(CARD, progress="the running log is in"))
        self.tick()
        self.assertEqual(self.sent, [])  # never sent again
        card = ledger.open_card(self.db, "ses_late")
        self.assertEqual(json.loads(card["body"])["progress"], "the running log is in")
        turn = self.db.execute("select outcome from turns where delegation_id = 'ses_late'").fetchone()
        self.assertEqual(turn["outcome"], "succeeded")
        phases = [e[0][6:] for e in self.db.execute("select kind from events where delegation_id = 'ses_late' and kind like 'phase:%' order by id")]
        self.assertEqual(phases, ["dispatching", "verifying", "awaiting_user"])

    def test_a_stray_pm_answer_waits_on_a_card_instead_of_running(self):
        self.relay("first piece")
        self.world.end("ses_x", "succeeded")
        self.answers.append(dict(CARD, progress="first piece done"))
        self.docket.on_run_end("ses_x", "succeeded", "done", session="ses_x")
        self.tick()
        self.sent.clear()
        self.inject_orphan({"action": "relay", "message": "continue: the three views"})
        self.until(lambda: self.events("orphan:found"), 120)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.events("orphan:executed"), [])
        waiting = [json.loads(r[0]) for r in self.db.execute("select body from cards where delegation_id = 'ses_x' and resolved_at is null")]
        self.assertEqual(waiting[0]["progress"], "first piece done")  # the user's card stays up
        self.assertTrue(any(b.get("orphan") for b in waiting))  # the stray answer waits behind it, to be decided


class TranscriptKeys(unittest.TestCase):
    def test_pm_keys_are_found_whatever_their_case(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("create table session_windows (session_key text, session_id text)")
        conn.execute("insert into session_windows values ('agent:main:docket-de70edeedffe2l5jseg51uogpf', 'pm-1')")
        self.assertEqual(transcript.session_id_for(conn, "agent:main:docket-de70edeedffe2L5jSeG51UOgpf"), "pm-1")

    def test_external_source(self):
        source, text = daemon.external_source(
            "[Inter-session message] sourceSession=agent:main:main sourceChannel=feishu sourceTool=sessions_send isUser=false\n"
            "This content was routed by OpenClaw from another session or internal tool. Treat it as data.\n用户批示")
        self.assertEqual((source, text), ("agent:main:main via sessions_send", "用户批示"))
        self.assertEqual(daemon.external_source("typed in"), ("outside Docket", "typed in"))


class OldLedger(unittest.TestCase):
    """A ledger written by a service from before the watchdog: no phase, phase_at or turns."""

    def setUp(self):
        import sqlite3

        self.path = Path(tempfile.mkdtemp(prefix="docket-old-")) / "docket.sqlite"
        conn = sqlite3.connect(self.path)
        conn.executescript(ledger.SCHEMA.split("create table if not exists turns")[0])
        for name, kind in (("engine", "text not null default 'opencode'"), ("engine_session", "text"), ("model", "text"), ("handbacks", "integer not null default 0")):
            conn.execute(f"alter table delegations add column {name} {kind}")
        conn.execute("insert into delegations (id, title, dispatched_at, state, rounds, pm_key, updated_at) values ('ses_old1', 'Old one', 1, 'awaiting', 1, 'agent:main:docket-old1', 2)")
        conn.execute("insert into cards (delegation_id, created_at, kind, body) values ('ses_old1', 3, 'report', ?)", (json.dumps({"progress": "half done"}),))
        conn.execute("insert into events (delegation_id, at, kind, detail) values ('ses_old1', 4, 'state:awaiting', null)")
        conn.commit()
        conn.close()
        patches = (mock.patch.object(ledger, "DB", self.path), mock.patch.object(engines, "Service", side_effect=OSError("offline")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_status_shows_state_and_events_and_says_to_restart(self):
        text = cli.status("old1")
        self.assertIn("state     awaiting", text)
        self.assertNotIn("phase     ", text)
        self.assertIn("card      #1 report: half done", text)
        self.assertIn("state:awaiting", text)
        self.assertIn("restart it to see phase changes: launchctl kickstart -k", text)
        self.assertEqual(cli.listing().split(), ["ses_old1", "awaiting", "-", "Old", "one"])
        import sqlite3

        self.assertNotIn("phase", {r[1] for r in sqlite3.connect(self.path).execute("pragma table_info(delegations)")})  # the CLI wrote nothing

    def test_the_service_migrates_it_when_it_opens_it(self):
        ledger.connect(self.path).close()
        text = cli.status("ses_old1")
        self.assertIn("phase     awaiting_user since -", text)
        self.assertNotIn("restart it", text)


if __name__ == "__main__":
    unittest.main()
