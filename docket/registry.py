"""Turn a new dispatched OpenCode session into a docket delegation.

The session tells us the title and the time. The dispatch itself is found in the
OpenClaw transcripts (only watched sessions), and the judge picks the background.
"""

from __future__ import annotations

import datetime as dt
import json

from docket import config as docket_config
from docket import dispatches
from judge import backend, relevance
from judge.transcript import MESSAGE_FILTER, messages, normalize, session_keys
from judge.transcript import connect as openclaw_connect

WINDOW_BEFORE = 20 * 60_000  # the dispatch call precedes the session by seconds; allow slack
WINDOW_AFTER = 2 * 60_000
CONTEXT_MESSAGES = 80


def dispatch_calls(conn, since_ms: int, until_ms: int):
    """Every dispatch-looking tool call in the window, newest first: (key, session_id, seq, time, command).

    Matched on the command marker rather than the title: 2026.11 can carry the arguments as a JSON
    string inside the event, where a title with quotes or non-ASCII text is escaped."""
    keys = session_keys(conn)
    rows = conn.execute(
        "select session_id, seq, created_at, event_json from transcript_events "
        f"where created_at between ? and ? and {MESSAGE_FILTER} "
        "and (event_json like '%dispatch%' or event_json like '%opencode run%') order by created_at desc, seq desc",
        (since_ms, until_ms),
    ).fetchall()
    for session_id, seq, created, raw in rows:
        if session_id not in keys:
            continue
        try:
            role, _, calls = normalize(json.loads(raw))
        except (ValueError, AttributeError, TypeError):
            continue
        for call in calls if role == "assistant" else []:
            if call["command"]:
                yield keys[session_id], session_id, seq, created, call["command"]


def find_dispatch(title: str, created_ms: int, settings: dict):
    """Return {"title", "session_key", "session_id", "seq", "time", "directory", "directory_raw", "brief", "instruction"} or None."""
    conn = openclaw_connect()
    try:
        calls = list(dispatch_calls(conn, created_ms - WINDOW_BEFORE, created_ms + WINDOW_AFTER))
    finally:
        conn.close()
    for key, session_id, seq, created, command in calls:
        if not docket_config.watched(settings, key):
            continue
        parsed = dispatches.parse(command, settings.get("prefix", ""))
        if parsed and dispatches.same_task(title, parsed["title"], settings.get("prefix", "")):
            return {
                "title": parsed["title"],
                "session_key": key,
                "session_id": session_id,
                "seq": seq,
                "time": created,
                "directory": parsed["directory"],  # already expanded; checked when it is stored
                "directory_raw": parsed["directory_raw"],
                "brief": dispatches.read_brief(parsed["directory"], parsed["brief_path"]),
                "instruction": parsed["instruction"],
            }
    return None


def _rows(items) -> list[dict]:
    return [
        {"id": f"m{m.seq}", "role": m.role, "ts": m.time, "time": dt.datetime.fromtimestamp(m.time / 1000).strftime("%m-%d %H:%M"), "text": m.text}
        for m in items
        if m.text.strip()
    ]


def build_context(dispatch: dict, title: str, settings: dict) -> list[dict]:
    """The judge's pick of earlier messages, verbatim, in time order."""
    conn = openclaw_connect()
    try:
        history = messages(conn, dispatch["session_id"], since=dispatch["time"] - 7 * 86400_000, until=dispatch["time"])
    finally:
        conn.close()
    rows = _rows(history[-CONTEXT_MESSAGES:])
    if not rows or not backend.available():
        return []
    task = {"title": title, "instruction": dispatch["instruction"], "brief": dispatch["brief"]}
    scores, _ = relevance.context_scores(task, rows)
    picked = [dict(row, score=round(scores.get(row["id"], 0.0), 3)) for row in rows if scores.get(row["id"], 0.0) >= settings["context_threshold"]]
    # Keep cards readable: the dispatching message itself and long OpenClaw essays are clipped.
    for row in picked:
        if len(row["text"]) > 800:
            row["text"] = row["text"][:799] + "…"
    return picked
