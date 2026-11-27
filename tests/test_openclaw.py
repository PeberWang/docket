# -*- coding: utf-8 -*-
"""Reading OpenClaw 2026.11: the custom event shape, heartbeat window keys, the database path.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)

Every database here is a synthetic copy in a temporary directory; OpenClaw's own are never opened.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["DOCKET_HOME"] = tempfile.mkdtemp(prefix="docket-unit-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config, daemon, ledger, registry  # noqa: E402
from judge import backend, transcript  # noqa: E402

MAIN = "221272f9-9411-4c57-8ce8-a4a6bbadf870"
SMOKE = 'cd "$HOME/proj" && desk-dispatch --detach --title "冒烟：更新后派发链路" -f BRIEF.md "跑一遍"'
T0 = 1_763_500_000_000


class Stop(BaseException):
    """Ends the daemon's endless poll loop from a test."""


def old_call(command):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": "派了"}, {"type": "toolCall", "name": "exec", "arguments": {"command": command}}]}}


def new_call(command):
    """2026.11: a custom event wrapping the assistant message; arguments arrive as a JSON string."""
    inner = {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "exec", "arguments": json.dumps({"command": command}, ensure_ascii=True)}]}
    return {"type": "message", "message": {"role": "custom", "customType": "openclaw.message", "message": inner}}


def new_user(text):
    return {"type": "message", "message": {"role": "custom", "customType": "openclaw.user_message", "content": [{"type": "text", "text": text}]}}


def agent_db(path: Path, events, windows) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "create table transcript_events (session_id text, seq integer, created_at integer, event_json text);"
        "create table session_windows (session_key text, session_id text);"
    )
    conn.executemany("insert into transcript_events values (?, ?, ?, ?)", [(sid, seq, at, json.dumps(e, ensure_ascii=False)) for sid, seq, at, e in events])
    conn.executemany("insert into session_windows values (?, ?)", windows)
    conn.commit()
    conn.close()
    return path


class Events(unittest.TestCase):
    def test_old_shape_still_reads(self):
        role, text, calls = transcript.normalize(old_call(SMOKE))
        self.assertEqual((role, text), ("assistant", "派了"))
        self.assertEqual(calls[0]["command"], SMOKE)

    def test_custom_event_wrapping_a_message(self):
        role, _, calls = transcript.normalize(new_call(SMOKE))
        self.assertEqual(role, "assistant")
        self.assertEqual(calls, [{"name": "exec", "arguments": {"command": SMOKE}, "command": SMOKE}])

    def test_custom_event_whose_type_names_the_role(self):
        self.assertEqual(transcript.normalize(new_user("[message_id: om_1]\nou_ab: 那个任务怎么样"))[:2], ("user", "那个任务怎么样"))

    def test_custom_event_that_is_the_tool_call(self):
        event = {"message": {"role": "custom", "customType": "openclaw.tool_call", "data": {"toolName": "exec", "input": {"command": ["/bin/zsh", "-lc", SMOKE]}}}}
        role, _, calls = transcript.normalize(event)
        self.assertEqual(role, "assistant")
        self.assertEqual(calls[0]["command"], SMOKE)

    def test_openai_style_tool_calls_list(self):
        event = {"message": {"role": "custom", "customType": "openclaw.reply", "payload": {"toolCalls": [{"function": {"name": "exec", "arguments": json.dumps({"cmd": SMOKE})}}]}}}
        self.assertEqual(transcript.normalize(event)[2][0]["command"], SMOKE)

    def test_custom_events_that_are_not_messages_are_dropped(self):
        for event in ({"message": {"role": "custom", "customType": "openclaw.compaction", "data": {"summary": "x"}}}, {"message": {"role": "toolResult", "content": "ok"}}):
            with self.subTest(event=event):
                self.assertIsNone(transcript.normalize(event)[0])

    def test_the_parser_gets_the_whole_command(self):
        parsed = registry.dispatches.parse(transcript.normalize(new_call(SMOKE))[2][0]["command"])
        self.assertEqual((parsed["title"], parsed["brief_path"], parsed["instruction"]), ("冒烟：更新后派发链路", "BRIEF.md", "跑一遍"))


class Keys(unittest.TestCase):
    def test_heartbeat_window_is_the_main_chat(self):
        self.assertEqual(transcript.canonical_key("agent:main:main:heartbeat"), "agent:main:main")
        settings = {"watch": ["agent:main:main"]}
        self.assertTrue(config.watched(settings, "agent:main:main:heartbeat"))
        self.assertTrue(config.watched(settings, "agent:main:main"))
        self.assertFalse(config.watched(settings, "agent:main:other"))

    def test_cron_and_pm_sessions_stay_unwatched(self):
        for key in ("agent:main:cron:daily", "agent:main:cron:daily:heartbeat", "agent:main:docket-x", "agent:main:desk-x"):
            with self.subTest(key=key):
                self.assertFalse(config.watched({"watch": ["*"]}, key))

    def test_one_session_under_two_windows_is_one_session(self):
        path = agent_db(Path(tempfile.mkdtemp()) / "a.sqlite", [], [("agent:main:main:heartbeat", MAIN), ("agent:main:main", MAIN), ("agent:main:cron:x", "s2")])
        conn = sqlite3.connect(path)
        self.assertEqual(transcript.session_keys(conn), {MAIN: "agent:main:main", "s2": "agent:main:cron:x"})
        conn.close()

    def test_session_found_through_its_heartbeat_key(self):
        path = agent_db(Path(tempfile.mkdtemp()) / "a.sqlite", [], [("agent:main:main:heartbeat", MAIN)])
        conn = sqlite3.connect(path)
        self.assertEqual(transcript.session_id_for(conn, "agent:main:main"), MAIN)
        conn.close()


class Paths(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fallback = agent_db(self.root / "old.sqlite", [], [])
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        os.environ.pop("DOCKET_OPENCLAW_DB", None)
        self.addCleanup(patcher.stop)

    def state(self, rows, columns=("agent_id", "path")):
        path = self.root / "state/openclaw.sqlite"
        path.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(f"create table agent_databases ({', '.join(columns)}, kind text)")
        conn.executemany(f"insert into agent_databases ({', '.join(columns)}, kind) values (?, ?, ?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_agent_databases_is_asked_first(self):
        new = agent_db(self.root / "new.sqlite", [], [])
        state = self.state([("main", str(new), "agent"), ("other", str(self.fallback), "agent")])
        self.assertEqual(transcript.resolve(state, self.fallback), (new, "agent_databases"))

    def test_the_row_holding_transcripts_wins(self):
        memory = self.root / "memory.sqlite"
        conn = sqlite3.connect(memory)
        conn.execute("create table chunks (x)")
        conn.close()
        (self.root / "agents/main").mkdir(parents=True)
        new = agent_db(self.root / "agents/main/agent.sqlite", [], [])
        state = self.state([("main", str(memory), "memory"), ("main", "../agents/main/agent.sqlite", "agent")])
        self.assertEqual(transcript.resolve(state, self.fallback), (new, "agent_databases"))

    def test_falls_back_when_the_state_database_is_absent(self):
        self.assertEqual(transcript.resolve(self.root / "nope.sqlite", self.fallback), (self.fallback, "fallback"))

    def test_falls_back_and_says_why_when_the_mapped_file_is_missing(self):
        state = self.state([("main", str(self.root / "gone.sqlite"), "agent")])
        path, source = transcript.resolve(state, self.fallback)
        self.assertEqual(path, self.fallback)
        self.assertIn("gone.sqlite", source)

    def test_other_column_names(self):
        new = agent_db(self.root / "new.sqlite", [], [])
        state = self.state([("main", str(new), "agent")], columns=("agent", "db_path"))
        self.assertEqual(transcript.resolve(state, self.fallback)[0], new)

    def test_unopenable_database_retries_once_then_says_so(self):
        attempts = []
        real = sqlite3.connect
        with mock.patch.object(transcript, "RETRY_DELAY", 0), mock.patch.object(transcript.sqlite3, "connect", lambda *a, **k: attempts.append(a) or real(*a, **k)):
            with self.assertRaises(transcript.Unavailable) as caught:
                transcript.connect(self.root / "missing.sqlite")
        self.assertEqual(len(attempts), 2)
        self.assertIn("missing.sqlite", str(caught.exception))
        self.assertIn("unable to open database file", str(caught.exception))


class Daemon(unittest.TestCase):
    """The daemon against a synthetic 2026.11 transcript."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        events = [
            (MAIN, 27187, T0 - 5000, new_user("派个冒烟任务")),
            (MAIN, 27188, T0 - 2000, new_call(SMOKE)),
            ("cron1", 5, T0 - 1000, new_call(SMOKE.replace("冒烟：更新后派发链路", "定时"))),
        ]
        self.agent = agent_db(self.root / "agent.sqlite", events, [("agent:main:main:heartbeat", MAIN), ("agent:main:main", MAIN), ("agent:main:cron:nightly", "cron1")])
        (self.root / "proj").mkdir()
        patcher = mock.patch.dict(os.environ, {"DOCKET_OPENCLAW_DB": str(self.agent), "HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        daemon.ledger.DB = self.root / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.docket = daemon.Docket()
        self.docket.settings = {**config.DEFAULTS, "watch": ["agent:main:main"]}

    def add(self, seq, event):
        conn = sqlite3.connect(self.agent)
        conn.execute("insert into transcript_events values (?, ?, ?, ?)", (MAIN, seq, ledger.now(), json.dumps(event, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def test_new_dispatch_is_found_under_the_plain_key(self):
        found = registry.find_dispatch("冒烟：更新后派发链路", T0, self.docket.settings)
        self.assertEqual((found["session_key"], found["session_id"], found["seq"]), ("agent:main:main", MAIN, 27188))
        self.assertEqual(found["instruction"], "跑一遍")
        self.assertIsNone(registry.find_dispatch("定时", T0, self.docket.settings))  # cron dispatches are not the docket's

    def test_new_dispatch_registers(self):
        class Session:
            def session(self, sid):
                return {"title": "冒烟：更新后派发链路", "time": {"created": T0}}

        with mock.patch.object(daemon, "Service", Session), mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_e08e87462ffeKMGprek88OIEUQ", attempts=1)
        row = self.docket.delegation("ses_e08e87462ffeKMGprek88OIEUQ")
        self.assertEqual((row["source_key"], row["dispatch_seq"], row["state"]), ("agent:main:main", 27188, "running"))

    def test_a_session_with_the_inbox_marker_registers_under_the_dispatch_title(self):
        class Session:
            def session(self, sid):
                return {"title": "\U0001F99E 冒烟：更新后派发链路", "time": {"created": T0}}  # opencode-dispatch adds the marker

        with mock.patch.object(daemon, "Service", Session), mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_marker", attempts=1)
        row = self.docket.delegation("ses_marker")
        self.assertEqual((row["title"], row["dispatch_seq"]), ("冒烟：更新后派发链路", 27188))

    def test_mentions_read_custom_user_events_once_per_session(self):
        db = self.docket.db()
        ledger.add_delegation(db, id="ses_live", title="t", directory=None, source_key="agent:main:main", dispatch_seq=1, dispatched_at=0, brief="", context=[])
        seen = []
        self.docket.attribute = lambda message, recent, live, *session: seen.append(message.text)
        self.docket.scan_mentions(db)  # first sight sets the cursor
        self.add(27189, new_user("冒烟那个怎么样了"))
        self.docket.scan_mentions(db)
        self.assertEqual(seen, ["冒烟那个怎么样了"])

    def test_an_unopenable_database_is_one_readable_warning(self):
        self.docket.settings["attribute"] = True
        mock.patch.object(backend, "available", lambda: True).start()
        self.addCleanup(mock.patch.stopall)
        os.environ["DOCKET_OPENCLAW_DB"] = str(self.root / "missing.sqlite")
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if polls.count(daemon.MENTION_POLL) == 3:
                raise Stop

        with mock.patch.object(daemon.time, "sleep", sleep):
            with self.assertLogs("docket", "WARNING") as logs, self.assertRaises(Stop):
                self.docket.watch_mentions()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("missing.sqlite", logs.output[0])
        self.assertNotIn("Traceback", logs.output[0])

    def test_registration_says_why_when_the_database_is_unreadable(self):
        class Session:
            def session(self, sid):
                return {"title": "冒烟：更新后派发链路", "time": {"created": T0}}

        os.environ["DOCKET_OPENCLAW_DB"] = str(self.root / "missing.sqlite")
        with mock.patch.object(daemon, "Service", Session), mock.patch.object(transcript, "RETRY_DELAY", 0):
            self.docket.maybe_register("ses_y", attempts=1)
        detail = json.loads(self.docket.db().execute("select detail from events where kind = 'skipped'").fetchone()[0])
        self.assertEqual(detail["reason"], "transcript unavailable")

    def test_selfcheck_says_it_in_one_line(self):
        out = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/selfcheck.py"), "--db", str(self.agent), "--hours", "100000"],
            capture_output=True, text=True, env={**os.environ, "DOCKET_HOME": str(self.root)},
        )
        first = out.stdout.splitlines()[0]
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("agent.sqlite (--db)", first)
        self.assertIn("agent:main:main = 221272f9", first)
        self.assertIn("2/2 recent events read as messages", first)
        self.assertTrue(first.startswith("WARN"))  # recognised, but nothing registered it in this ledger
        self.assertIn("“冒烟：更新后派发链路” recognised, not in the ledger", first)

    def test_selfcheck_is_ok_once_the_dispatch_is_registered(self):
        self.test_new_dispatch_registers()
        out = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/selfcheck.py"), "--db", str(self.agent), "--hours", "100000"],
            capture_output=True, text=True, env={**os.environ, "DOCKET_HOME": str(self.root)},
        )
        first = out.stdout.splitlines()[0]
        self.assertTrue(first.startswith("OK"), first)
        self.assertIn("in the ledger as ses_e08e87462ffeKMGprek88OIEUQ (running)", first)

    def test_selfcheck_fails_plainly_on_a_missing_database(self):
        out = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/selfcheck.py"), "--db", str(self.root / "nope.sqlite")], capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertTrue(out.stdout.startswith("FAIL  transcript"))


if __name__ == "__main__":
    unittest.main()
