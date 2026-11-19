"""Build the judge evaluation set from real OpenClaw -> OpenCode dispatches.

For every dispatch the main session made, keep:
  - the task (title, one-line instruction, brief file)
  - `before`: the last messages leading up to it (where its context lives)
  - `after`:  the conversation that followed (where stray mentions of it show up)

Output: eval/data/cases.json (git-ignored: it holds private conversation).

    python3 -I eval/extract_cases.py [--since 2026-11-12] [--key agent:main:main]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import dispatches  # noqa: E402
from judge.redact import redact  # noqa: E402
from judge.transcript import connect, messages, session_id_for  # noqa: E402

BEFORE_MESSAGES = 80
LOOKBACK_DAYS = 7
AFTER_HOURS = 36
MAX_MESSAGES = 150
MAX_CHARS = 1500
MAX_BRIEF = 4000


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_dispatch(command: str):
    parsed = dispatches.parse(command)
    return (parsed["title"], parsed["directory"], parsed["brief_path"], parsed["instruction"]) if parsed else None


def read_brief(directory, brief) -> str:
    return dispatches.read_brief(directory, brief, MAX_BRIEF)


OPENCODE_DB = Path.home() / ".local/share/opencode/opencode.db"


def opencode_rounds(title: str) -> list:
    """Instructions the delegation's OpenCode session received (first dispatch and every follow-up)."""
    import sqlite3

    conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    row = conn.execute("select id from session_v2 where title = ? and parent_id is null order by time_created limit 1", (f"🦞 {title}",)).fetchone()
    if not row:
        return []
    rounds = []
    for created, raw in conn.execute("select time_created, data from session_message where session_id = ? and type = 'user' order by seq", (row[0],)):
        text = json.loads(raw).get("text") or ""
        rounds.append({"ts": created, "text": redact(clip(text, 400))})
    return rounds


def as_rows(items):
    return [
        {
            "id": f"m{m.seq}",
            "ts": m.time,
            "role": m.role,
            "time": dt.datetime.fromtimestamp(m.time / 1000).strftime("%m-%d %H:%M"),
            "text": redact(clip(m.text, MAX_CHARS)),
        }
        for m in items
        if m.text.strip()
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-11-12")
    parser.add_argument("--key", default="agent:main:main")
    args = parser.parse_args()

    conn = connect()
    session = session_id_for(conn, args.key)
    if not session:
        sys.exit(f"no session for {args.key}")
    since = int(dt.datetime.fromisoformat(args.since).timestamp() * 1000)
    history = messages(conn, session, since=since - LOOKBACK_DAYS * 86400_000)

    cases, seen = [], set()
    for index, message in enumerate(history):
        if message.role != "assistant" or message.time < since:
            continue
        for call in message.tool_calls:
            command = call["arguments"].get("command") if call["name"] == "exec" else None
            parsed = parse_dispatch(command) if command else None
            if not parsed:
                continue
            title, directory, brief, instruction = parsed
            title = re.sub(r"^🦞\s*", "", title)
            if title in seen:
                continue
            seen.add(title)
            end = message.time + AFTER_HOURS * 3600_000
            before = history[max(0, index + 1 - BEFORE_MESSAGES) : index + 1]
            after = [m for m in history[index + 1 :] if m.time <= end][:MAX_MESSAGES]
            cases.append(
                {
                    "case": f"c{len(cases) + 1:02d}",
                    "title": title,
                    "dispatched_at": dt.datetime.fromtimestamp(message.time / 1000).isoformat(timespec="minutes"),
                    "dispatch_seq": message.seq,
                    "instruction": redact(instruction),
                    "brief": redact(read_brief(directory, brief)),
                    "before": as_rows(before),
                    "after": as_rows(after),
                    "rounds": opencode_rounds(title),
                }
            )

    out = ROOT / "eval/data/cases.json"
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    for case in cases:
        print(case["case"], case["dispatched_at"], case["title"], len(case["before"]), len(case["after"]), len(case["rounds"]), "rounds")
    print(f"{len(cases)} cases -> {out}")


if __name__ == "__main__":
    main()
