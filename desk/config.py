"""User settings: ~/Library/Application Support/docket/config.json

{
  "watch": ["*"],               // OpenClaw session keys whose dispatches go to the desk;
                                // "*" = every session (default); e.g. ["agent:main:main"] for the main chat only
  "context_threshold": 0.5,     // judge: earlier messages at or above this become the card's background
  "move_threshold": 0.85,       // judge: a main-thread message moves only at or above this
  "attribute": true,            // judge later main-thread messages at all
  "prefix": "🦞 ",              // same convention as opencode-openclaw-inbox
  "locale": "auto",             // "auto" follows macOS, or "en" / "zh"
  "engine": "opencode",         // default coding agent for desk-dispatch: opencode | claude | codex
  "pm_model": null,             // model for PM sessions (openclaw agent --model); null = OpenClaw's default
  "fallback_model": "deepseek/deepseek-flash"  // coding agent retries once with this when a run hits its usage limit
}
"""

from __future__ import annotations

import fnmatch
import json

from desk.ledger import HOME

PATH = HOME / "config.json"
DEFAULTS = {"watch": ["*"], "context_threshold": 0.5, "move_threshold": 0.85, "attribute": True, "prefix": "🦞 ", "locale": "auto", "engine": "opencode", "pm_model": None, "fallback_model": "deepseek/deepseek-flash"}


def load() -> dict:
    try:
        user = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        user = {}
    return {**DEFAULTS, **user}


def watched(config: dict, session_key: str) -> bool:
    # Heartbeat and cron sessions never hand work to the desk.
    if session_key.endswith(":heartbeat") or ":cron:" in session_key or ":desk-" in session_key:
        return False
    return any(fnmatch.fnmatchcase(session_key, pattern) for pattern in config["watch"])
