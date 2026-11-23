"""Recognise an OpenClaw -> OpenCode dispatch in a transcript tool call."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

PLACEHOLDER = re.compile(r"^[<.…]|任务名|task name", re.I)
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


def strip_prefix(title: str, prefix: str = "") -> str:
    """The task name: the title without the configured prefix (config.json `prefix`); "" strips nothing."""
    title, prefix = title.strip(), (prefix or "").strip()
    if prefix and title.startswith(prefix):
        title = title[len(prefix):].strip()
    return title


def same_task(session_title: str, dispatch_title: str, prefix: str = "") -> bool:
    """Whether a session (already stripped of the configured prefix) is the dispatch's.

    With no prefix configured, a dispatcher may still have put a short marker of its own before
    the title (opencode-dispatch adds the inbox plugin's, an emoji by default): symbols only, no
    letters or digits, then a space. A marker with letters in it ("[oc] ") has to be configured.
    """
    if session_title == dispatch_title:
        return True
    if (prefix or "").strip() or not dispatch_title or not session_title.endswith(dispatch_title):
        return False
    head = session_title[: -len(dispatch_title)]
    return head[-1:].isspace() and 0 < len(head.strip()) <= 4 and not any(c.isalnum() for c in head)


def parse(command: str, prefix: str = ""):
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
    return {"title": strip_prefix(title, prefix), "directory": directory, "directory_raw": raw_directory, "brief_path": brief, "instruction": rest[-1] if rest else ""}


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
