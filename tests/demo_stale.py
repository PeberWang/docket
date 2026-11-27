# -*- coding: utf-8 -*-
"""Demo: a card the user answered in the main chat is found, settled by its PM and closed.

    /usr/bin/python3 -I tests/demo_stale.py               # scripted judge and PM, fully offline
    /usr/bin/python3 -I tests/demo_stale.py --live-judge  # the configured judge reads the chat (one request)

A fixture handoff and a question card in a temporary ledger, one user message in a synthetic
transcript, one sweep. Prints the card's resolve record, its resolution JSON and the wake text
the PM received. Nothing outside a temporary directory is read or written, except the judge's
key and endpoint with --live-judge.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="docket-demo-"))
os.environ["DOCKET_HOME"] = str(SANDBOX / "home")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import config, daemon, i18n, ledger, pm  # noqa: E402
from judge import backend, transcript  # noqa: E402

LIVE = "--live-judge" in sys.argv
MAIN, CHAT = "agent:main:main", "demo-chat"
CARD = {
    "kind": "question",
    "progress": "两种排序都能做，等你定。",
    "decisions": [{"id": "d1", "question": "期刊列表按什么排序？", "options": [
        {"id": "a", "label": "按影响因子", "recommended": True},
        {"id": "b", "label": "按发表时间", "recommended": False},
    ]}],
    "decided_for_you": ["沿用现有分页"],
}
SAID = "刚想了下，期刊列表就按发表时间排吧，影响因子先不管"


class Service:
    def session(self, sid):
        return {"outcome": None, "time": {"busy": 1, "idle": 2}}  # idle, still waiting on the user


def scripted_judge(state, questions):
    said = state["messages_after_card"][-1]["id"]
    picks = {"status": "answered", "message": said, "pick_d1": "b"}
    answers = {}
    for qid, question in questions.items():
        options = list(question["criteria"])
        p = {o: (0.96 if o == picks[qid] else 0.04 / (len(options) - 1)) for o in options}
        answers[qid] = {"choice": picks[qid], "probabilities": p, "confidence": 0.96}
    return {"answers": answers, "usage": {"input_tokens": 0}}


wakes = []


def scripted_pm(key, message, validate, record=None):
    wakes.append(message)
    reply = {"action": "close", "conclusion": "你在主会话里定了按发表时间排序，这件结案。", "basis": "用户在主会话里说「就按发表时间排吧」"}
    assert validate(reply) == [], validate(reply)
    return reply


def main():
    chat = SANDBOX / "agent.sqlite"
    conn = sqlite3.connect(chat)
    conn.executescript("create table transcript_events (session_id text, seq integer, created_at integer, event_json text);"
                       "create table session_windows (session_key text, session_id text);")
    conn.execute("insert into session_windows values (?, ?)", (MAIN, CHAT))
    conn.commit()

    daemon.Service = Service
    daemon.MEMORY_DIR = SANDBOX / "memory"
    daemon.MEMORY_DIR.mkdir()
    daemon.openclaw_connect = lambda: transcript.connect(chat)
    pm.ask = scripted_pm
    if not LIVE:
        backend.available = lambda: True
        backend.ask = scripted_judge
    docket = daemon.Docket()
    docket.settings = dict(config.DEFAULTS)
    docket.locale, docket.t = "zh", i18n.Strings("zh")
    pm.configure("zh")
    docket.stale_since = 0
    docket.submit = lambda did, job, *args: job(*args)
    db = docket.db()

    ledger.add_delegation(db, id="ses_demo", title="期刊排序", directory=None, source_key=MAIN, dispatch_seq=1,
                          dispatched_at=ledger.now() - 7200_000, brief="把期刊列表的排序改掉，排序方式留给你定", context=[])
    card_id = docket.show_card("ses_demo", "question", CARD)
    db.execute("update cards set created_at = ?, shown_at = ? where id = ?", (ledger.now() - 3600_000, ledger.now() - 3600_000, card_id))
    event = {"message": {"role": "user", "content": [{"type": "text", "text": SAID}]}}
    conn.execute("insert into transcript_events values (?, 1, ?, ?)", (CHAT, ledger.now() - 60_000, json.dumps(event, ensure_ascii=False)))
    conn.commit()
    conn.close()

    print(f"judge: {'live ' + backend.current.settings['backend'] if LIVE else 'scripted'}; sandbox {SANDBOX}")
    if LIVE and not backend.available():
        print(f"the judge is off ({backend.current.missing()}): the sweep skips the chat and leaves the card")
    print(f"before: card {card_id} open, handoff {docket.delegation('ses_demo')['state']}")
    docket.sweep_stale()

    card = db.execute("select * from cards where id = ?", (card_id,)).fetchone()
    print(f"\n== card {card_id} resolve record ==")
    print(f"resolved_at = {card['resolved_at']}  handoff state = {docket.delegation('ses_demo')['state']}  rounds = {docket.delegation('ses_demo')['rounds']}")
    print("\n== resolution JSON ==")
    print(json.dumps(json.loads(card["resolution"]) if card["resolution"] else None, ensure_ascii=False, indent=2))
    print("\n== events ==")
    for kind, detail in db.execute("select kind, detail from events where delegation_id = 'ses_demo' and kind like 'stale:%' order by id"):
        print(f"{kind} {detail}")
    closed = db.execute("select body from cards where delegation_id = 'ses_demo' and kind = 'closed'").fetchone()
    if closed:
        print("\n== closed card conclusion ==")
        print(json.loads(closed[0])["conclusion"])
    print("\n== wake text the PM received ==")
    print(wakes[0] if wakes else "(the PM was not woken)")


if __name__ == "__main__":
    main()
