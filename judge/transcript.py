"""Read an OpenClaw session transcript as a flat list of user/assistant messages.

OpenClaw 2026.9.x keeps transcripts in the agent SQLite (`transcript_events`).
Only text is kept: tool calls, tool results and thinking are dropped, because the
judge decides relevance on what the user and the agent actually said to each other.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

AGENT_DB = Path.home() / ".openclaw/agents/main/agent/openclaw-agent.sqlite"

# "[message_id: om_x]\nou_x: text" -> "text"
_FEISHU_PREFIX = re.compile(r"^\[message_id: [^\]]+\]\s*\n(?:ou_[A-Za-z0-9]+|[^\n:]{1,40}):\s*", re.S)


@dataclass
class Message:
    seq: int
    role: str  # "user" | "assistant"
    time: int  # epoch ms
    text: str
    tool_calls: list  # assistant only: [{"name", "arguments"}]


def connect(path: Path = AGENT_DB) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def session_id_for(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "select session_id from session_windows where session_key = ? order by rowid desc limit 1", (key,)
    ).fetchone()
    return row[0] if row else None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _calls_of(content) -> list:
    if not isinstance(content, list):
        return []
    return [
        {"name": c.get("name"), "arguments": c.get("arguments") or {}}
        for c in content
        if isinstance(c, dict) and c.get("type") == "toolCall"
    ]


def messages(conn: sqlite3.Connection, session_id: str, since: int = 0, until: int | None = None) -> list[Message]:
    query = (
        "select seq, created_at, event_json from transcript_events "
        "where session_id = ? and created_at >= ? and json_extract(event_json, '$.message.role') in ('user', 'assistant')"
    )
    args: list = [session_id, since]
    if until is not None:
        query += " and created_at <= ?"
        args.append(until)
    query += " order by seq"
    out = []
    for seq, created, raw in conn.execute(query, args):
        event = json.loads(raw)
        msg = event["message"]
        role = msg["role"]
        text = _text_of(msg.get("content"))
        if role == "user":
            text = _FEISHU_PREFIX.sub("", text, count=1)
        calls = _calls_of(msg.get("content")) if role == "assistant" else []
        if not text.strip() and not calls:
            continue
        out.append(Message(seq=seq, role=role, time=created, text=text.strip(), tool_calls=calls))
    return out
