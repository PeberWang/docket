# -*- coding: utf-8 -*-
"""Offline checks of the docket's decision paths (fake PM, fake OpenCode).

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["DOCKET_HOME"] = tempfile.mkdtemp(prefix="docket-unit-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import daemon, dispatches, engines, ledger, pm  # noqa: E402

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
        daemon.ledger.DB = Path(tempfile.mkdtemp()) / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.docket = daemon.Docket()
        self.docket.submit = lambda did, job, *args: job(*args)
        self.db = self.docket.db()
        ledger.add_delegation(self.db, id="ses_x", title="t", directory=None, source_key="k", dispatch_seq=1, dispatched_at=0, brief="", context=[])

    def awaiting_card(self):
        card_id = ledger.add_card(self.db, "ses_x", "report", CARD)
        ledger.set_state(self.db, "ses_x", "awaiting")
        return card_id

    def test_mention_relay_needs_confirmation(self):
        self.answers.append({"action": "relay", "message": "do more"})
        self.docket.act("ses_x", "m", "a chat message")
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(card["body"])["pending_relay"], "do more")
        self.assertEqual(self.sent, [])
        self.docket.decide(card["id"], {"relay": "yes"}, "")
        self.assertEqual(self.sent, ["do more"])
        self.assertEqual(self.docket.delegation("ses_x")["rounds"], 1)

    def test_mention_relay_declined_sends_nothing(self):
        self.answers.append({"action": "relay", "message": "do more"})
        self.docket.act("ses_x", "m", "a chat message")
        self.docket.decide(ledger.open_card(self.db, "ses_x")["id"], {"relay": "no"}, "")
        self.assertEqual(self.sent, [])

    def test_pm_sessions_new_prefix_and_old_rows_kept(self):
        self.assertEqual(ledger.pm_key("ses_ab_c"), "agent:main:docket-ab-c")
        self.db.execute("update delegations set pm_key = 'agent:main:desk-x' where id = 'ses_x'")
        self.assertEqual(self.docket.delegation("ses_x")["pm_key"], "agent:main:desk-x")
        from docket import config
        settings = {"watch": ["*"]}
        self.assertFalse(config.watched(settings, "agent:main:docket-x"))
        self.assertFalse(config.watched(settings, "agent:main:desk-x"))
        self.assertTrue(config.watched(settings, "agent:main:main"))

    def test_round_limit(self):
        for i in range(3):
            self.answers.append({"action": "relay", "message": f"r{i}"})
            self.docket.decide(self.awaiting_card(), {}, "go")
        self.assertEqual(self.docket.delegation("ses_x")["rounds"], 3)
        self.docket.decide(self.awaiting_card(), {}, "one more")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "limit")

    def test_card_and_close_actions(self):
        self.answers.append({"action": "card", "card": dict(CARD, kind="question")})
        self.docket.decide(self.awaiting_card(), {}, "先问一下")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "question")
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.answers.append({"action": "close", "conclusion": "办完了"})
        self.docket.decide(ledger.open_card(self.db, "ses_x")["id"], {}, "可以了")
        self.assertEqual(self.docket.delegation("ses_x")["state"], "closed")
        self.assertIn("办完了", next(daemon.MEMORY_DIR.glob("*.md")).read_text(encoding="utf-8"))

    def test_permission_requests_ask_only_when_destructive(self):
        answered = []
        engines.OpenCode.answer_permission = lambda engine, handoff, request, decision: answered.append((request, decision))
        # Reads outside the project are approved silently: the run was started with --auto.
        self.docket.source_tool = lambda session_id, source: "read"
        self.docket.permission_card("ses_x", {"id": "per_read", "action": "external_directory", "resources": ["/tmp/notes.md"], "source": {"type": "tool"}})
        self.assertEqual(answered, [("per_read", "once")])
        self.assertIsNone(ledger.open_card(self.db, "ses_x"))
        # A destructive one becomes a card and waits for the user.
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "shell", "resources": ["rm -rf /tmp/build"]})
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(card["body"])["pending_permission"], "per_1")
        self.assertNotIn("pending_permission", ledger.snapshot(self.db)[0]["card"])
        self.docket.decide(card["id"], {"permission": "once"}, "")
        self.assertEqual(answered[-1], ("per_1", "once"))
        self.assertEqual(self.docket.delegation("ses_x")["state"], "running")
        self.assertEqual(self.sent, [])  # answering a permission is not a round

    def test_permission_tiers(self):
        project = Path(tempfile.mkdtemp()) / "proj"
        (project / "src").mkdir(parents=True)
        (project.parent / "link").symlink_to(project)
        ledger.add_delegation(self.db, id="ses_p", title="p", directory=str(project.parent / "link"), source_key=None, dispatch_seq=None, dispatched_at=0, brief="", context=[])
        answered = []
        engines.OpenCode.answer_permission = lambda engine, handoff, request, decision: answered.append(request)
        tools = {"t_read": "read", "t_write": "write", "t_shell": "shell"}
        self.docket.source_tool = lambda session_id, source: tools.get((source or {}).get("id"))
        cases = [
            # (request, auto-approved?, reason)
            ({"action": "edit", "resources": ["src/a.py"]}, True, "inside project, write"),
            ({"action": "read", "resources": [str(project / "README.md")]}, True, "inside project, read"),
            ({"action": "edit", "resources": [str(project / "src/../../proj/b.py")]}, True, "inside project, write"),
            ({"action": "external_directory", "resources": ["/etc/*"], "source": {"type": "tool", "id": "t_read"}}, True, "outside project, read only"),
            ({"action": "external_directory", "resources": ["/tmp/*"], "source": {"type": "tool", "id": "t_write"}}, False, "outside project, write"),
            ({"action": "edit", "resources": [str(project / "../escape.txt")]}, False, "outside project, write"),
            ({"action": "external_directory", "resources": ["/tmp/x/*"], "metadata": {"filepath": "/tmp/x/a", "parentDir": "/tmp/x"}}, False, "outside project, write"),
            ({"action": "shell", "resources": ["rm -rf build"]}, False, "destructive"),
            ({"action": "edit", "resources": [str(project / "trash/a")]}, False, "destructive"),
            ({"action": "external_directory", "resources": ["/opt/*"], "source": {"type": "tool", "id": "t_shell"}}, False, "outside project, direction unknown"),
            ({"action": "external_directory", "resources": ["/opt/*"]}, False, "outside project, direction unknown"),
            ({"action": "external_directory", "resources": []}, False, "no resources"),
            ({"action": "shell", "resources": ["pytest -q"]}, True, "project shell"),
        ]
        for i, (request, auto, reason) in enumerate(cases):
            with self.subTest(request=request):
                self.db.execute("update cards set resolved_at = 1 where delegation_id = 'ses_p' and resolved_at is null")
                self.docket.permission_card("ses_p", {"id": f"per_{i}", **request})
                event = self.db.execute("select kind, detail from events where delegation_id = 'ses_p' and kind like 'permission:%' order by id desc limit 1").fetchone()
                self.assertEqual(event[0], "permission:auto" if auto else "permission:ask")
                self.assertEqual(json.loads(event[1])["reason"], reason)
                self.assertEqual(f"per_{i}" in answered, auto)
                card = ledger.open_card(self.db, "ses_p")
                self.assertEqual(card is not None and json.loads(card["body"])["pending_permission"] == f"per_{i}", not auto)
        # A handoff with no directory of its own vouches for nothing outside reads.
        self.docket.source_tool = lambda session_id, source: "write"
        self.docket.permission_card("ses_x", {"id": "per_w", "action": "external_directory", "resources": ["/tmp/*"], "source": {"type": "tool"}})
        self.assertNotIn("per_w", answered)

    def test_cards_wait_their_turn_and_none_is_overwritten(self):
        self.docket.show_card("ses_x", "question", dict(CARD, progress="first"))
        self.docket.show_card("ses_x", "report", dict(CARD, progress="second"))
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "first")
        self.assertEqual(self.db.execute("select count(*) from cards where delegation_id='ses_x' and queued=1").fetchone()[0], 1)
        ledger.resolve_card(self.db, ledger.open_card(self.db, "ses_x")["id"], {"reply": "ok"})
        self.docket.advance("ses_x")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "second")

    def test_a_permission_card_takes_its_turn_like_any_other(self):
        self.docket.show_card("ses_x", "question", dict(CARD, progress="brief"))
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "external_directory", "resources": ["/tmp/a", "/tmp/b"]})
        brief = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(brief["body"])["progress"], "brief")  # the brief keeps its place
        ledger.resolve_card(self.db, brief["id"], {"reply": "ok"})
        self.docket.advance("ses_x")
        permission = ledger.open_card(self.db, "ses_x")
        progress = json.loads(permission["body"])["progress"]
        self.assertIn("files there may change", progress)  # one plain sentence; no tool behind it, so not called a read
        self.assertNotIn("/tmp/a", progress)  # no raw paths
        answered = []
        engines.OpenCode.answer_permission = lambda engine, handoff, request, decision: answered.append(decision)
        self.docket.decide(permission["id"], {"permission": "once"}, "")
        self.assertEqual(answered, ["once"])

    def register(self, command: str, home: Path):
        """Run maybe_register on a dispatch command as OpenClaw would have written it."""
        parsed = dispatches.parse(command)
        found = {"session_key": "k", "session_id": "s", "seq": 1, "time": 0, "directory": parsed["directory"],
                 "directory_raw": parsed["directory_raw"], "brief": "", "instruction": parsed["instruction"]}

        class Session:
            def session(self, sid):
                return {"title": f"🦞 {parsed['title']}", "time": {"created": 1}, "location": {}}

        daemon.Service = Session
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(daemon.registry, "find_dispatch", lambda *a: found), \
                mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_new", attempts=1)
        return self.docket.delegation("ses_new")

    def test_home_variable_in_dispatch_directory_is_stored_expanded(self):
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "Desktop/Code工作区/proj").mkdir(parents=True)
        row = self.register('cd "$HOME/Desktop/Code工作区/proj" && opencode-dispatch --detach --auto --title "🦞 T" "go"', home)
        self.assertEqual(row["directory"], str(home / "Desktop/Code工作区/proj"))
        self.assertEqual(row["state"], "running")
        self.assertIsNone(ledger.open_card(self.db, "ses_new"))

    def test_missing_dispatch_directory_fails_visibly(self):
        home = Path(tempfile.mkdtemp()).resolve()
        row = self.register('cd "$HOME/nope" && opencode-dispatch --detach --title "🦞 T" "go"', home)
        self.assertIsNone(row["directory"])  # nothing unrunnable is stored
        self.assertEqual(row["state"], "closed")
        card = ledger.open_card(self.db, "ses_new")
        self.assertEqual(card["kind"], "failure")
        progress = json.loads(card["body"])["progress"]
        self.assertIn("$HOME/nope", progress)
        self.assertIn(str(home / "nope"), progress)
        event = self.db.execute("select detail from events where delegation_id = 'ses_new' and kind = 'directory:missing'").fetchone()
        self.assertEqual(json.loads(event[0]), {"raw": "$HOME/nope", "expanded": str(home / "nope")})

    def test_rows_stored_with_a_literal_home_still_resolve(self):
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "proj").mkdir()
        ledger.add_delegation(self.db, id="ses_old", title="o", directory="$HOME/proj", source_key=None, dispatch_seq=None, dispatched_at=0, brief="", context=[])
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(self.docket.project_dir(self.docket.delegation("ses_old")), home / "proj")

    def test_card_validation(self):
        self.assertEqual(pm.validate_card(dict(CARD)), [])
        bad = {"kind": "x", "progress": "", "decisions": [{"id": "d1", "question": "q", "options": [{"id": "a", "label": "a"}]}]}
        self.assertEqual(len(pm.validate_card(bad)), 3)
        self.assertEqual(pm.extract_json('好\n```json\n{"action": "close", "conclusion": "ok"}\n```')["action"], "close")


class Directories(unittest.TestCase):
    """`cd "$HOME/…"` in a dispatch once reached the ledger verbatim and failed at resume."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        (self.home / "x").mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def directory(self, cd: str):
        return dispatches.parse(f'cd {cd} && opencode-dispatch --detach --title "🦞 T" "go"')["directory"]

    def test_home_variable_expands(self):
        for cd in ('"$HOME/x"', "$HOME/x", "'${HOME}/x'", '"${HOME}/x/"'):
            with self.subTest(cd=cd):
                self.assertEqual(self.directory(cd), str(self.home / "x"))
        self.assertEqual(dispatches.parse('cd "$HOME/x" && opencode-dispatch --title "T" "go"')["directory_raw"], "$HOME/x")

    def test_tilde_still_expands(self):
        self.assertEqual(self.directory("~/x"), str(self.home / "x"))
        self.assertEqual(dispatches.existing_dir("~/x"), self.home / "x")

    def test_missing_directory_names_both_paths(self):
        with self.assertRaises(dispatches.MissingDirectory) as caught:
            dispatches.existing_dir("$HOME/nope")
        self.assertEqual((caught.exception.raw, caught.exception.expanded), ("$HOME/nope", str(self.home / "nope")))
        self.assertIn("$HOME/nope -> " + str(self.home / "nope"), str(caught.exception))

    def test_other_variables_are_not_mistaken_for_home(self):
        with mock.patch.dict(os.environ, {"HOMEDIR": "/elsewhere"}):
            self.assertEqual(dispatches.expand_dir("$HOMEDIR/x"), Path("/elsewhere/x").resolve())

    def test_brief_under_a_home_variable_is_read(self):
        (self.home / "x/BRIEF.md").write_text("brief", encoding="utf-8")
        self.assertEqual(dispatches.read_brief(str(self.home / "x"), "BRIEF.md"), "brief")
        self.assertEqual(dispatches.read_brief(None, "$HOME/x/BRIEF.md"), "brief")


if __name__ == "__main__":
    unittest.main()
