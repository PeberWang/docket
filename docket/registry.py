"""Turn a new 🦞 OpenCode session into a docket delegation.

The session tells us the title and the time. The dispatch itself is found in the
OpenClaw transcripts (only watched sessions), and the judge picks the background.
"""

from __future__ import annotations

import datetime as dt
import json

from docket import config as docket_config
from docket import dispatches
from judge import jev, relevance
from judge.transcript import connect as openclaw_connect
from judge.transcript import messages

WINDOW_BEFORE = 20 * 60_000  # the dispatch call precedes the session by seconds; allow slack
WINDOW_AFTER = 2 * 60_000
CONTEXT_MESSAGES = 80


def find_dispatch(title: str, created_ms: int, settings: dict):
    """Return {"session_key", "session_id", "seq", "time", "directory", "directory_raw", "brief", "instruction"} or None."""
    conn = openclaw_connect()
    try:
        rows = conn.execute(
            "select w.session_key, t.session_id, t.seq, t.created_at, t.event_json from transcript_events t "
            "join session_windows w on w.session_id = t.session_id "
            "where t.created_at between ? and ? and json_extract(t.event_json, '$.message.role') = 'assistant' "
            "and t.event_json like ? order by t.created_at desc",
            (created_ms - WINDOW_BEFORE, created_ms + WINDOW_AFTER, f"%{title}%"),
        ).fetchall()
    finally:
        conn.close()
    for key, session_id, seq, created, raw in rows:
        if not docket_config.watched(settings, key):
            continue
        content = (json.loads(raw).get("message") or {}).get("content") or []
        for part in content if isinstance(content, list) else []:
            if part.get("type") != "toolCall" or part.get("name") != "exec":
                continue
            parsed = dispatches.parse((part.get("arguments") or {}).get("command", ""))
            if parsed and parsed["title"] == title:
                return {
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
    if not rows or not jev.available():
        return []
    task = {"title": title, "instruction": dispatch["instruction"], "brief": dispatch["brief"]}
    scores, _ = relevance.context_scores(task, rows)
    picked = [dict(row, score=round(scores.get(row["id"], 0.0), 3)) for row in rows if scores.get(row["id"], 0.0) >= settings["context_threshold"]]
    # Keep cards readable: the dispatching message itself and long OpenClaw essays are clipped.
    for row in picked:
        if len(row["text"]) > 800:
            row["text"] = row["text"][:799] + "…"
    return picked
