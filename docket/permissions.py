"""Docket's side of opencode-permission-notify (six classes, three tiers; see its HANDOFF.md).

permission-notify decides every OpenCode permission request before OpenCode publishes it:
silent allows, pm allows and records it for Docket to review, human waits for the person in the
popup. What it agreed with Docket, and what this module does about it:

- a heartbeat file Docket touches while it runs (`pm-online`): without it, Docket counts as offline
  and class 1 falls back to the popup even where the person delegated it;
- its ledger (`ledger.jsonl`, one JSON line per decision): Docket reads what was decided in its
  handoffs' sessions, so the PM reviews what was allowed on its behalf, and the audit keeps it;
- the batches waiting for the person (`pending/<batch>.json`): a run waiting there is waiting for
  the person, not stuck.

Paths follow permission-notify's own environment variables, so both sides always agree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
ROOT = HOME / "Library/Application Support/opencode/permission-notify"
HEARTBEAT = Path(os.environ.get("PERMISSION_NOTIFY_PM_HEARTBEAT", ROOT / "pm-online"))
LEDGER = Path(os.environ.get("PERMISSION_NOTIFY_LEDGER", ROOT / "ledger.jsonl"))
PENDING = Path(os.environ.get("PERMISSION_NOTIFY_PENDING", ROOT / "pending"))

READ_LIMIT = 4 * 1024 * 1024  # bytes per pass: a long backlog is read over several passes


def installed() -> bool:
    return HEARTBEAT.parent.is_dir()


def heartbeat() -> bool:
    """Tell permission-notify that Docket is there (it counts 60 s without a touch as offline)."""
    if not installed():
        return False
    try:
        HEARTBEAT.touch()
        return True
    except OSError:
        return False


def read_ledger(offset: int | None) -> tuple[list[dict], int]:
    """Decisions written since `offset` (bytes), and the new offset. None: start at the end, so a
    first start never replays history. A ledger that shrank (permission-notify rotated it) is read
    from the start. A line still being written is left for the next pass."""
    try:
        size = LEDGER.stat().st_size
    except OSError:
        return [], offset or 0
    if offset is None:
        return [], size
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with LEDGER.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(READ_LIMIT)
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    out = []
    for line in chunk[: end + 1].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out, offset + end + 1


def waiting() -> list[dict]:
    """The batches waiting for the person right now: {batch, session, who, class, what, created}."""
    out = []
    try:
        files = sorted(PENDING.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("session"):
            out.append(entry)
    return out


def summary(entry: dict) -> dict:
    """What Docket keeps of one decision: enough for the audit and the PM, no full commands."""
    return {
        "class": str(entry.get("class") or ""),
        "class_name": entry.get("class_name"),
        "tier": entry.get("tier"),
        "effect": entry.get("effect"),
        "what": [str(r)[:160] for r in (entry.get("resources") or [])[:3]],
        "tool": entry.get("tool") or entry.get("action"),
        "by": entry.get("by"),
        "answer": entry.get("answer"),
        "note": entry.get("note"),
        "at": entry.get("ts"),
    }
