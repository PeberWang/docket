"""The ledger: the docket's single source of truth for delegations and cards.

One SQLite file, independent of both OpenClaw's and OpenCode's databases. The daemon
writes it; the native app reads it through the daemon's local HTTP API.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

HOME = Path(os.environ.get("DOCKET_HOME", Path.home() / "Library/Application Support/docket"))
DB = HOME / "docket.sqlite"
ROUND_LIMIT = 3

# A delegation moves through these states:
#   running       OpenCode is working; nothing for the user to do
#   pending       the PM is writing a card (OpenCode just finished, failed or asked)
#   awaiting      a card is waiting for the user's decision
#   limit         the user has spent all ROUND_LIMIT rounds; no more cards, only close or take over
#   taken_over    the user took it into OpenCode; the docket asks nothing until it is handed back
#   closed        done; the conclusion went back to OpenClaw's memory
# taken_over is not an ending: hand_back puts it on the docket again (awaiting or pending).
STATES = ("running", "pending", "awaiting", "limit", "taken_over", "closed")
# Once at the limit a handoff stays there: only an ending moves it on.
LIMIT_EXITS = ("taken_over", "closed")

SCHEMA = """
create table if not exists delegations (
  id text primary key,                 -- the OpenCode session id
  title text not null,
  directory text,
  source_key text,                     -- OpenClaw session the dispatch came from
  dispatch_seq integer,                -- transcript seq of the dispatch call
  dispatched_at integer not null,      -- epoch ms
  state text not null default 'running',
  rounds integer not null default 0,   -- times a person took part (answered a card, wrote in chat); see spend_round
  pm_key text not null,                -- OpenClaw session key of this delegation's PM
  brief text,
  context text,                        -- JSON: [{id, role, time, text, score}] chosen by the judge
  updated_at integer not null
);
create table if not exists cards (
  id integer primary key autoincrement,
  delegation_id text not null references delegations(id) on delete cascade,
  created_at integer not null,
  kind text not null,                  -- report | question | failure | closed | note
  body text not null,                  -- JSON, see docket/pm.py CARD_SCHEMA
  resolved_at integer,
  resolution text,                     -- JSON: {"choice": ..., "reply": ..., "action": ...}
  queued integer not null default 0    -- 1: written while another card was open; shown when that one is answered
);
create table if not exists events (
  id integer primary key autoincrement,
  delegation_id text,
  at integer not null,
  kind text not null,
  detail text
);
create table if not exists kv (key text primary key, value text);
create table if not exists runs (                -- CLI engine processes (claude, codex)
  id integer primary key autoincrement,
  delegation_id text not null references delegations(id) on delete cascade,
  pid integer not null,
  out text not null,                   -- output file
  started_at integer not null,
  finished_at integer
);
create index if not exists cards_open on cards(delegation_id, resolved_at);
"""


def _clean(value):
    """Card text is plain: strip the emphasis markers a model may have written."""
    if isinstance(value, str):
        return value.replace("**", "").replace("`", "")
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def now() -> int:
    return int(time.time() * 1000)


def connect(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("pragma table_info(delegations)")}
    for name, kind in (("engine", "text not null default 'opencode'"), ("engine_session", "text"), ("model", "text"), ("handbacks", "integer not null default 0")):
        if name not in columns:  # ledgers from before the engine layer (handbacks: before hand_back)
            conn.execute(f"alter table delegations add column {name} {kind}")
    card_columns = {row[1] for row in conn.execute("pragma table_info(cards)")}
    if "queued" not in card_columns:
        conn.execute("alter table cards add column queued integer not null default 0")
    return conn


def log(conn, delegation_id, kind: str, detail=None) -> None:
    conn.execute(
        "insert into events (delegation_id, at, kind, detail) values (?, ?, ?, ?)",
        (delegation_id, now(), kind, json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def get(conn, delegation_id: str):
    return conn.execute("select * from delegations where id = ?", (delegation_id,)).fetchone()


def add_delegation(conn, *, id, title, directory, source_key, dispatch_seq, dispatched_at, brief, context, engine="opencode", engine_session=None, model=None) -> None:
    conn.execute(
        "insert or ignore into delegations (id, title, directory, source_key, dispatch_seq, dispatched_at, pm_key, brief, context, updated_at, engine, engine_session, model)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, title, directory, source_key, dispatch_seq, dispatched_at, pm_key(id), brief, json.dumps(context, ensure_ascii=False), now(), engine, engine_session or id, model),
    )
    log(conn, id, "registered", {"title": title, "engine": engine})


def pm_key(delegation_id: str) -> str:
    # New handoffs get agent:main:docket-<id>. Handoffs registered before the rename keep the
    # agent:main:desk-<id> stored in their row, so their PM sessions stay reachable.
    return f"agent:main:docket-{delegation_id.removeprefix('ses_').replace('_', '-')}"


def set_state(conn, delegation_id: str, state: str, **fields) -> None:
    assert state in STATES, state
    current = conn.execute("select state from delegations where id = ?", (delegation_id,)).fetchone()
    if current and current[0] == "limit" and state not in LIMIT_EXITS:
        state = "limit"
    columns = ["state = ?", "updated_at = ?"] + [f"{name} = ?" for name in fields]
    conn.execute(f"update delegations set {', '.join(columns)} where id = ?", (state, now(), *fields.values(), delegation_id))
    log(conn, delegation_id, f"state:{state}", fields or None)


def add_card(conn, delegation_id: str, kind: str, body: dict, queued: bool = False) -> int:
    cursor = conn.execute(
        "insert into cards (delegation_id, created_at, kind, body, queued) values (?, ?, ?, ?, ?)",
        (delegation_id, now(), kind, json.dumps(body, ensure_ascii=False), 1 if queued else 0),
    )
    return cursor.lastrowid


def push_card(conn, delegation_id: str, kind: str, body: dict) -> tuple[int, bool]:
    """Cards wait their turn, first in first out; none is ever overwritten.

    A permission request is one of these cards: without an answer the work cannot go on,
    so it must not be skipped either. Answered cards drop out of the list entirely.
    Returns (card id, queued).

    The one exception is a question: only the newest unanswered question stands. An older
    one (never a permission request, which OpenCode is still waiting on) is superseded, and
    if it was the card on show the new question takes its place instead of queueing.
    """
    took_place = False
    if kind == "question" and not body.get("pending_permission"):
        shown = open_card(conn, delegation_id)
        for old in conn.execute(
            "select id, body from cards where delegation_id = ? and resolved_at is null and kind = 'question'", (delegation_id,)
        ).fetchall():
            if json.loads(old["body"]).get("pending_permission"):
                continue
            resolve_card(conn, old["id"], {"action": "superseded"})
            log(conn, delegation_id, "card:superseded", {"card": old["id"], "by": "question"})
            took_place = took_place or (shown is not None and shown["id"] == old["id"])
    queued = open_card(conn, delegation_id) is not None or (not took_place and bool(
        conn.execute("select 1 from cards where delegation_id = ? and resolved_at is null and queued = 1", (delegation_id,)).fetchone()
    ))
    return add_card(conn, delegation_id, kind, body, queued=queued), queued


def open_card(conn, delegation_id: str):
    """The card the panel shows: the oldest unanswered one (queued ones wait)."""
    return conn.execute(
        "select * from cards where delegation_id = ? and resolved_at is null and queued = 0 order by id limit 1", (delegation_id,)
    ).fetchone()


def next_queued(conn, delegation_id: str):
    row = conn.execute(
        "select * from cards where delegation_id = ? and resolved_at is null and queued = 1 order by id limit 1", (delegation_id,)
    ).fetchone()
    if row:
        conn.execute("update cards set queued = 0 where id = ?", (row["id"],))
    return row


def open_permission_card(conn, delegation_id: str):
    row = open_card(conn, delegation_id)
    if row and json.loads(row["body"]).get("pending_permission"):
        return row
    return None


def resolve_card(conn, card_id: int, resolution: dict) -> None:
    conn.execute(
        "update cards set resolved_at = ?, resolution = ? where id = ?",
        (now(), json.dumps(resolution, ensure_ascii=False), card_id),
    )


def resolve_open(conn, card_id: int, resolution: dict) -> bool:
    """Resolve a card only if nobody has yet; False when it was answered in the meantime."""
    done = conn.execute(
        "update cards set resolved_at = ?, resolution = ? where id = ? and resolved_at is null",
        (now(), json.dumps(resolution, ensure_ascii=False), card_id),
    )
    return done.rowcount > 0


def amend_resolution(conn, card_id: int, extra: dict) -> None:
    """Add fields to a resolved card's resolution (the PM's basis, written after the fact)."""
    row = conn.execute("select resolution from cards where id = ?", (card_id,)).fetchone()
    current = json.loads(row[0]) if row and row[0] else {}
    conn.execute("update cards set resolution = ? where id = ?", (json.dumps({**current, **extra}, ensure_ascii=False), card_id))


def add_conclusion(conn, delegation_id: str, body: dict) -> int:
    """The closed card: a record of the ending, resolved at birth so it never counts as waiting."""
    card_id = add_card(conn, delegation_id, "closed", body)
    resolve_card(conn, card_id, {"action": "conclusion"})
    return card_id


def spend_round(conn, delegation_id: str, card_id: int | None = None, resolution: dict | None = None) -> int | None:
    """A person took part: add one round, in one transaction with resolving their card.

    Returns the new round count, or None when the card was already resolved (a double
    click, or two requests racing): that answer is not a second round.
    """
    conn.execute("begin immediate")
    try:
        if card_id is not None:
            done = conn.execute(
                "update cards set resolved_at = ?, resolution = ? where id = ? and resolved_at is null",
                (now(), json.dumps(resolution or {}, ensure_ascii=False), card_id),
            )
            if done.rowcount == 0:
                conn.execute("rollback")
                return None
        conn.execute("update delegations set rounds = rounds + 1, updated_at = ? where id = ?", (now(), delegation_id))
        rounds = conn.execute("select rounds from delegations where id = ?", (delegation_id,)).fetchone()[0]
        log(conn, delegation_id, "round", {"rounds": rounds, "card": card_id})
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise
    return rounds


def hand_back(conn, delegation_id: str) -> str | None:
    """The user hands a taken-over handoff back to the docket, in one transaction.

    Returns the new state, or None when the handoff is not taken over (a second click, or
    any other state: nothing changes). A card left waiting at the takeover goes up and the
    handoff is awaiting; otherwise it is pending, because the PM is woken next. Rounds stay
    as they were: handing back never refills the budget. `handbacks` counts how often.
    """
    conn.execute("begin immediate")
    try:
        row = conn.execute("select state, handbacks from delegations where id = ?", (delegation_id,)).fetchone()
        if not row or row[0] != "taken_over":
            conn.execute("rollback")
            return None
        if not open_card(conn, delegation_id):
            next_queued(conn, delegation_id)
        state = "awaiting" if open_card(conn, delegation_id) else "pending"
        conn.execute(
            "update delegations set state = ?, handbacks = handbacks + 1, updated_at = ? where id = ?", (state, now(), delegation_id)
        )
        log(conn, delegation_id, "handback", {"handbacks": row[1] + 1})
        log(conn, delegation_id, f"state:{state}")
        conn.execute("commit")
    except BaseException:
        conn.execute("rollback")
        raise
    return state


def active(conn) -> list:
    return conn.execute("select * from delegations where state not in ('closed') order by dispatched_at").fetchall()


def snapshot(conn, closed_hours: int = 24, taken_over_days: int = 7) -> list[dict]:
    """Everything the panel shows: open delegations plus ones closed in the last day.
    A taken-over one stays for a week so it can still be handed back; older ones would
    only crowd the panel (hand_back works on them all the same, e.g. from scripts/handback.py).
    Live ones come first, by what needs a hand; the ended ones (taken over, closed) follow as
    one group, newest first, whatever their state."""
    since = now() - closed_hours * 3600_000
    rows = conn.execute(
        "select * from delegations where state not in ('closed', 'taken_over')"
        " or (state = 'taken_over' and updated_at >= ?) or updated_at >= ? order by "
        "case state when 'awaiting' then 0 when 'limit' then 0 when 'pending' then 1 when 'running' then 2 else 3 end, updated_at desc",
        (now() - taken_over_days * 86400_000, since),
    ).fetchall()
    out = []
    for row in rows:
        if row["state"] == "closed":  # a closed handoff shows its conclusion, never an older card left behind
            card = conn.execute(
                "select * from cards where delegation_id = ? and kind = 'closed' order by id desc limit 1", (row["id"],)
            ).fetchone()
            last = card or conn.execute("select * from cards where delegation_id = ? order by id desc limit 1", (row["id"],)).fetchone()
        else:
            card = open_card(conn, row["id"])
            last = card or conn.execute("select * from cards where delegation_id = ? order by id desc limit 1", (row["id"],)).fetchone()
        answer = json.loads(last["resolution"]) if last and last["resolution"] else {}
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "engine": row["engine"],
                "state": row["state"],
                "rounds": row["rounds"],
                "handbacks": row["handbacks"],
                "queued": conn.execute(
                    "select count(*) from cards where delegation_id = ? and resolved_at is null and queued = 1", (row["id"],)
                ).fetchone()[0],
                "round_limit": ROUND_LIMIT,
                "dispatched_at": row["dispatched_at"],
                "updated_at": row["updated_at"],
                "card": {
                    "id": last["id"],
                    "kind": last["kind"],
                    "open": last["resolved_at"] is None,
                    **{k: _clean(v) for k, v in json.loads(last["body"]).items() if k not in ("pending_relay", "pending_permission", "pm_retry", "wrap_up", "round_spent")},
                    **({"choices": answer.get("choices") or {}, "reply": answer.get("reply") or ""} if answer and last["kind"] != "closed" else {}),
                }
                if last
                else None,
            }
        )
    return out
