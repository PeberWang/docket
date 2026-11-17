"""The ledger: the desk's single source of truth for delegations and cards.

One SQLite file, independent of both OpenClaw's and OpenCode's databases. The daemon
writes it; the native app reads it through the daemon's local HTTP API.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

HOME = Path(os.environ.get("DESK_HOME", Path.home() / "Library/Application Support/openclaw-desk"))
DB = HOME / "desk.sqlite"
ROUND_LIMIT = 3

# A delegation moves through these states:
#   running       OpenCode is working; nothing for the user to do
#   pending       the PM is writing a card (OpenCode just finished, failed or asked)
#   awaiting      a card is waiting for the user's decision
#   taken_over    the user took it into OpenCode; the desk only shows a link
#   closed        done; the conclusion went back to OpenClaw's memory
STATES = ("running", "pending", "awaiting", "taken_over", "closed")

SCHEMA = """
create table if not exists delegations (
  id text primary key,                 -- the OpenCode session id
  title text not null,
  directory text,
  source_key text,                     -- OpenClaw session the dispatch came from
  dispatch_seq integer,                -- transcript seq of the dispatch call
  dispatched_at integer not null,      -- epoch ms
  state text not null default 'running',
  rounds integer not null default 0,   -- user decisions relayed to OpenCode
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
  body text not null,                  -- JSON, see desk/pm.py CARD_SCHEMA
  resolved_at integer,
  resolution text                      -- JSON: {"choice": ..., "reply": ..., "action": ...}
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
    for name, kind in (("engine", "text not null default 'opencode'"), ("engine_session", "text"), ("model", "text")):
        if name not in columns:  # ledgers from before the engine layer
            conn.execute(f"alter table delegations add column {name} {kind}")
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
    return f"agent:main:desk-{delegation_id.removeprefix('ses_').replace('_', '-')}"


def set_state(conn, delegation_id: str, state: str, **fields) -> None:
    assert state in STATES, state
    columns = ["state = ?", "updated_at = ?"] + [f"{name} = ?" for name in fields]
    conn.execute(f"update delegations set {', '.join(columns)} where id = ?", (state, now(), *fields.values(), delegation_id))
    log(conn, delegation_id, f"state:{state}", fields or None)


def add_card(conn, delegation_id: str, kind: str, body: dict) -> int:
    cursor = conn.execute(
        "insert into cards (delegation_id, created_at, kind, body) values (?, ?, ?, ?)",
        (delegation_id, now(), kind, json.dumps(body, ensure_ascii=False)),
    )
    return cursor.lastrowid


def open_card(conn, delegation_id: str):
    return conn.execute(
        "select * from cards where delegation_id = ? and resolved_at is null order by id desc limit 1", (delegation_id,)
    ).fetchone()


def resolve_card(conn, card_id: int, resolution: dict) -> None:
    conn.execute(
        "update cards set resolved_at = ?, resolution = ? where id = ?",
        (now(), json.dumps(resolution, ensure_ascii=False), card_id),
    )


def active(conn) -> list:
    return conn.execute("select * from delegations where state not in ('closed') order by dispatched_at").fetchall()


def snapshot(conn, closed_hours: int = 24) -> list[dict]:
    """Everything the panel shows: open delegations plus ones closed in the last day."""
    since = now() - closed_hours * 3600_000
    rows = conn.execute(
        "select * from delegations where state != 'closed' or updated_at >= ? order by "
        "case state when 'awaiting' then 0 when 'pending' then 1 when 'running' then 2 when 'taken_over' then 3 else 4 end, updated_at desc",
        (since,),
    ).fetchall()
    out = []
    for row in rows:
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
                "round_limit": ROUND_LIMIT,
                "dispatched_at": row["dispatched_at"],
                "updated_at": row["updated_at"],
                "card": {
                    "id": last["id"],
                    "kind": last["kind"],
                    "open": last["resolved_at"] is None,
                    **{k: v for k, v in json.loads(last["body"]).items() if k not in ("pending_relay", "pending_permission")},
                    **({"choices": answer.get("choices") or {}, "reply": answer.get("reply") or ""} if answer else {}),
                }
                if last
                else None,
            }
        )
    return out
