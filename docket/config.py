"""User settings: ~/Library/Application Support/docket/config.json

{
  "watch": ["*"],               // OpenClaw session keys whose dispatches go to the docket;
                                // "*" = every session (default); e.g. ["agent:main:main"] for the main chat only
  "context_threshold": 0.5,     // judge: earlier messages at or above this become the card's background
  "move_threshold": 0.85,       // judge: a main-thread message moves only at or above this
  "attribute": true,            // judge later main-thread messages at all
  "prefix": "",                 // a title prefix your dispatcher puts on OpenCode sessions, e.g. "[oc] ";
                                // "" = no prefix: every new session is checked against the dispatches, and a
                                // symbols-only marker before the title (the inbox plugin's emoji) still matches
  "locale": "auto",             // "auto" follows macOS, or "en" / "zh"
  "engine": "opencode",         // default coding agent for desk-dispatch: opencode | claude | codex
  "pm_model": null,             // model for PM sessions (openclaw agent --model); null = OpenClaw's default
  "pm_fallback_model": null,    // third try when the PM's reply failed validation twice; null = pm_model again
  "pm_agent": "main",           // OpenClaw agent the PMs run as: "docket-pm" once the installer created it
                                // (its own workspace and tool policy); "main" runs them in the main agent
  "fallback_model": null,       // OpenCode model ("provider/model") to retry once with after a usage limit; null = no retry
  "reconcile_max_age_hours": 6, // on restart a pending handoff idle longer than this is closed as overdue, not woken
  "health_dir": null,           // where the daily health summary goes; null = health/ in this folder
  "continue_limit": 5,          // after a run the PM may hand OpenCode the next piece itself this many times
                                // in a row; then it must show you where things stand (your answer resets it)
  "watchdog": "observe",        // "act": an instruction that never shows up in its session, goes quiet or ends
                                // unfinished is sent again once, then stalled on a card; "observe": the watchdog
                                // only logs what it would do (watchdog:observed) and an unfinished run gets a PM card
  "stale_enabled": true,        // look after cards nobody answers on the panel (the three settings below); false = off
  "stale_threshold": 0.9,       // judge: a card counts as answered (or made moot) in the main chat only at or
                                // above this; then its PM closes it, or relays the user's words to OpenCode
  "quiet_wake_minutes": 180,    // a card unanswered this long, with the session idle, wakes its PM once:
                                // close it, or ask one more specific question instead
  "quiet_expire_minutes": 720, // a card unanswered this long is closed by the docket itself, noted as expired;
                                // both clocks start when the card goes up on the panel, move on when the chat
                                // shows you still weighing it, and never before the first sweep with this on
  "judge": {                    // the model that picks a card's background and attributes main-thread messages
                                // with no key it is off: cards still register, without background or attribution
    "backend": "systemone",     // "systemone" (TypeSafe System One, probabilities) | "openai" (any OpenAI-compatible endpoint)
    "model": null,              // null = the backend's default: jev-1.14.0 for systemone; required for openai
    "endpoint": null,           // null = https://api.typesafe.ai/v1/systemone for systemone; the base URL for openai
    "keychain_service": "docket.judge" // the key's keychain item: this service, account = the backend name
  }
}

The judge's settings resolve config.json > DOCKET_JUDGE_BACKEND / DOCKET_JUDGE_MODEL (JEV_MODEL)
/ DOCKET_JUDGE_ENDPOINT > default; its key keychain > DOCKET_JUDGE_API_KEY (TYPESAFE_API_KEY)
> secrets.json `typesafe.api_key` (System One only). See judge/backend.py. No key is ever
written here. The daemon rereads this file when it changes, within one mention poll.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re

from docket.ledger import HOME
from judge.transcript import canonical_key

log = logging.getLogger("docket")

PATH = HOME / "config.json"
DEFAULTS = {"watch": ["*"], "context_threshold": 0.5, "move_threshold": 0.85, "attribute": True, "prefix": "", "locale": "auto", "engine": "opencode", "pm_model": None, "pm_fallback_model": None, "pm_agent": "main", "fallback_model": None, "reconcile_max_age_hours": 6, "health_dir": None, "watchdog": "observe", "continue_limit": 5, "stale_enabled": True, "stale_threshold": 0.9, "quiet_wake_minutes": 180, "quiet_expire_minutes": 720, "judge": {}}
# What the settings window may change; everything else in the file is kept as it is.
EDITABLE = ("pm_model", "pm_fallback_model", "judge")
JUDGE_KEYS = ("backend", "model", "endpoint", "keychain_service")


AGENT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def load() -> dict:
    try:
        user = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        user = {}
    if not isinstance(user, dict):
        user = {}
    merged = {**DEFAULTS}
    for name, value in user.items():
        if name == "pm_agent" and not (isinstance(value, str) and AGENT_ID.fullmatch(value)):
            log.warning("config.json: pm_agent = %r is not an agent id, using %r", value, DEFAULTS[name])
            continue
        if name in DEFAULTS and not fits(DEFAULTS[name], value):
            # A mistyped setting falls back to its default instead of taking the service down.
            log.warning("config.json: %s = %r is not valid, using the default %r", name, value, DEFAULTS[name])
            continue
        merged[name] = value
    return merged


def fits(default, value) -> bool:
    if default is None:
        return value is None or isinstance(value, str)
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, type(default))


def mtime() -> float | None:
    try:
        return PATH.stat().st_mtime
    except OSError:
        return None


def clean(changes: dict) -> dict:
    """Validate what the settings window sent: strings trimmed, empty means default. ValueError when off."""
    out = {}
    for name in ("pm_model", "pm_fallback_model"):
        if name in changes:
            value = changes[name]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            out[name] = (value or "").strip() or None
    if "judge" in changes:
        judge = changes["judge"] or {}
        if not isinstance(judge, dict):
            raise ValueError("judge must be an object")
        unknown = set(judge) - set(JUDGE_KEYS)
        if unknown:
            # A key has no place here: it lives in the keychain.
            raise ValueError(f"unknown judge settings: {', '.join(sorted(unknown))}")
        section = {}
        for name in JUDGE_KEYS:
            value = judge.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"judge.{name} must be a string")
            value = (value or "").strip()
            if value:
                section[name] = value
        if section.get("backend", "systemone") not in ("systemone", "openai"):
            raise ValueError("judge.backend must be systemone or openai")
        endpoint = section.get("endpoint")
        if endpoint and not endpoint.startswith(("https://", "http://")):
            raise ValueError("judge.endpoint must start with https:// or http://")
        out["judge"] = section
    return out


def save(changes: dict) -> dict:
    """Write the editable settings into config.json, keeping every other key; return load()."""
    changes = clean(changes)
    try:
        user = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(user, dict):
            user = {}
    except OSError:
        user = {}
    except json.JSONDecodeError as error:
        raise ValueError(f"{PATH} is not valid JSON; fix it by hand first ({error})") from None
    for name, value in changes.items():
        if value in (None, {}):
            user.pop(name, None)
        else:
            user[name] = value
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, PATH)
    return load()


def watched(config: dict, session_key: str) -> bool:
    # OpenClaw 2026.11 keys the heartbeat window of a chat `agent:main:main:heartbeat`: that is the same chat, judged by its plain key.
    session_key = canonical_key(session_key)
    # Cron and PM sessions (agent:main:docket-*, and agent:main:desk-* from before the rename) never hand work to the docket.
    if ":cron:" in session_key or ":docket-" in session_key or ":desk-" in session_key:
        return False
    return any(fnmatch.fnmatchcase(session_key, pattern) for pattern in config["watch"])
