# -*- coding: utf-8 -*-
"""Cards nobody answers on the panel: settled in the main chat, woken for once, expired at the cap.

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)

A synthetic OpenClaw transcript, a scripted judge and a scripted PM; nothing real is opened.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOCKET_HOME", tempfile.mkdtemp(prefix="docket-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import config, daemon, engines, i18n, ledger, pm  # noqa: E402
from judge import backend, relevance, transcript  # noqa: E402

MAIN = "agent:main:main"
CHAT = "chat-session-1"
HOUR = 3600_000
QUESTION = {
    "kind": "question",
    "progress": "两种排序都能做，等你定。",
    "decisions": [{"id": "d1", "question": "期刊列表按什么排序？", "options": [
        {"id": "a", "label": "按影响因子", "recommended": True},
        {"id": "b", "label": "按发表时间", "recommended": False},
    ]}],
    "decided_for_you": [],
}


def user_event(text: str, role: str = "user") -> str:
    return json.dumps({"message": {"role": role, "content": [{"type": "text", "text": text}]}}, ensure_ascii=False)


class Judge:
    """Answers card_status's questions as told; records what it was asked."""

    def __init__(self):
        self.asked = []
        self.status, self.message, self.picks, self.p = "open", "none", {}, 0.97

    def __call__(self, state, questions):
        self.asked.append((state, questions))
        answers = {}
        for qid, question in questions.items():
            pick = {"status": self.status, "message": self.message}.get(qid) or self.picks.get(qid.removeprefix("pick_"), "none")
            options = list(question["criteria"])
            probabilities = {o: (self.p if o == pick else (1 - self.p) / (len(options) - 1)) for o in options}
            answers[qid] = {"choice": pick, "probabilities": probabilities, "confidence": self.p}
        return {"answers": answers, "usage": {"input_tokens": 1}}


class Stale(unittest.TestCase):
    def setUp(self):
        self.sent, self.replies, self.wakes = [], [], []
        sent = self.sent

        class Service:
            busy = False

            def prompt(self, sid, text):
                sent.append(text)

            def session(self, sid):
                return {"outcome": None, "time": {"busy": 3, "idle": 2} if Service.busy else {"busy": 1, "idle": 2}}

        self.service = Service
        for target, name, value in ((daemon, "Service", Service), (engines, "Service", Service)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.object(engines.OpenCode, "resume", lambda engine, handoff, text, model=None: sent.append(text)),
            mock.patch.object(pm, "ask", self.pm),
            mock.patch.object(daemon, "MEMORY_DIR", Path(tempfile.mkdtemp())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The transcript: a real SQLite file with OpenClaw's two tables, the main chat in it.
        self.chat = Path(tempfile.mkdtemp()) / "agent.sqlite"
        conn = sqlite3.connect(self.chat)
        conn.executescript("create table transcript_events (session_id text, seq integer, created_at integer, event_json text);"
                           "create table session_windows (session_key text, session_id text);")
        conn.execute("insert into session_windows values (?, ?)", (MAIN, CHAT))
        conn.commit()
        conn.close()
        self.seq = 0
        self.judge = Judge()
        for patcher in (
            mock.patch.object(daemon, "openclaw_connect", lambda: transcript.connect(self.chat)),
            mock.patch.object(backend, "available", lambda: True),
            mock.patch.object(backend, "ask", self.judge),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        daemon.ledger.DB = Path(tempfile.mkdtemp()) / "docket.sqlite"
        ledger.connect.__defaults__ = (daemon.ledger.DB,)
        self.docket = daemon.Docket()
        self.docket.settings = dict(config.DEFAULTS)  # whatever config.json the test machine has, use the defaults
        self.docket.locale, self.docket.t = "zh", i18n.Strings("zh")
        self.docket.stale_since = 0  # the fixtures' cards are older than this test's docket
        self.docket.submit = lambda did, job, *args: job(*args)
        self.db = self.docket.db()
        ledger.add_delegation(self.db, id="ses_s", title="期刊排序", directory=None, source_key=MAIN, dispatch_seq=1, dispatched_at=0, brief="改期刊列表的排序", context=[])

    def pm(self, key, message, validate, record=None):
        self.wakes.append(message)
        reply = self.replies.pop(0)
        self.assertEqual(validate(reply), [], reply)
        return reply

    def card(self, age_hours: float, body: dict = QUESTION) -> int:
        card_id = self.docket.show_card("ses_s", body["kind"], dict(body))
        self.age(card_id, age_hours)
        return card_id

    def age(self, card_id: int, hours: float):
        """Make a card as old as if it had been on show for `hours`."""
        at = ledger.now() - int(hours * HOUR)
        self.db.execute("update cards set created_at = ?, shown_at = ? where id = ?", (at, at, card_id))

    def say(self, text: str, ago_minutes: float = 1, role: str = "user") -> int:
        self.seq += 1
        conn = sqlite3.connect(self.chat)
        conn.execute("insert into transcript_events values (?, ?, ?, ?)", (CHAT, self.seq, ledger.now() - int(ago_minutes * 60_000), user_event(text, role)))
        conn.commit()
        conn.close()
        return self.seq

    def row(self, card_id: int):
        return self.db.execute("select * from cards where id = ?", (card_id,)).fetchone()

    def state(self) -> str:
        return self.docket.delegation("ses_s")["state"]

    def events(self, kind: str) -> list:
        return [json.loads(d or "null") for (d,) in self.db.execute("select detail from events where kind = ? order by id", (kind,))]

    # ---------- settled in the main chat ----------

    def test_answered_in_chat_closes_with_the_users_words(self):
        card = self.card(1)
        seq = self.say("期刊那个就按发表时间排吧，影响因子不用管")
        self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq}", {"d1": "b"}
        self.replies.append({"action": "close", "conclusion": "你在主会话里定了按发表时间排序，已结案。", "basis": "用户说「就按发表时间排吧」"})
        self.docket.sweep_stale()
        resolution = json.loads(self.row(card)["resolution"])
        self.assertEqual(resolution["action"], "answered-in-chat")
        self.assertEqual(resolution["by"], "chat-match")
        self.assertEqual(resolution["quote"], "期刊那个就按发表时间排吧，影响因子不用管")
        self.assertEqual((resolution["seq"], resolution["choices"], resolution["settled"]), (seq, {"d1": "b"}, "close"))
        self.assertEqual(resolution["basis"], "用户说「就按发表时间排吧」")
        self.assertEqual(self.docket.delegation("ses_s")["rounds"], 1)  # answering in the chat is taking part
        # The PM was told the card, its options and the user's words verbatim.
        wake = self.wakes[0]
        self.assertIn("answered your last card", wake)
        self.assertIn("期刊列表按什么排序？", wake)
        self.assertIn("b) 按发表时间", wake)
        self.assertIn("“期刊那个就按发表时间排吧，影响因子不用管”", wake)
        self.assertIn("→ 按发表时间", wake)
        self.assertEqual(self.state(), "closed")
        conclusion = json.loads(self.db.execute("select body from cards where kind = 'closed'").fetchone()[0])["conclusion"]
        self.assertIn("按发表时间", conclusion)
        self.assertIn("按发表时间", next(daemon.MEMORY_DIR.glob("*.md")).read_text(encoding="utf-8"))
        self.assertEqual(self.events("stale:answered")[0]["card"], card)
        self.assertEqual(self.events("stale:settled")[0]["action"], "close")
        # What the judge saw: the card and only the messages after it.
        state, questions = self.judge.asked[0]
        self.assertEqual(state["card"]["decisions"][0]["options"][1], {"id": "b", "label": "按发表时间"})
        self.assertEqual([m["id"] for m in state["messages_after_card"]], [f"m{seq}"])
        self.assertEqual(set(questions), {"status", "message", "pick_d1"})

    def test_answered_in_chat_relays_when_opencode_must_go_on(self):
        card = self.card(1)
        seq = self.say("按影响因子，顺便把测试补上")
        self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq}", {"d1": "a"}
        self.replies.append({"action": "relay", "message": "按影响因子降序排列期刊列表，并补上排序的单元测试。", "basis": "用户说「按影响因子」"})
        self.docket.sweep_stale()
        self.assertEqual(self.sent, ["按影响因子降序排列期刊列表，并补上排序的单元测试。"])
        self.assertEqual(self.state(), "running")
        self.assertEqual(json.loads(self.row(card)["resolution"])["settled"], "relay")
        self.assertEqual(self.events("relayed")[0]["via"], "chat")
        self.assertEqual(self.docket.relayed_count("ses_s"), 1)  # paid for by the round the chat answer spent

    def test_a_card_the_chat_made_moot_is_closed_as_void(self):
        card = self.card(2)
        self.say("今天天气不错", ago_minutes=30)
        seq = self.say("排序那件不做了，期刊列表整个换成 OpenAlex 的接口")
        self.judge.status, self.judge.message = "superseded", f"m{seq}"
        self.replies.append({"action": "close", "conclusion": "排序问题作废：你改成整体换 OpenAlex 接口。", "basis": "用户说「排序那件不做了」"})
        self.docket.sweep_stale()
        resolution = json.loads(self.row(card)["resolution"])
        self.assertEqual((resolution["action"], resolution["by"], resolution["seq"]), ("superseded", "chat-match", seq))
        self.assertEqual(self.docket.delegation("ses_s")["rounds"], 0)  # the user did not answer the card
        self.assertIn("no longer needs an answer", self.wakes[0])
        self.assertIn("do not relay", self.wakes[0])
        self.assertEqual(self.state(), "closed")
        self.assertEqual(self.events("stale:superseded")[0]["card"], card)

    def test_a_moot_card_may_not_be_relayed(self):
        validate = pm.validate_settle("superseded")
        self.assertTrue(validate({"action": "relay", "message": "go", "basis": "x"}))
        self.assertTrue(validate({"action": "close", "conclusion": "void"}))  # the basis is required
        self.assertEqual(validate({"action": "close", "conclusion": "void", "basis": "x"}), [])
        self.assertTrue(pm.validate_settle("quiet")({"action": "relay", "message": "go"}))
        self.assertEqual(pm.validate_settle("answered")({"action": "relay", "message": "go", "basis": "x"}), [])

    def test_an_open_verdict_leaves_the_card_and_is_not_asked_twice(self):
        card = self.card(1)
        self.say("那个排序的事我再想想")
        self.docket.sweep_stale()
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(len(self.judge.asked), 1)  # nothing new in the chat, no second call
        self.say("还是没想好")
        self.docket.sweep_stale()
        self.assertEqual(len(self.judge.asked), 2)
        self.assertEqual([m["text"] for m in self.judge.asked[1][0]["messages_after_card"]], ["那个排序的事我再想想", "还是没想好"])
        self.assertEqual(self.wakes, [])

    def test_messages_before_the_card_are_not_read(self):
        self.say("按影响因子", ago_minutes=120)
        self.card(1)
        self.docket.sweep_stale()
        self.assertEqual(self.judge.asked, [])

    # ---------- nobody answers ----------

    def test_three_quiet_hours_wake_the_pm_once(self):
        card = self.card(3.1)
        self.replies.append({"action": "card", "card": dict(QUESTION, progress="排序还差你一句话：按影响因子还是按时间？")})
        self.docket.sweep_stale()
        self.assertEqual(len(self.wakes), 1)
        self.assertIn("Nobody has answered your last card on Docket for 3.1 hours", self.wakes[0])
        self.assertIn("Silence is not an answer", self.wakes[0])
        self.assertEqual(json.loads(self.row(card)["resolution"]), {"action": "superseded", "by": "quiet-wake"})
        new = ledger.open_card(self.db, "ses_s")
        body = json.loads(new["body"])
        self.assertTrue(body["quiet_woken"])
        self.assertEqual(body["quiet_since"], self.row(card)["shown_at"])  # the first card's clock
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(self.docket.delegation("ses_s")["rounds"], 0)
        # The re-asked card is not woken for again; it expires on the first one's clock.
        self.age(new["id"], 4)
        self.docket.sweep_stale()
        self.assertEqual(len(self.wakes), 1)
        self.db.execute("update cards set body = ? where id = ?", (json.dumps({**body, "quiet_since": ledger.now() - 25 * HOUR}), new["id"]))
        self.docket.sweep_stale()
        self.assertEqual(self.state(), "closed")
        self.assertEqual(len(self.wakes), 1)

    def test_a_quiet_wake_can_close_as_expired(self):
        card = self.card(4)
        self.replies.append({"action": "close", "conclusion": "排序没等到回复，已收掉。", "remaining": ["期刊排序方式待定"]})
        self.docket.sweep_stale()
        self.assertEqual(json.loads(self.row(card)["resolution"]), {"action": "expired", "by": "quiet-wake"})
        self.assertEqual(self.state(), "closed")
        self.assertIn("期刊排序方式待定", json.loads(self.db.execute("select body from cards where kind = 'closed'").fetchone()[0])["conclusion"])

    def test_a_quiet_wake_yields_to_an_answer_given_meanwhile(self):
        card = self.card(4)

        def answer_while_thinking(key, message, validate, record=None):
            ledger.spend_round(self.db, "ses_s", card, {"choices": {"d1": "a"}, "reply": ""})
            return {"action": "close", "conclusion": "expired"}

        with mock.patch.object(pm, "ask", answer_while_thinking):
            self.docket.sweep_stale()
        self.assertEqual(json.loads(self.row(card)["resolution"]), {"choices": {"d1": "a"}, "reply": ""})
        self.assertNotEqual(self.state(), "closed")
        self.assertEqual(self.events("stale:dropped")[0]["card"], card)

    def test_a_day_unanswered_is_closed_by_the_docket_itself(self):
        card = self.card(25)
        self.docket.sweep_stale()
        self.assertEqual(self.wakes, [])  # past the cap the PM is not asked
        resolution = json.loads(self.row(card)["resolution"])
        self.assertEqual((resolution["action"], resolution["by"], resolution["reason"]), ("auto-expired", "quiet-expiry", "no reply"))
        self.assertEqual(self.state(), "closed")
        closed = json.loads(self.db.execute("select body from cards where kind = 'closed'").fetchone()[0])
        self.assertIn("无人回答，已过期", closed["conclusion"])
        self.assertEqual(closed["auto_expired"]["by"], "quiet-expiry")
        self.assertIn("无人回答，已过期", next(daemon.MEMORY_DIR.glob("*.md")).read_text(encoding="utf-8"))
        self.assertEqual(self.events("stale:quiet-expired")[0]["card"], card)
        self.assertEqual(self.docket.delegation("ses_s")["rounds"], 0)

    def test_a_relay_confirmation_or_a_docket_failure_only_expires(self):
        card = self.card(5, dict(QUESTION, pending_relay="do it", round_spent=True))
        self.docket.sweep_stale()
        self.assertEqual(self.wakes, [])
        self.assertIsNone(self.row(card)["resolved_at"])
        self.age(card, 25)
        self.docket.sweep_stale()
        self.assertEqual(self.state(), "closed")

    def test_clocks_start_no_earlier_than_this_docket(self):
        card = self.card(30)  # a card from before the upgrade
        self.docket.stale_since = ledger.now() - HOUR
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.wakes, [])

    # ---------- when nothing may move ----------

    def test_a_busy_session_is_left_alone(self):
        self.service.busy = True
        card = self.card(30)
        seq = self.say("按发表时间")
        self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq}", {"d1": "b"}
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual((self.wakes, self.judge.asked, self.state()), ([], [], "awaiting"))
        # A CLI run still going is busy too.
        self.service.busy = False
        self.db.execute("update delegations set engine = 'claude' where id = 'ses_s'")
        self.db.execute("insert into runs (delegation_id, pid, out, started_at) values ('ses_s', 1, '/dev/null', 0)")
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        # So is a PM job in flight.
        self.db.execute("update runs set finished_at = 1")
        self.docket.jobs["ses_s"] = 1
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])

    def test_with_the_judge_off_nothing_happens_and_nothing_fails(self):
        card = self.card(1)
        self.say("按发表时间")
        with mock.patch.object(backend, "available", lambda: False):
            self.docket.sweep_stale()
        self.assertEqual(self.judge.asked, [])
        # A judge that fails is the same: logged, skipped, tried again on the next new message.
        with mock.patch.object(backend, "ask", mock.Mock(side_effect=backend.JudgeError("HTTP 500"))), self.assertLogs("docket", "WARNING"):
            self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual((self.wakes, self.state()), ([], "awaiting"))
        self.assertIsNone(self.db.execute("select 1 from kv where key like 'stale_seq:%'").fetchone())
        # An unreadable transcript too.
        with mock.patch.object(daemon, "openclaw_connect", mock.Mock(side_effect=transcript.Unavailable(self.chat, "test", OSError("gone")))):
            self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])

    def test_switched_off_it_does_nothing(self):
        card = self.card(30)
        self.docket.settings["stale_enabled"] = False
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.state(), "awaiting")

    def test_a_permission_request_is_never_swept(self):
        card = self.card(30, dict(QUESTION, pending_permission="per_1"))
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])

    # ---------- what the review found ----------

    def test_a_queued_card_waits_from_when_it_went_up(self):
        first = self.card(23)
        queued = self.docket.show_card("ses_s", "report", {"kind": "report", "progress": "报告", "decisions": [], "decided_for_you": []})
        self.age(queued, 23)
        self.db.execute("update cards set shown_at = null where id = ?", (queued,))  # written long ago, never on show
        self.replies.append({"action": "card", "card": {"kind": "report", "progress": "ok", "decisions": [], "decided_for_you": []}})
        ledger.spend_round(self.db, "ses_s", first, {"choices": {"d1": "a"}, "reply": ""})
        self.docket.advance("ses_s")
        self.assertEqual(ledger.open_card(self.db, "ses_s")["id"], queued)
        self.docket.sweep_stale()
        self.assertIsNone(self.row(queued)["resolved_at"])  # neither woken for nor expired
        self.assertEqual((self.wakes, self.state()), ([], "awaiting"))

    def test_chat_before_a_queued_card_went_up_is_not_read_against_it(self):
        self.card(1)
        queued = self.docket.show_card("ses_s", "report", {"kind": "report", "progress": "报告", "decisions": [], "decided_for_you": []})
        self.say("按发表时间", ago_minutes=10)
        ledger.resolve_card(self.db, ledger.open_card(self.db, "ses_s")["id"], {"reply": "x"})
        self.docket.advance("ses_s")
        self.assertEqual(ledger.open_card(self.db, "ses_s")["id"], queued)
        self.docket.sweep_stale()
        self.assertEqual(self.judge.asked, [])

    def test_one_chat_message_is_never_charged_twice(self):
        card = self.card(1)
        seq = self.say("按发表时间")
        self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq}", {"d1": "b"}
        self.replies.append({"action": "close", "conclusion": "定了", "basis": "用户说按发表时间"})
        self.docket.sweep_stale()
        self.assertEqual(self.docket.delegation("ses_s")["rounds"], 1)
        # The mention poll reaches the same message afterwards: it is already taken.
        ledger.add_delegation(self.db, id="ses_t", title="t", directory=None, source_key=MAIN, dispatch_seq=1, dispatched_at=0, brief="", context=[])
        message = mock.Mock(seq=seq, text="按发表时间", time=ledger.now(), role="user")
        answer = {"choice": "ses_t", "belongs": 0.95, "new_work": 0.1}
        with mock.patch.object(relevance, "attribute", lambda *a: (answer, None)), mock.patch.object(relevance, "should_move", lambda *a: True):
            self.docket.attribute(message, [], [self.docket.delegation("ses_t")], CHAT)
        self.assertEqual(self.docket.delegation("ses_t")["rounds"], 0)
        self.assertEqual(self.events("mention:claimed")[0]["seq"], seq)
        # And the other way round: a message the mention took is not used to settle a card.
        card2 = self.docket.show_card("ses_t", "question", dict(QUESTION))
        seq2 = self.say("按影响因子", ago_minutes=-1)
        ledger.claim(self.db, f"chat_claim:{CHAT}:{seq2}")
        self.judge.message, self.judge.picks = f"m{seq2}", {"d1": "a"}
        self.db.execute("update delegations set state = 'awaiting' where id = 'ses_t'")
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card2)["resolved_at"])
        self.assertEqual(self.events("stale:claimed")[0]["card"], card2)
        self.assertEqual(self.docket.delegation("ses_t")["rounds"], 0)
        self.assertIsNotNone(self.row(card)["resolved_at"])

    def test_a_failed_quiet_wake_leaves_the_card_and_no_failure_card(self):
        card = self.card(4)
        with mock.patch.object(pm, "ask", mock.Mock(side_effect=pm.PMFailed([{"attempt": 1, "model": None, "raw": "", "errors": ["x"]}]))):
            self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(ledger.open_card(self.db, "ses_s")["id"], card)
        self.assertEqual(self.db.execute("select count(*) from cards where kind = 'failure'").fetchone()[0], 0)
        self.assertEqual(self.state(), "awaiting")
        self.assertEqual(self.events("stale:pm-failed")[0]["card"], card)
        self.assertNotIn("quiet_wake", daemon.Docket.RETRYABLE)

    def test_still_weighing_it_moves_the_clock(self):
        card = self.card(23.9)
        seq = self.say("排序那件我再想想，明天定")
        self.judge.status, self.judge.message, self.judge.p = "discussing", f"m{seq}", 0.7
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.events("stale:discussing")[0]["seq"], seq)
        self.age(card, 25)  # a day after the card, but only an hour after the user last weighed it
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.wakes, [])

    def test_a_user_line_is_judged_with_what_it_replies_to(self):
        self.card(1)
        self.say("要不要把日报也改成周报？", role="assistant")
        self.say("好")
        self.docket.sweep_stale()
        rows = self.judge.asked[0][0]["messages_after_card"]
        self.assertEqual([(r["text"], r.get("replying_to")) for r in rows], [("好", "要不要把日报也改成周报？")])

    def test_a_quiet_wake_yields_to_a_chat_answer_given_meanwhile(self):
        card = self.card(4)
        seq = []

        def answer_in_chat_while_thinking(key, message, validate, record=None):
            seq.append(self.say("按发表时间", ago_minutes=0))
            self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq[0]}", {"d1": "b"}
            self.wakes.append(message)
            if len(self.wakes) == 1:
                return {"action": "close", "conclusion": "没人回，收掉"}
            return {"action": "close", "conclusion": "你在主会话里定了按发表时间", "basis": "用户说按发表时间"}

        with mock.patch.object(pm, "ask", answer_in_chat_while_thinking):
            self.docket.sweep_stale()
        resolution = json.loads(self.row(card)["resolution"])
        self.assertEqual((resolution["action"], resolution["choices"]), ("answered-in-chat", {"d1": "b"}))
        self.assertEqual(self.events("stale:dropped")[0]["chat"], "answered")
        self.assertIn("你在主会话里定了", json.loads(self.db.execute("select body from cards where kind = 'closed'").fetchone()[0])["conclusion"])

    def test_nonsense_minutes_fall_back_to_the_defaults(self):
        card = self.card(1)
        self.docket.settings.update(quiet_expire_minutes=0, quiet_wake_minutes=-5)
        self.docket.sweep_stale()
        self.assertIsNone(self.row(card)["resolved_at"])
        self.assertEqual(self.docket.minutes_ms("quiet_expire_minutes"), 1440 * 60_000)

    def test_the_grace_starts_when_the_feature_first_runs(self):
        card = self.card(30)
        self.docket.stale_since = None
        self.docket.settings["stale_enabled"] = False
        self.docket.sweep_stale()
        self.assertIsNone(self.db.execute("select 1 from kv where key = 'stale_since'").fetchone())
        self.docket.settings["stale_enabled"] = True
        self.docket.sweep_stale()  # switched on now: the 30-hour card's clock starts now
        self.assertIsNotNone(self.docket.stale_since)
        self.assertIsNone(self.row(card)["resolved_at"])

    def test_a_session_that_is_gone_counts_as_idle(self):
        card = self.card(25)

        def gone(service, sid):
            raise RuntimeError("404 NotFound")

        with mock.patch.object(self.service, "session", gone):
            self.docket.sweep_stale()
        self.assertEqual(self.state(), "closed")
        self.assertEqual(json.loads(self.row(card)["resolution"])["by"], "quiet-expiry")

    def test_at_the_limit_a_chat_answer_is_blocked(self):
        card = self.card(1)
        self.db.execute("update delegations set rounds = 3 where id = 'ses_s'")
        seq = self.say("按发表时间")
        self.judge.status, self.judge.message, self.judge.picks = "answered", f"m{seq}", {"d1": "b"}
        self.docket.sweep_stale()
        self.assertEqual(self.events("stale:blocked")[0]["card"], card)
        self.assertEqual(self.state(), "limit")
        self.assertEqual((self.docket.delegation("ses_s")["rounds"], self.wakes), (3, []))

    def test_a_docket_failure_card_only_expires(self):
        card = self.card(5, {"kind": "failure", "progress": "案头出错", "decisions": [], "decided_for_you": [], "docket_fault": True})
        self.say("按发表时间")
        self.docket.sweep_stale()
        self.assertEqual((self.wakes, self.judge.asked), ([], []))
        self.assertIsNone(self.row(card)["resolved_at"])
        self.age(card, 25)
        self.docket.sweep_stale()
        self.assertEqual(self.state(), "closed")

    def test_a_held_lock_is_not_waited_on(self):
        card = self.card(25)
        lock = self.docket.lock("ses_s")
        lock.acquire()
        try:
            self.docket.sweep_stale()  # returns at once instead of blocking the sweep thread
        finally:
            lock.release()
        self.assertIsNone(self.row(card)["resolved_at"])

    def test_a_new_chat_session_is_read_afresh(self):
        card = self.card(1)
        self.docket.mark_judged(card, "old-session:99")
        self.say("按发表时间")  # seq 1 in the current session, below the old session's 99
        self.docket.sweep_stale()
        self.assertEqual(len(self.judge.asked), 1)

    def test_a_closed_handoff_cannot_be_retried(self):
        self.docket.settings["fallback_model"] = "p/m"
        ledger.log(self.db, "ses_s", "relayed", {"message": "go"})
        self.docket.finish("ses_s", "done")
        with self.assertRaises(ValueError):
            self.docket.retry("ses_s")

    def test_reconcile_prunes_notes_of_resolved_cards(self):
        card = self.card(1)
        self.docket.mark_judged(card, f"{CHAT}:1")
        self.db.execute("insert into kv values ('chat_claim:x:1', ?)", (str(ledger.now() - 8 * 86400_000),))
        self.docket.finish("ses_s", "done")

        class Service:
            def recent_sessions(self, limit=40):
                return []

        with mock.patch.object(daemon, "Service", Service):
            self.docket.reconcile()
        self.assertIsNone(self.db.execute("select 1 from kv where key like 'stale_seq:%' or key like 'chat_claim:%'").fetchone())

    # ---------- the ledger's leftovers ----------

    def test_reconcile_resolves_closed_cards_an_older_build_left_open(self):
        for i in range(3):
            sid = f"ses_old{i}"
            ledger.add_delegation(self.db, id=sid, title=sid, directory=None, source_key=None, dispatch_seq=None, dispatched_at=0, brief="", context=[])
            ledger.add_card(self.db, sid, "closed", {"progress": "done", "conclusion": "done", "decisions": [], "decided_for_you": []})
            ledger.set_state(self.db, sid, "closed")

        class Service:
            def recent_sessions(self, limit=40):
                return []

        with mock.patch.object(daemon, "Service", Service):
            self.docket.reconcile()
        left = self.db.execute("select count(*) from cards where resolved_at is null and kind = 'closed'").fetchone()[0]
        self.assertEqual(left, 0)
        self.assertEqual(self.events("reconcile:conclusions"), [{"cards": 3}])
        shown = next(d for d in ledger.snapshot(self.db) if d["id"] == "ses_old0")
        self.assertEqual((shown["card"]["kind"], shown["card"]["conclusion"]), ("closed", "done"))
        self.assertNotIn("choices", shown["card"])  # the panel shows it as before
        # Closing from now on leaves nothing unresolved.
        self.docket.finish("ses_s", "done")
        self.assertEqual(self.db.execute("select count(*) from cards where resolved_at is null").fetchone()[0], 0)


class CardStatus(unittest.TestCase):
    """relevance.card_status: only a sure, complete reading counts."""

    def setUp(self):
        self.judge = Judge()
        patcher = mock.patch.object(backend, "ask", self.judge)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = {"id": "d", "title": "t", "instruction": "", "brief": "", "rounds": []}
        self.messages = [{"id": "m4", "role": "user", "time": "11-25 10:00", "text": "按时间"}]

    def status(self, card=QUESTION):
        return relevance.card_status(self.task, card, self.messages, threshold=0.9)[0]

    def test_a_sure_complete_answer(self):
        self.judge.status, self.judge.message, self.judge.picks = "answered", "m4", {"d1": "b"}
        answer = self.status()
        self.assertEqual((answer["status"], answer["message"], answer["choices"]), ("answered", "m4", {"d1": "b"}))

    def test_short_of_sure_is_open(self):
        self.judge.status, self.judge.message, self.judge.picks, self.judge.p = "answered", "m4", {"d1": "b"}, 0.8
        self.assertEqual(self.status()["status"], "open")

    def test_a_decision_left_unread_is_open(self):
        self.judge.status, self.judge.message = "answered", "m4"  # pick_d1 -> none
        self.assertEqual(self.status()["status"], "open")

    def test_no_message_named_is_open(self):
        self.judge.status = "superseded"
        self.assertEqual(self.status()["status"], "open")

    def test_an_answer_in_other_words_is_kept_as_other(self):
        self.judge.status, self.judge.message, self.judge.picks = "answered", "m4", {"d1": "other"}
        self.assertEqual(self.status()["choices"], {"d1": "other"})

    def test_no_user_message_asks_nothing(self):
        self.messages = [{"id": "m4", "role": "assistant", "time": "", "text": "x"}]
        self.assertEqual(self.status()["status"], "open")
        self.assertEqual(self.judge.asked, [])

    def test_a_report_card_has_no_option_questions(self):
        self.judge.status, self.judge.message = "answered", "m4"
        answer = self.status({"kind": "report", "progress": "做完了", "decisions": []})
        self.assertEqual((answer["status"], answer["choices"]), ("answered", {}))
        self.assertEqual(set(self.judge.asked[0][1]), {"status", "message"})


if __name__ == "__main__":
    unittest.main()
