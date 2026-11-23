# -*- coding: utf-8 -*-
"""A stranger's first run: an empty DOCKET_HOME, no config.json, no judge key.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)

The daemon must start, say in one log line that the judge is off, register a dispatch, and
never call the judge. Nothing here reads the real keychain, OpenClaw or OpenCode.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOCKET_HOME", tempfile.mkdtemp(prefix="docket-unit-"))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config, daemon, ledger, registry  # noqa: E402
from judge import backend, keys, relevance  # noqa: E402

# The real daemon.main(), with only the two outside edges cut: no keychain item (as on a new
# Mac), and a judge call is recorded instead of going out.
CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from judge import jev, keys, openai_judge
keys.read = lambda service, account: None
def called(*args, **kwargs):
    Path(sys.argv[2]).write_text("called", encoding="utf-8")
    raise RuntimeError("the judge was called without a key")
jev.ask = openai_judge.ask = called
from docket import daemon
daemon.main()
"""

# Stands in for the Claude Code CLI: stays "running" long enough to be seen, then reports.
STUB = """#!/bin/sh
sleep 30
echo '{"type": "result", "result": "hi", "session_id": "s"}'
"""


class FreshInstall(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="docket-fresh-"))
        self.home = self.root / "home"
        self.data = self.root / "data"  # does not exist yet: the daemon creates it
        self.project = self.home / "project"
        self.project.mkdir(parents=True)
        stubs = self.root / "bin"
        stubs.mkdir()
        (stubs / "claude").write_text(STUB, encoding="utf-8")
        (stubs / "claude").chmod(0o755)
        self.calls = self.root / "judge-calls"
        self.log = self.root / "daemon.log"
        env = {
            "HOME": str(self.home),
            "DOCKET_HOME": str(self.data),
            "PATH": f"{stubs}:/usr/bin:/bin",
            "DOCKET_OPENCLAW_DB": str(self.root / "no-openclaw.sqlite"),
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(self.root),
        }
        with open(self.log, "wb") as out:
            self.daemon = subprocess.Popen(
                [sys.executable, "-I", "-c", CHILD, str(ROOT), str(self.calls)],
                env=env, stdout=out, stderr=subprocess.STDOUT, cwd=str(self.root),
            )
        self.addCleanup(self.stop)

    def stop(self):
        self.daemon.terminate()
        try:
            self.daemon.wait(10)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
        try:
            conn = sqlite3.connect(self.data / "docket.sqlite")
            pids = [row[0] for row in conn.execute("select pid from runs")]
            conn.close()
        except sqlite3.Error:
            pids = []
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGTERM)  # the stub runs in its own session
            except OSError:
                pass

    def service(self) -> dict:
        discovery = self.data / "daemon.json"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                self.fail(f"the daemon exited:\n{self.log.read_text(encoding='utf-8')}")
            if discovery.exists():
                return json.loads(discovery.read_text(encoding="utf-8"))
            time.sleep(0.2)
        self.fail(f"the daemon did not start:\n{self.log.read_text(encoding='utf-8')}")

    def call(self, service: dict, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(service["url"] + path, data=data, method="POST" if data else "GET",
                                         headers={"X-Docket-Token": service["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_starts_registers_a_dispatch_and_never_calls_the_judge(self):
        service = self.service()
        self.assertFalse((self.data / "config.json").exists())  # defaults only

        judge = self.call(service, "/api/settings")["judge"]
        self.assertEqual((judge["available"], judge["missing"]), (False, "no key"))

        sent = self.call(service, "/api/dispatch", {"engine": "claude", "title": "Say hello", "instruction": "say hi", "directory": str(self.project)})
        self.assertTrue(sent["ok"])
        state = self.call(service, "/api/state")
        self.assertEqual([(d["id"], d["title"], d["state"]) for d in state["delegations"]], [(sent["id"], "Say hello", "running")])

        log = self.log.read_text(encoding="utf-8")
        self.assertIn("judge off (no key for systemone)", log)
        self.assertNotIn("Traceback", log)
        self.assertFalse(self.calls.exists(), "the judge was called")


class NoKeyRegistration(unittest.TestCase):
    """The OpenCode path: a new session matching a dispatch registers, with no judge call."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "proj").mkdir()
        mock.patch.object(keys, "read", lambda service, account: None).start()
        mock.patch.object(backend, "SECRETS", self.root / "no-secrets.json").start()
        mock.patch.dict(os.environ, {"HOME": str(self.root)}).start()
        for name in ("DOCKET_JUDGE_API_KEY", "TYPESAFE_API_KEY", "DOCKET_JUDGE_BACKEND", "DOCKET_JUDGE_ENDPOINT", "DOCKET_JUDGE_MODEL"):
            os.environ.pop(name, None)
        self.addCleanup(mock.patch.stopall)
        self.judged = []
        mock.patch.object(backend.Judge, "ask", lambda *a, **k: self.judged.append(a)).start()
        mock.patch.object(relevance, "context_scores", lambda *a, **k: self.judged.append(a)).start()
        daemon.ledger.DB = self.root / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.docket = daemon.Docket()
        self.docket.settings = dict(config.DEFAULTS)  # whatever config.json the test machine has, use the defaults

    def test_registers_without_the_judge(self):
        self.assertFalse(self.docket.judging)
        dispatch = {"session_key": "agent:main:main", "session_id": "chat", "seq": 7, "time": 1, "directory": str(self.root / "proj"),
                    "directory_raw": str(self.root / "proj"), "brief": "", "instruction": "go"}
        history = [types.SimpleNamespace(seq=6, role="user", time=0, text="please add tests")]

        class Session:
            def session(self, sid):
                return {"title": "Add tests", "time": {"created": 1}}  # no prefix: the default

        class Connection:
            def close(self):
                pass

        with mock.patch.object(daemon, "Service", Session), \
                mock.patch.object(registry, "find_dispatch", lambda title, *a: dispatch if title == "Add tests" else None), \
                mock.patch.object(registry, "openclaw_connect", Connection), \
                mock.patch.object(registry, "messages", lambda *a, **k: history):
            self.docket.maybe_register("ses_fresh", attempts=1)
        row = self.docket.delegation("ses_fresh")
        self.assertEqual((row["title"], row["state"], json.loads(row["context"])), ("Add tests", "running", []))
        self.assertEqual(self.judged, [])


class Defaults(unittest.TestCase):
    def test_no_personal_defaults(self):
        self.assertEqual(config.DEFAULTS["prefix"], "")
        self.assertIsNone(config.DEFAULTS["fallback_model"])

    def test_a_configured_prefix_is_stripped_and_an_empty_one_strips_nothing(self):
        command = 'opencode-dispatch --detach --title "[oc] Fix it" "go"'
        self.assertEqual(registry.dispatches.parse(command, "[oc] ")["title"], "Fix it")
        self.assertEqual(registry.dispatches.parse(command)["title"], "[oc] Fix it")

    def test_with_no_prefix_a_dispatcher_marker_still_matches(self):
        same = registry.dispatches.same_task
        self.assertTrue(same("Fix it", "Fix it"))
        self.assertTrue(same("\U0001F99E Fix it", "Fix it"))  # opencode-dispatch's default inbox marker
        self.assertTrue(same("\u2605 Fix it", "Fix it"))
        self.assertFalse(same("[oc] Fix it", "Fix it"))  # letters: configure it as the prefix
        self.assertFalse(same("Please Fix it", "Fix it"))
        self.assertFalse(same("\u2605Fix it", "Fix it"))  # no space, not a marker
        self.assertFalse(same("\u2605 Fix it", "Fix it", "[oc] "))  # a configured prefix means exact titles

    def test_a_mistyped_setting_falls_back_to_its_default(self):
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"prefix": None, "watch": "agent:main:main", "move_threshold": "high", "locale": "zh"}), encoding="utf-8")
        with mock.patch.object(config, "PATH", path), self.assertLogs("docket", "WARNING"):
            loaded = config.load()
        self.assertEqual((loaded["prefix"], loaded["watch"], loaded["move_threshold"], loaded["locale"]), ("", ["*"], 0.85, "zh"))
