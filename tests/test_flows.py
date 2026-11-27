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

REAL_ASK = pm.ask  # setUp swaps in a fake PM; the PM-failure tests need the real retry loop
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
        pm.ask = lambda key, message, validate, record=None: self.answers.pop(0)
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

    def mention(self, text: str):
        """A main-thread message the judge is sure belongs to ses_x."""
        message = mock.Mock(seq=7, text=text, time=10, role="user")
        answer = {"choice": "ses_x", "belongs": 0.95, "new_work": 0.1}
        with mock.patch.object(daemon.relevance, "attribute", lambda *a: (answer, None)), \
                mock.patch.object(daemon.relevance, "should_move", lambda *a: True):
            self.docket.attribute(message, [], [self.docket.delegation("ses_x")])

    def rounds(self):
        return self.docket.delegation("ses_x")["rounds"]

    def test_mention_relay_needs_confirmation(self):
        self.answers.append({"action": "relay", "message": "do more"})
        self.mention("a chat message")
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(json.loads(card["body"])["pending_relay"], "do more")
        self.assertEqual(self.sent, [])
        self.docket.decide(card["id"], {"relay": "yes"}, "")
        self.assertEqual(self.sent, ["do more"])
        self.assertEqual(self.rounds(), 1)

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

    # ---------- the round budget: every time a person takes part is one round ----------

    def test_three_answers_spend_three_rounds_then_the_limit(self):
        seen = []
        for i in range(3):
            self.answers.append({"action": "card", "card": dict(CARD, kind="question", progress=f"q{i}")})
            card = ledger.open_card(self.db, "ses_x")["id"] if i else self.awaiting_card()
            self.docket.decide(card, {}, f"answer {i}")
            seen.append(self.rounds())
        self.assertEqual(seen, [1, 2, 3])
        row = self.docket.delegation("ses_x")
        self.assertEqual(row["state"], "limit")
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(card["kind"], "limit")
        self.assertIn("3", json.loads(card["body"])["progress"])
        # The PM's third card was refused, and nothing more can be answered.
        self.assertEqual(self.db.execute("select count(*) from events where delegation_id = 'ses_x' and kind = 'card:blocked'").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.docket.decide(card["id"], {}, "one more")
        self.assertEqual(self.rounds(), 3)
        self.assertEqual(ledger.snapshot(self.db)[0]["state"], "limit")

    def test_show_card_is_refused_at_the_limit(self):
        self.db.execute("update delegations set rounds = 3 where id = 'ses_x'")
        self.assertIsNone(self.docket.show_card("ses_x", "question", dict(CARD, progress="may I ask?")))
        event = self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = 'card:blocked'").fetchone()
        self.assertEqual(json.loads(event[0])["progress"], "may I ask?")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "limit")
        self.assertEqual(self.docket.delegation("ses_x")["state"], "limit")
        # A permission request is a card too: refused the same way, and the state stays at the limit.
        self.docket.permission_card("ses_x", {"id": "per_9", "action": "shell", "resources": ["rm -rf /tmp/x"]})
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "limit")
        self.assertEqual(self.db.execute("select count(*) from events where delegation_id = 'ses_x' and kind = 'card:blocked'").fetchone()[0], 2)
        ledger.set_state(self.db, "ses_x", "running")
        self.assertEqual(self.docket.delegation("ses_x")["state"], "limit")  # only closing or taking over leaves it

    def test_an_answer_that_relays_is_one_round(self):
        self.answers.append({"action": "relay", "message": "go on"})
        self.docket.decide(self.awaiting_card(), {}, "go")
        self.assertEqual(self.sent, ["go on"])
        self.assertEqual(self.rounds(), 1)
        self.assertEqual([r["kind"] for r in self.db.execute("select kind from events where delegation_id = 'ses_x' and kind in ('round', 'relayed') order by id")], ["round", "relayed"])

    def test_a_chat_mention_is_one_round(self):
        self.answers.append({"action": "relay", "message": "from chat"})
        self.mention("also do Y")
        self.assertEqual(self.rounds(), 1)
        self.docket.decide(ledger.open_card(self.db, "ses_x")["id"], {"relay": "yes"}, "")
        self.assertEqual(self.sent, ["from chat"])
        self.assertEqual(self.rounds(), 1)  # confirming it is the same act of taking part
        # A mention that answers an open card spends that card with it, still one round.
        self.awaiting_card()
        self.answers.append({"action": "card", "card": dict(CARD, kind="question")})
        self.mention("about that card")
        self.assertEqual(self.rounds(), 2)

    def test_replay_spends_nothing(self):
        self.answers.append({"action": "relay", "message": "first"})
        card = self.awaiting_card()
        self.docket.decide(card, {"d1": "a"}, "go")
        self.answers.append({"action": "relay", "message": "again"})
        self.docket.replay(card)
        self.assertEqual(self.sent, ["first", "again"])
        self.assertEqual(self.rounds(), 1)

    def test_answering_a_permission_card_is_one_round(self):
        engines.OpenCode.answer_permission = lambda engine, handoff, request, decision: None
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "shell", "resources": ["rm -rf /tmp/build"]})
        self.docket.decide(ledger.open_card(self.db, "ses_x")["id"], {"permission": "reject"}, "")
        self.assertEqual(self.rounds(), 1)
        self.assertEqual(self.sent, [])
        # A double click on the same card is not a second round.
        with self.assertRaises(ValueError):
            self.docket.decide(self.db.execute("select max(id) from cards").fetchone()[0], {"permission": "reject"}, "")
        self.assertEqual(self.rounds(), 1)

    def test_the_third_answer_is_carried_out_then_wrapped_up(self):
        self.db.execute("update delegations set rounds = 2 where id = 'ses_x'")
        self.answers.append({"action": "relay", "message": "last one"})
        self.docket.decide(self.awaiting_card(), {}, "finish it")
        self.assertEqual(self.sent, ["last one"])  # the last round is honoured, not refused
        self.assertEqual(self.docket.delegation("ses_x")["state"], "limit")
        # The run ends: the PM writes a handoff note, not a question.
        self.answers.append({"action": "close", "conclusion": "half done", "remaining": ["docs", "tests"]})
        self.docket.on_run_end("ses_x", "succeeded", "ok")
        card = ledger.open_card(self.db, "ses_x")
        self.assertEqual(card["kind"], "limit")
        self.assertIn("docs", json.loads(card["body"])["progress"])
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.docket.close("ses_x", "")
        row = self.docket.delegation("ses_x")
        self.assertEqual(row["state"], "closed")
        conclusion = json.loads(self.db.execute("select body from cards where kind = 'closed'").fetchone()[0])["conclusion"]
        self.assertIn("half done", conclusion)
        self.assertIn("tests", conclusion)

    # ---------- the docket's own failures never close a handoff ----------

    def real_pm(self, replies):
        """The real retry loop with a scripted PM; returns the models each try asked for."""
        models = []

        def run(key, prompt, timeout=None, model=None):
            models.append(model)
            return replies.pop(0)

        for patch in (mock.patch.object(pm, "ask", REAL_ASK), mock.patch.object(pm, "run", run),
                      mock.patch.object(pm, "MODEL", "main/model"), mock.patch.object(pm, "FALLBACK", "fallback/model")):
            patch.start()
            self.addCleanup(patch.stop)
        self.docket.submit = lambda did, job, *args: daemon.Docket.submit(self.docket, did, job, *args).result()
        return models

    def test_json_is_found_inside_prose(self):
        card = json.dumps(CARD, ensure_ascii=False)
        for text in (
            f"好的，核对完了。\n{card}\n以上就是简报。",
            f"Here it is:\n```json\n{card}\n```\nDone.",
            json.dumps(card),  # the whole object quoted once more as a string
            'Note {not json} then {"kind": "report", "progress": "p", "note": "a } in a string"}',
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(pm.extract_json(text)["kind"], "report")
        value, errors = pm.pick('first {"action": "nope"} then {"action": "close", "conclusion": "ok"}', pm.validate_action)
        self.assertEqual((value["action"], errors), ("close", []))

    def test_a_reply_without_json_is_retried_not_closed(self):
        models = self.real_pm(["I checked everything and it looks fine.", json.dumps(CARD)])
        self.docket.on_run_end("ses_x", "succeeded", "done")
        self.assertEqual(models, ["main/model", "main/model"])
        row = self.docket.delegation("ses_x")
        self.assertEqual(row["state"], "awaiting")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "report")
        event = self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = 'pm:invalid'").fetchone()
        self.assertEqual(json.loads(event[0])["raw"], "I checked everything and it looks fine.")

    def test_a_card_with_paths_and_too_many_words_is_asked_again(self):
        sloppy = {"kind": "question",
                  "progress": "健身 FitLog v0.1 已在 项目/fitness-app 建好工程，tools/build.sh 能编译通过，模拟器跑起来了，但真机安装还需要你在 Config/Signing.local.xcconfig 里填 Team ID 并按 docs/INSTALL_DEVICE.md 操作。",
                  "decisions": [{"id": "d1", "question": "签名怎么处理？", "options": [
                      {"id": "a", "label": "我来填 DEVELOPMENT_TEAM", "recommended": True}, {"id": "b", "label": "先不装真机", "recommended": False}]}]}
        clean = {"kind": "question", "progress": "健身记录 App 第一版做好了，模拟器上能用。", "next": "装到你手机上还差签名配置，要我先补上吗？",
                 "decisions": [{"id": "d1", "question": "签名配置怎么处理？", "options": [
                     {"id": "a", "label": "你来补", "recommended": True}, {"id": "b", "label": "先不装手机", "recommended": False}]}]}
        prompts = []
        self.real_pm([json.dumps(sloppy, ensure_ascii=False), json.dumps(clean, ensure_ascii=False)])
        run = pm.run
        pm.run = lambda key, prompt, timeout=None, model=None: (prompts.append(prompt), run(key, prompt))[1]
        with mock.patch.object(pm, "LOCALE", "zh"):
            self.docket.on_run_end("ses_x", "succeeded", "done")
        retry = prompts[1]
        for said in ("/fitness-app", "tools/build.sh", "Config/Signing.local.xcconfig", "docs/INSTALL_DEVICE.md", "DEVELOPMENT_TEAM",
                     "over the limit of 40", "over the limit of 8", "a file path"):
            self.assertIn(said, retry)
        self.assertIn(sloppy["progress"], retry)  # the offending sentence itself, not just "too long"
        body = json.loads(ledger.open_card(self.db, "ses_x")["body"])
        self.assertEqual(body["progress"], clean["progress"])
        shown = next(d for d in ledger.snapshot(self.db) if d["id"] == "ses_x")["card"]
        self.assertEqual(shown["progress"], "健身记录 App 第一版做好了，模拟器上能用。装到你手机上还差签名配置，要我先补上吗？")
        self.assertNotIn("next", shown)

    def test_three_bad_replies_become_a_failure_card_not_a_closed_handoff(self):
        models = self.real_pm(["prose one", "prose two", "prose three"])
        self.docket.on_run_end("ses_x", "succeeded", "done")
        self.assertEqual(models, ["main/model", "main/model", "fallback/model"])
        row = self.docket.delegation("ses_x")
        self.assertNotEqual(row["state"], "closed")
        self.assertEqual(row["state"], "awaiting")
        card = ledger.open_card(self.db, "ses_x")
        body = json.loads(card["body"])
        self.assertEqual(card["kind"], "failure")
        self.assertTrue(body["docket_fault"])
        self.assertIn("不是任务失败" if self.docket.locale == "zh" else "not the task", body["progress"])
        self.assertIn("prose three", body["progress"])
        raws = [json.loads(d)["raw"] for (d,) in self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = 'pm:invalid' order by id")]
        self.assertEqual(raws, ["prose one", "prose two", "prose three"])
        self.assertNotIn("pm_retry", ledger.snapshot(self.db)[0]["card"])
        # The retry button runs the PM again and costs no round.
        self.real_pm([json.dumps(CARD)])
        self.docket.decide(card["id"], {"retry": "yes"}, "")
        self.assertEqual(self.rounds(), 0)
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "report")

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
        self.assertEqual(self.sent, [])  # a permission answer goes to OpenCode, not to the PM

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

    # ---------- the card queue never locks up ----------

    def state(self):
        return self.docket.delegation("ses_x")["state"]

    def queued(self):
        return self.db.execute("select count(*) from cards where delegation_id='ses_x' and resolved_at is null and queued=1").fetchone()[0]

    def test_a_queued_card_leaves_the_handoff_awaiting_not_pending(self):
        self.docket.show_card("ses_x", "question", dict(CARD, progress="first"))
        ledger.set_state(self.db, "ses_x", "pending")  # a PM job starts, as on_run_end does
        self.docket.show_card("ses_x", "report", dict(CARD, progress="second"))
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(self.queued(), 1)
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "first")

    def test_a_new_question_supersedes_the_old_one(self):
        old = self.docket.show_card("ses_x", "question", dict(CARD, progress="old"))
        new = self.docket.show_card("ses_x", "question", dict(CARD, progress="new"))
        card = self.db.execute("select * from cards where id = ?", (old,)).fetchone()
        self.assertIsNotNone(card["resolved_at"])
        self.assertEqual(json.loads(card["resolution"]), {"action": "superseded"})
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], new)
        self.assertEqual(self.queued(), 0)
        self.assertEqual(self.state(), "awaiting")

    def test_a_question_taking_the_place_of_the_shown_one_jumps_the_queue(self):
        self.docket.show_card("ses_x", "question", dict(CARD, progress="old"))
        self.docket.show_card("ses_x", "report", dict(CARD, progress="report"))
        new = self.docket.show_card("ses_x", "question", dict(CARD, progress="new"))
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], new)
        self.assertEqual(self.queued(), 1)  # the report still waits behind it

    def test_reports_failures_and_permission_requests_are_never_superseded(self):
        report = self.docket.show_card("ses_x", "report", dict(CARD, progress="r"))
        self.docket.permission_card("ses_x", {"id": "per_1", "action": "shell", "resources": ["rm -rf /tmp/x"]})
        self.docket.show_card("ses_x", "question", dict(CARD, progress="q"))
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], report)
        self.assertEqual(self.queued(), 2)
        unresolved = self.db.execute("select count(*) from cards where delegation_id='ses_x' and resolved_at is null").fetchone()[0]
        self.assertEqual(unresolved, 3)

    def test_sweep_supersedes_an_orphan_and_lifts_the_queue(self):
        orphan = ledger.add_card(self.db, "ses_x", "question", dict(CARD, progress="orphan"))
        waiting = ledger.add_card(self.db, "ses_x", "report", dict(CARD, progress="waiting"), queued=True)
        self.db.execute("update cards set created_at = ? where id = ?", (ledger.now() - 3 * 3600_000, orphan))
        ledger.set_state(self.db, "ses_x", "pending")
        self.docket.sweep_orphans()
        card = self.db.execute("select * from cards where id = ?", (orphan,)).fetchone()
        self.assertEqual(json.loads(card["resolution"])["action"], "superseded")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], waiting)
        self.assertEqual(self.state(), "awaiting")
        # The lifted card can be answered and the handoff closed.
        self.answers.append({"action": "close", "conclusion": "done"})
        self.docket.decide(waiting, {}, "close it")
        self.assertEqual(self.state(), "closed")

    def test_sweep_leaves_cards_that_are_on_show_or_being_written(self):
        old = ledger.add_card(self.db, "ses_x", "question", dict(CARD, progress="old"))
        self.db.execute("update cards set created_at = ? where id = ?", (ledger.now() - 3 * 3600_000, old))
        ledger.set_state(self.db, "ses_x", "awaiting")  # on the panel: the user simply has not answered yet
        self.docket.sweep_orphans()
        self.assertIsNone(self.db.execute("select resolved_at from cards where id = ?", (old,)).fetchone()[0])
        ledger.set_state(self.db, "ses_x", "pending")
        self.docket.jobs["ses_x"] = 1  # a PM job is on its way
        self.docket.sweep_orphans()
        self.assertIsNone(self.db.execute("select resolved_at from cards where id = ?", (old,)).fetchone()[0])
        self.docket.jobs["ses_x"] = 0
        self.docket.sweep_orphans()
        self.assertIsNotNone(self.db.execute("select resolved_at from cards where id = ?", (old,)).fetchone()[0])
        self.assertEqual(self.state(), "running")  # nothing left to show

    # ---------- an awaiting card whose session has ended ----------

    def stale_awaiting(self, minutes: int = 40) -> int:
        card_id = self.awaiting_card()
        self.db.execute("update cards set created_at = ? where id = ?", (ledger.now() - minutes * 60_000, card_id))
        return card_id

    def sweep_awaiting_with(self, session: dict):
        class Service:
            def session(self, sid):
                return session

        daemon.Service = Service
        self.docket.sweep_awaiting()

    def test_sweep_awaiting_expires_a_card_whose_session_ended(self):
        card = self.stale_awaiting()
        self.sweep_awaiting_with({"outcome": "succeeded", "time": {"busy": 1, "idle": 2}})
        row = self.db.execute("select * from cards where id = ?", (card,)).fetchone()
        self.assertEqual(json.loads(row["resolution"]), {"action": "auto-expired", "by": "sweep", "note": "session ended, no reply"})
        self.assertEqual(self.state(), "closed")
        closed = self.db.execute("select * from cards where delegation_id = 'ses_x' and kind = 'closed'").fetchone()
        body = json.loads(closed["body"])
        self.assertEqual(body["session_outcome"], "succeeded")
        self.assertEqual(body["auto_expired"]["reason"], "session ended, no reply")
        event = json.loads(self.db.execute(
            "select detail from events where delegation_id = 'ses_x' and kind = 'sweep:auto-expired'"
        ).fetchone()[0])
        self.assertEqual((event["card"], event["outcome"], event["cards"]), (card, "succeeded", [card]))
        shown = next(d for d in ledger.snapshot(self.db) if d["id"] == "ses_x")
        self.assertEqual(shown["state"], "closed")
        self.assertEqual(shown["card"]["kind"], "closed")

    def test_sweep_awaiting_leaves_a_fresh_card_alone(self):
        card = self.stale_awaiting(minutes=5)
        self.sweep_awaiting_with({"outcome": "succeeded", "time": {"busy": 1, "idle": 2}})
        self.assertIsNone(self.db.execute("select resolved_at from cards where id = ?", (card,)).fetchone()[0])
        self.assertEqual(self.state(), "awaiting")

    def test_sweep_awaiting_leaves_a_session_that_is_still_running(self):
        card = self.stale_awaiting()
        self.sweep_awaiting_with({"outcome": None, "time": {"busy": 3, "idle": 2}})
        self.assertIsNone(self.db.execute("select resolved_at from cards where id = ?", (card,)).fetchone()[0])
        # The session is over, but a PM job of this handoff is still in flight: still no touch.
        self.docket.jobs["ses_x"] = 1
        self.sweep_awaiting_with({"outcome": "succeeded", "time": {"busy": 1, "idle": 2}})
        self.assertIsNone(self.db.execute("select resolved_at from cards where id = ?", (card,)).fetchone()[0])
        self.docket.jobs["ses_x"] = 0
        self.sweep_awaiting_with({"outcome": "succeeded", "time": {"busy": 1, "idle": 2}})
        self.assertEqual(self.state(), "closed")

    def test_sweep_awaiting_marks_an_interrupted_session_as_such(self):
        card = self.stale_awaiting()
        self.sweep_awaiting_with({"outcome": "interrupted", "time": {"busy": 1, "idle": 2}})
        self.assertEqual(self.state(), "closed")
        closed = self.db.execute("select * from cards where delegation_id = 'ses_x' and kind = 'closed'").fetchone()
        body = json.loads(closed["body"])
        self.assertEqual(body["session_outcome"], "interrupted")
        self.assertIn("interrupted", body["progress"])
        event = json.loads(self.db.execute(
            "select detail from events where delegation_id = 'ses_x' and kind = 'sweep:auto-expired'"
        ).fetchone()[0])
        self.assertEqual((event["card"], event["outcome"]), (card, "interrupted"))

    def test_answering_shows_the_next_card_in_order(self):
        first = self.docket.show_card("ses_x", "report", dict(CARD, progress="first"))
        second = self.docket.show_card("ses_x", "report", dict(CARD, progress="second"))
        self.assertEqual(self.state(), "awaiting")
        # The first answer goes to OpenCode; the second report, waiting behind it, comes up.
        self.answers.append({"action": "relay", "message": "a"})
        self.docket.decide(first, {}, "go on")
        self.assertEqual(self.sent, ["a"])
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], second)
        self.assertEqual(self.state(), "awaiting")
        self.answers.append({"action": "relay", "message": "b"})
        self.docket.decide(second, {}, "fine")
        self.assertEqual(self.sent, ["a", "b"])
        self.assertEqual(self.state(), "running")
        self.assertIsNone(ledger.open_card(self.db, "ses_x"))
        # The run ends: pending while the PM writes, awaiting once its card is up.
        self.answers.append(dict(CARD, progress="done"))
        engines.OpenCode.last_reply = lambda engine, handoff: "finished"
        self.docket.on_run_end("ses_x", "succeeded")
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "done")

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
                return {"title": parsed["title"], "time": {"created": 1}, "location": {}}

        daemon.Service = Session
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(daemon.registry, "find_dispatch", lambda *a: found), \
                mock.patch.object(daemon.registry, "build_context", lambda *a: []):
            self.docket.maybe_register("ses_new", attempts=1)
        return self.docket.delegation("ses_new")

    # ---------- handing a taken-over handoff back ----------

    def take_over(self):
        self.docket.takeover("ses_x")
        self.assertEqual(self.state(), "taken_over")

    def handbacks(self):
        return self.docket.delegation("ses_x")["handbacks"]

    def test_a_taken_over_handoff_can_be_handed_back(self):
        self.answers.append({"action": "relay", "message": "go on"})
        self.docket.decide(self.awaiting_card(), {}, "go")
        self.take_over()
        # While taken over the docket asks nothing: a run ending in the user's own session writes no card.
        self.docket.on_run_end("ses_x", "succeeded")
        self.assertEqual(self.state(), "taken_over")
        seen = []
        pm.ask = lambda key, message, validate, record=None: seen.append(message) or self.answers.pop(0)
        self.answers.append({"action": "relay", "message": "next piece"})
        self.assertEqual(self.docket.handback("ses_x"), "pending")
        self.assertIn("handed it back", seen[0])
        self.assertEqual(self.sent, ["go on", "next piece"])
        self.assertEqual(self.state(), "running")
        self.assertEqual((self.rounds(), self.handbacks()), (1, 1))  # rounds kept, not refilled
        self.assertEqual(self.docket.relayed_count("ses_x"), 1)  # the handback's relay is not one an answer paid for
        self.assertEqual(self.db.execute("select count(*) from events where delegation_id = 'ses_x' and kind = 'handback'").fetchone()[0], 1)
        self.assertEqual(ledger.snapshot(self.db)[0]["handbacks"], 1)
        # Back under the docket's care: the next run end is a card again.
        self.answers.append(dict(CARD, progress="piece done"))
        self.docket.on_run_end("ses_x", "succeeded", "ok")
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "piece done")

    def test_a_closed_handoff_cannot_be_handed_back(self):
        self.docket.finish("ses_x", "done")
        self.docket.takeover("ses_x")  # taking over a closed handoff leaves it closed
        with self.assertRaises(ValueError):
            self.docket.handback("ses_x")
        self.assertEqual((self.state(), self.handbacks()), ("closed", 0))
        with self.assertRaises(ValueError):
            self.docket.handback("ses_nope")

    def test_handing_back_twice_is_refused_and_counted_once(self):
        with self.assertRaises(ValueError):  # never taken over
            self.docket.handback("ses_x")
        self.take_over()
        self.answers.append({"action": "card", "card": dict(CARD, kind="question", progress="which way?")})
        self.docket.handback("ses_x")
        self.assertEqual(self.state(), "awaiting")
        with self.assertRaises(ValueError) as refused:
            self.docket.handback("ses_x")
        self.assertIn("taken over", str(refused.exception))
        self.assertEqual(self.handbacks(), 1)
        # Taking over again and handing back again works, and is counted.
        self.take_over()
        self.answers.append({"action": "relay", "message": "again"})
        self.docket.handback("ses_x")
        self.assertEqual(self.handbacks(), 2)

    def test_a_card_left_waiting_goes_up_on_handback(self):
        shown = self.docket.show_card("ses_x", "report", dict(CARD, progress="shown"))
        waiting = self.docket.show_card("ses_x", "report", dict(CARD, progress="waiting"))
        self.take_over()  # resolves the card on show; the one behind it is still queued
        self.answers.append({"action": "relay", "message": "carry on"})
        self.assertEqual(self.docket.handback("ses_x"), "awaiting")
        self.assertNotEqual(ledger.open_card(self.db, "ses_x")["id"], shown)
        self.assertEqual(ledger.open_card(self.db, "ses_x")["id"], waiting)
        self.assertEqual(self.sent, ["carry on"])
        self.assertEqual(self.state(), "awaiting")  # the relay does not hide the card on show

    def test_after_handback_the_limit_and_the_sweep_still_hold(self):
        self.db.execute("update delegations set rounds = 3 where id = 'ses_x'")
        self.docket.enter_limit("ses_x")
        self.take_over()  # taking over is one of the two ways out of the limit
        # The PM may not ask again: its card is refused, the limit card comes back, rounds stay at 3.
        self.answers.append({"action": "card", "card": dict(CARD, kind="question", progress="one more?")})
        self.docket.handback("ses_x")
        self.assertEqual(self.state(), "limit")
        self.assertEqual(ledger.open_card(self.db, "ses_x")["kind"], "limit")
        self.assertEqual(self.rounds(), 3)
        with self.assertRaises(ValueError):
            self.docket.decide(ledger.open_card(self.db, "ses_x")["id"], {}, "please")
        # A handed-back handoff with an old card nobody can see is swept like any other.
        ledger.add_delegation(self.db, id="ses_y", title="y", directory=None, source_key="k", dispatch_seq=1, dispatched_at=0, brief="", context=[])
        self.docket.takeover("ses_y")
        self.answers.append({"action": "relay", "message": "y on"})
        self.docket.handback("ses_y")
        self.assertEqual(self.docket.delegation("ses_y")["state"], "running")
        old = ledger.add_card(self.db, "ses_y", "question", dict(CARD, progress="old"))
        self.db.execute("update cards set created_at = ? where id = ?", (ledger.now() - 3 * 3600_000, old))
        self.docket.sweep_orphans()
        self.assertIsNotNone(self.db.execute("select resolved_at from cards where id = ?", (old,)).fetchone()[0])

    def test_a_pm_reply_after_another_takeover_is_dropped(self):
        self.take_over()

        def ask(key, message, validate, record=None):
            self.docket.takeover("ses_x")  # the user takes it back while the PM is thinking
            return {"action": "relay", "message": "too late"}

        pm.ask = ask
        self.docket.handback("ses_x")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state(), "taken_over")

    def test_handback_wake_forbids_closing_and_deciding_reserved_items(self):
        brief = "# 任务\n1. 改排序\n2. sources 分层：留给你定\n3. 跑测试"
        message = pm.handback_wake("done", 1, brief)
        self.assertIn("Do not close on your own", message)
        self.assertIn("Only close when the user has said to wrap up", message)
        self.assertIn("stop and put it on a card. Do not decide it for the user", message)
        self.assertIn("- 2. sources 分层：留给你定", message)
        self.assertIn('"do"', message)
        self.assertIn('"ask the user"', message)
        self.assertNotIn("照做", message)
        self.assertNotIn("出卡问", message)
        self.assertEqual(pm.reserved_items(brief), ["2. sources 分层：留给你定"])
        validate = pm.validate_handback(1, brief)
        self.assertTrue(validate({"action": "close", "conclusion": "done", "checklist": ["x → ask the user"]}))
        self.assertTrue(validate(dict({"action": "card", "card": CARD})))  # reserved items need a checklist
        self.assertEqual(validate({"action": "card", "card": CARD, "checklist": ["sources 分层 → ask the user"]}), [])
        # With the budget spent a close is the only way out, so it stays allowed.
        self.assertEqual(pm.validate_handback(3, None)({"action": "close", "conclusion": "done"}), [])

    def test_a_handback_close_is_retried_as_a_card(self):
        self.db.execute("update delegations set brief = '2. sources 分层：待用户决定' where id = 'ses_x'")
        self.take_over()
        replies = [{"action": "close", "conclusion": "all done"},
                   {"action": "card", "card": dict(CARD, progress="this piece is done; go on or wrap up?"), "checklist": ["sources 分层 → ask the user"]}]

        def ask(key, message, validate, record=None):  # the real loop's shape: first passing reply wins
            for reply in replies:
                if not validate(reply):
                    return reply
            raise AssertionError("no reply passed")

        pm.ask = ask
        self.docket.handback("ses_x")
        self.assertEqual(self.state(), "awaiting")
        self.assertIn("go on or wrap up", json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"])
        event = self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = 'pm:checklist'").fetchone()
        self.assertEqual(json.loads(event[0])["items"], ["sources 分层 → ask the user"])

    # ---------- the panel's order ----------

    def test_ended_handoffs_mix_by_time(self):
        for sid, state, age in (("ses_a", "taken_over", 3), ("ses_b", "closed", 1), ("ses_c", "closed", 2)):
            ledger.add_delegation(self.db, id=sid, title=sid, directory=None, source_key="k", dispatch_seq=1, dispatched_at=0, brief="", context=[])
            self.db.execute("update delegations set state = ?, updated_at = ? where id = ?", (state, ledger.now() - age * 3600_000, sid))
        order = [d["id"] for d in ledger.snapshot(self.db)]
        self.assertEqual(order, ["ses_x", "ses_b", "ses_c", "ses_a"])  # the live one first, then newest ended first

    # ---------- reconcile after a restart ----------

    def reconcile_with(self, sessions: dict):
        class Service:
            def recent_sessions(self, limit=40):
                return []

            def session(self, sid):
                return sessions[sid]

        daemon.Service = Service
        daemon.MEMORY_DIR = Path(tempfile.mkdtemp())
        self.docket.reconcile()

    def test_reconcile_wakes_a_recent_pending(self):
        ledger.set_state(self.db, "ses_x", "pending")
        self.answers.append(dict(CARD, progress="picked up"))
        self.reconcile_with({"ses_x": {"time": {"busy": 1, "idle": 2}, "outcome": "succeeded"}})
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "picked up")

    def test_reconcile_closes_an_overdue_pending(self):
        ledger.set_state(self.db, "ses_x", "pending")
        old = ledger.now() - 48 * 3600_000
        self.db.execute("update delegations set updated_at = ?, dispatched_at = ? where id = 'ses_x'", (old, old - 1000))
        self.reconcile_with({"ses_x": {"time": {"busy": 1, "idle": 2}, "outcome": "succeeded"}})
        self.assertEqual(self.state(), "closed")
        self.assertEqual(self.answers, [])  # the PM was never woken
        card = self.db.execute("select * from cards where delegation_id = 'ses_x' order by id desc limit 1").fetchone()
        self.assertEqual(card["kind"], "closed")
        self.assertIn("48", json.loads(card["body"])["conclusion"])
        event = json.loads(self.db.execute("select detail from events where delegation_id = 'ses_x' and kind = 'orphaned'").fetchone()[0])
        self.assertEqual((event["dispatched_at"], event["updated_at"], event["hours"]), (old - 1000, old, 48))

    def test_reconcile_still_resumes_an_old_running(self):
        self.db.execute("update delegations set updated_at = ? where id = 'ses_x'", (ledger.now() - 48 * 3600_000,))
        self.assertEqual(self.state(), "running")
        self.answers.append(dict(CARD, progress="finished while down"))
        self.reconcile_with({"ses_x": {"time": {"busy": 1, "idle": ledger.now()}, "outcome": "succeeded"}})
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(json.loads(ledger.open_card(self.db, "ses_x")["body"])["progress"], "finished while down")

    def test_home_variable_in_dispatch_directory_is_stored_expanded(self):
        home = Path(tempfile.mkdtemp()).resolve()
        (home / "Desktop/项目/proj").mkdir(parents=True)
        row = self.register('cd "$HOME/Desktop/项目/proj" && opencode-dispatch --detach --auto --title "T" "go"', home)
        self.assertEqual(row["directory"], str(home / "Desktop/项目/proj"))
        self.assertEqual(row["state"], "running")
        self.assertIsNone(ledger.open_card(self.db, "ses_new"))

    def test_missing_dispatch_directory_fails_visibly(self):
        home = Path(tempfile.mkdtemp()).resolve()
        row = self.register('cd "$HOME/nope" && opencode-dispatch --detach --title "T" "go"', home)
        self.assertIsNone(row["directory"])  # nothing unrunnable is stored
        self.assertEqual(row["state"], "awaiting")  # a technical fault is shown, never closed for the user
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
        return dispatches.parse(f'cd {cd} && opencode-dispatch --detach --title "T" "go"')["directory"]

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
