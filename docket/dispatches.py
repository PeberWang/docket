"""Recognise an OpenClaw -> OpenCode dispatch in a transcript tool call."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

PLACEHOLDER = re.compile(r"^[<.…]|任务名|task name", re.I)
PREFIX = re.compile(r"^🦞\s*")
HOME_VAR = re.compile(r"\$(?:HOME\b|\{HOME\})")


class MissingDirectory(RuntimeError):
    def __init__(self, raw: str, expanded: str):
        super().__init__(f"project directory not found: {raw} -> {expanded}")
        self.raw, self.expanded = raw, expanded


def expand_dir(raw) -> Path:
    """Read `~`, `$HOME` and `${HOME}` the way the dispatching shell did; Path and subprocess read none of them."""
    # $HOME goes through Path.home() so a daemon started without HOME in its environment still expands it.
    text = HOME_VAR.sub(lambda _: str(Path.home()), str(raw))
    return Path(os.path.expanduser(os.path.expandvars(text))).resolve()


def existing_dir(raw) -> Path:
    path = expand_dir(raw)
    if not path.is_dir():
        raise MissingDirectory(str(raw), str(path))
    return path


def parse(command: str):
    """Return {"title", "directory", "brief_path", "instruction"} for a new-task dispatch, else None."""
    if not command or "--title" not in command or not re.search(r"desk-dispatch|opencode-dispatch|opencode run", command):
        return None
    directory = raw_directory = None
    match = re.match(r"\s*cd\s+(\S+)\s*&&", command)
    if match:
        # `cd ~/…` and `cd "$HOME/…"` are common; subprocess expands neither, so store the real path.
        raw_directory = match.group(1).strip("'\"")
        directory = str(expand_dir(raw_directory))
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
    return {"title": PREFIX.sub("", title.strip()), "directory": directory, "directory_raw": raw_directory, "brief_path": brief, "instruction": rest[-1] if rest else ""}


def read_brief(directory, brief_path, limit: int = 6000) -> str:
    if not brief_path or "$(" in brief_path:
        return ""
    path = Path(brief_path)
    if not path.is_absolute() and not brief_path.startswith(("~", "$")) and directory:
        path = Path(directory) / path
    path = expand_dir(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
