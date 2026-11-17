"""Recognise an OpenClaw -> OpenCode dispatch in a transcript tool call."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

PLACEHOLDER = re.compile(r"^[<.…]|任务名|task name", re.I)
PREFIX = re.compile(r"^🦞\s*")


def parse(command: str):
    """Return {"title", "directory", "brief_path", "instruction"} for a new-task dispatch, else None."""
    if not command or "--title" not in command or not re.search(r"desk-dispatch|opencode-dispatch|opencode run", command):
        return None
    directory = None
    match = re.match(r"\s*cd\s+(\S+)\s*&&", command)
    if match:
        directory = match.group(1)
    marker = next(m for m in ("desk-dispatch", "opencode-dispatch", "opencode run") if m in command)
    tail = re.split(r"\s(?:2>&1|\||;|&&)\s", command.split(marker, 1)[-1])[0]
    try:
        tokens = shlex.split(tail)
    except ValueError:
        return None
    title = brief = None
    rest = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--title" and i + 1 < len(tokens):
            title, i = tokens[i + 1], i + 2
            continue
        if token in ("--engine", "--model", "-m") and i + 1 < len(tokens):
            i += 2
            continue
        if token in ("-f", "--file") and i + 1 < len(tokens):
            brief, i = tokens[i + 1], i + 2
            continue
        if token in ("-s", "--session", "-c", "--continue"):
            return None
        if not token.startswith("-"):
            rest.append(token)
        i += 1
    if not title or PLACEHOLDER.search(title.strip()):
        return None
    return {"title": PREFIX.sub("", title.strip()), "directory": directory, "brief_path": brief, "instruction": rest[-1] if rest else ""}


def read_brief(directory, brief_path, limit: int = 6000) -> str:
    if not brief_path or "$(" in brief_path:
        return ""
    path = Path(brief_path).expanduser()
    if not path.is_absolute() and directory:
        path = Path(directory).expanduser() / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
