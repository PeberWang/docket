"""Check that the docket can read OpenClaw. Run it after every OpenClaw upgrade.

    /usr/bin/python3 -I scripts/selfcheck.py                    # one summary line, then the details
    /usr/bin/python3 -I scripts/selfcheck.py --db COPY.sqlite   # another agent database (a copy, say)
    /usr/bin/python3 -I scripts/selfcheck.py --json             # the same, as JSON

Three things:
  1. which transcript database is used and where that came from (agent_databases or the fallback);
  2. whether the current main session can be read, and how many of its recent events read as messages;
  3. the latest dispatch in the transcripts: did it parse, is its session watched, did it reach the ledger.
Only reads. Exit status 1 when the database or the main session cannot be read.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config, dispatches, ledger, registry  # noqa: E402
from judge import transcript  # noqa: E402


def when(ms) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M") if ms else "-"


def short(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + "/") else text


def database(args) -> dict:
    if args.db:
        path, source = Path(args.db), "--db"
    else:
        path, source = transcript.resolve(Path(args.state) if args.state else None)
    out = {"path": str(path), "source": source, "ok": False}
    try:
        conn = transcript.connect(path)
    except transcript.Unavailable as error:
        out["error"] = str(error.error)
        return out
    out["ok"], out["conn"] = True, conn
    return out


def main_session(conn, key: str, hours: int) -> dict:
    session_id = transcript.session_id_for(conn, key)
    out = {"key": key, "session_id": session_id, "ok": bool(session_id)}
    if not session_id:
        out["keys_seen"] = sorted({k for k in transcript.session_keys(conn).values()})[:12]
        return out
    since = ledger.now() - hours * 3600_000
    last = conn.execute("select max(seq), max(created_at) from transcript_events where session_id = ?", (session_id,)).fetchone()
    raw = conn.execute("select event_json from transcript_events where session_id = ? and created_at >= ?", (session_id, since)).fetchall()
    unread = collections.Counter()
    for (event_json,) in raw:
        try:
            event = json.loads(event_json)
        except ValueError:
            unread["(not JSON)"] += 1
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        if message.get("role") == "custom" and transcript.normalize(event)[0] is None:
            unread[str(message.get("customType") or event.get("customType") or "?")] += 1
    read = transcript.messages(conn, session_id, since=since)
    out.update(
        last_seq=last[0],
        last_at=last[1],
        events=len(raw),
        messages=len(read),
        users=sum(m.role == "user" for m in read),
        tool_calls=sum(len(m.tool_calls) for m in read),
        unread_custom=dict(unread.most_common(5)),
    )
    return out


def latest_dispatch(conn, settings: dict, hours: int) -> dict:
    now = ledger.now()
    marked = conn.execute(
        "select count(*) from transcript_events where created_at >= ? and (event_json like '%desk-dispatch%' or event_json like '%opencode-dispatch%')",
        (now - hours * 3600_000,),
    ).fetchone()[0]
    unwatched = None
    for key, session_id, seq, created, command in registry.dispatch_calls(conn, now - hours * 3600_000, now):
        if not any(m in command for m in ("desk-dispatch", "opencode-dispatch", "opencode run")):
            continue
        parsed = dispatches.parse(command)
        out = {"found": True, "key": key, "session_id": session_id, "seq": seq, "at": created, "command": command[:200]}
        if parsed and not config.watched(settings, key):
            # Cron and PM sessions dispatch too; they are never the docket's, so look further back.
            unwatched = unwatched or dict(out, title=parsed["title"], result="unwatched", ok=False)
            continue
        if not parsed:
            out.update(result="unparsed", ok=False)  # a follow-up (-s/--continue) or a placeholder title parses to None too
            return out
        out.update(title=parsed["title"], result="recognised", ok=True, ledger=in_ledger(parsed["title"], created))
        return out
    if unwatched:
        return unwatched
    # No tool call came out, yet the text is there: the event shape is one normalize does not know.
    return {"found": False, "ok": marked == 0, "result": "unextracted" if marked else "none", "marked_events": marked}


def in_ledger(title: str, dispatched_at: int):
    if not ledger.DB.is_file():
        return None
    conn = sqlite3.connect(f"file:{ledger.DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "select id, state from delegations where title = ? and dispatched_at between ? and ? order by dispatched_at desc limit 1",
            (title, dispatched_at - 60_000, dispatched_at + 60_000),
        ).fetchone()
        if row:
            return {"id": row[0], "state": row[1]}
        skipped = conn.execute(
            "select detail from events where kind = 'skipped' and detail like ? order by id desc limit 1", (f"%{title}%",)
        ).fetchone()
        return {"skipped": json.loads(skipped[0])} if skipped else {}
    finally:
        conn.close()


def summary(db: dict, session: dict | None, dispatch: dict | None) -> str:
    source = f"{short(Path(db['path']))} ({db['source']})"
    if not db["ok"]:
        return f"FAIL  transcript {source} does not open: {db.get('error')}"
    if not session["ok"]:
        return f"FAIL  transcript {source}; no session for {session['key']}"
    parts = [f"transcript {source}", f"{session['key']} = {session['session_id'][:8]}… last event {when(session['last_at'])}, {session['messages']}/{session['events']} recent events read as messages"]
    result = dispatch["result"]
    if result == "none":
        parts.append("no dispatch in the window")
    elif result == "unextracted":
        parts.append(f"{dispatch['marked_events']} events mention a dispatch but no tool call could be read from them")
    elif result == "unparsed":
        parts.append(f"latest dispatch {when(dispatch['at'])} did not parse")
    elif result == "unwatched":
        parts.append(f"latest dispatch {when(dispatch['at'])} “{dispatch['title']}” is in {dispatch['key']}, not watched")
    else:
        entry = dispatch.get("ledger")
        where = f"in the ledger as {entry['id']} ({entry['state']})" if entry and "id" in entry else "skipped by the daemon" if entry else "not in the ledger"
        parts.append(f"latest dispatch {when(dispatch['at'])} “{dispatch['title']}” recognised, {where}")
    entry = dispatch.get("ledger")
    registered = result != "recognised" or entry is None or "id" in entry  # entry None: no ledger on this machine
    healthy = session["messages"] > 0 and dispatch["ok"] and registered
    return ("OK    " if healthy else "WARN  ") + "; ".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="agent database to read instead of the resolved one")
    parser.add_argument("--state", help="OpenClaw state database (default ~/.openclaw/state/openclaw.sqlite)")
    parser.add_argument("--key", default="agent:main:main")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = config.load()
    db = database(args)
    session = dispatch = None
    if db["ok"]:
        conn = db.pop("conn")
        try:
            session = main_session(conn, transcript.canonical_key(args.key), args.hours)
            dispatch = latest_dispatch(conn, settings, args.hours) if session["ok"] else None
        finally:
            conn.close()
    line = summary(db, session, dispatch)
    if args.json:
        print(json.dumps({"summary": line, "database": db, "main_session": session, "latest_dispatch": dispatch}, ensure_ascii=False, indent=2))
    else:
        print(line)
        for name, part in (("database", db), ("main session", session), ("latest dispatch", dispatch)):
            if part is not None:
                print(f"  {name}: " + json.dumps(part, ensure_ascii=False))
    sys.exit(0 if db["ok"] and session and session["ok"] else 1)


if __name__ == "__main__":
    main()
