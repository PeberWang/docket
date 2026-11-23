"""Which judge answers relevance's questions, and with which key.

Two backends answer the same System One request shape ({"answers": {id: {"noul": p}} or
{"choice", "probabilities", "confidence"}}), so relevance.py does not know which one ran:

- "systemone": TypeSafe System One (jev.py), a model that returns calibrated probabilities
- "openai": any OpenAI-compatible /chat/completions endpoint (openai_judge.py), whose
  JSON answers are read as probabilities

Settings come from config.json `judge` ({backend, model, endpoint, keychain_service}), then
the environment, then the per-backend default. The key comes from the keychain item named by
`keychain_service` (account = backend), then the environment, then, for System One only,
~/.openclaw/workspace/secrets.json at `typesafe.api_key` (the place older installs use).
The OpenAI-compatible backend never takes a key from secrets.json or OPENAI_API_KEY: those
belong to other tools, and its endpoint may be anyone's.

The key is never logged and never part of an error message.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from judge import keys

log = logging.getLogger("docket")

BACKENDS = {
    "systemone": {"model": "jev-1.14.0", "endpoint": "https://api.typesafe.ai/v1/systemone"},
    "openai": {"model": "", "endpoint": ""},
}
DEFAULT_BACKEND = "systemone"
KEYCHAIN_SERVICE = "docket.judge"
SECRETS = Path.home() / ".openclaw/workspace/secrets.json"
KEY_TTL = 60  # seconds a key read stays cached; saving one from the settings window refreshes it at once


class JudgeError(RuntimeError):
    pass


def resolve(judge: dict | None, env=None) -> dict:
    """config.json's judge section -> {backend, model, endpoint, keychain_service}, with defaults filled in."""
    judge = judge or {}
    env = os.environ if env is None else env
    backend = judge.get("backend") or env.get("DOCKET_JUDGE_BACKEND") or DEFAULT_BACKEND
    if backend not in BACKENDS:
        log.warning("unknown judge backend %r, using %s", backend, DEFAULT_BACKEND)
        backend = DEFAULT_BACKEND
    env_model = env.get("DOCKET_JUDGE_MODEL") or (env.get("JEV_MODEL") if backend == "systemone" else None)
    return {
        "backend": backend,
        "model": judge.get("model") or env_model or BACKENDS[backend]["model"],
        "endpoint": (judge.get("endpoint") or env.get("DOCKET_JUDGE_ENDPOINT") or BACKENDS[backend]["endpoint"]).rstrip("/"),
        "keychain_service": judge.get("keychain_service") or KEYCHAIN_SERVICE,
    }


def find_key(settings: dict, env=None, secrets: Path | None = None) -> tuple[str | None, str | None, str | None]:
    """Return (key, source, problem): source is "keychain", "env", "secrets" or None."""
    env = os.environ if env is None else env
    backend = settings["backend"]
    problem = None
    try:
        key = keys.read(settings["keychain_service"], backend)
        if key:
            return key, "keychain", None
    except keys.KeychainError as error:
        problem = str(error)
    names = ["DOCKET_JUDGE_API_KEY"] + (["TYPESAFE_API_KEY"] if backend == "systemone" else [])
    for name in names:
        if env.get(name):
            return env[name], "env", problem
    if backend == "systemone":
        try:
            data = json.loads((secrets or SECRETS).read_text(encoding="utf-8"))
            key = (data.get("typesafe") or {}).get("api_key")
            if key:
                return key, "secrets", problem
        except (OSError, ValueError, AttributeError):
            pass
    return None, None, problem


class Judge:
    """The judge the daemon uses: settings swapped in by configure(), the key cached for a minute."""

    def __init__(self):
        self.guard = threading.Lock()
        self.settings = resolve(None)
        self.cached: tuple[float, str | None, str | None, str | None] | None = None

    def configure(self, judge: dict | None) -> None:
        with self.guard:
            self.settings = resolve(judge)
            self.cached = None

    def key(self, fresh: bool = False) -> tuple[str | None, str | None, str | None]:
        with self.guard:
            settings, cached = self.settings, self.cached
        if fresh or not cached or time.monotonic() - cached[0] > KEY_TTL:
            key, source, problem = find_key(settings)
            if problem and (not cached or cached[3] != problem):
                log.warning("judge key: %s", problem)
            cached = (time.monotonic(), key, source, problem)
            with self.guard:
                if self.settings is settings:
                    self.cached = cached
        return cached[1], cached[2], cached[3]

    def missing(self) -> str | None:
        """Why the judge cannot run now, or None when it can."""
        settings = self.settings
        if not settings["endpoint"]:
            return "no endpoint"
        if not settings["model"]:
            return "no model"
        if not self.key()[0]:
            return "no key"
        return None

    def available(self) -> bool:
        return self.missing() is None

    def status(self) -> dict:
        """What the settings window shows: the resolved settings and, per backend, where its key comes from."""
        settings = self.settings
        backends = {}
        for name in BACKENDS:
            if name == settings["backend"]:
                _, source, problem = self.key(fresh=True)
            else:
                _, source, problem = find_key({**settings, "backend": name})
            backends[name] = {**BACKENDS[name], "configured": source is not None, "source": source, "problem": problem}
        return {**settings, "available": self.available(), "missing": self.missing(), "backends": backends}

    def ask(self, state, questions: dict) -> dict:
        settings = self.settings
        key = self.key()[0]
        if not key:
            raise JudgeError(f"no API key for the {settings['backend']} judge")
        if settings["backend"] == "openai":
            from judge import openai_judge

            return openai_judge.ask(state, questions, model=settings["model"], endpoint=settings["endpoint"], key=key)
        from judge import jev

        return jev.ask(state, questions, model=settings["model"], endpoint=settings["endpoint"], key=key)


current = Judge()


def configure(judge: dict | None) -> None:
    current.configure(judge)


def available() -> bool:
    return current.available()


def ask(state, questions: dict) -> dict:
    return current.ask(state, questions)


def scrub(text: str, key: str | None) -> str:
    """An error body may quote the key back (some servers echo part of it); never pass it on."""
    if key:
        text = text.replace(key, "***")
        if len(key) > 12:
            text = text.replace(key[:12], "***")
    return text
