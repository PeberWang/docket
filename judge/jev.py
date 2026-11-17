"""Minimal TypeSafe System One client (stdlib only).

The key is read from TYPESAFE_API_KEY, else ~/.openclaw/workspace/secrets.json
at `typesafe.api_key`. It never goes through a prompt or a log line.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
SECRETS = Path.home() / ".openclaw/workspace/secrets.json"
MODEL = os.environ.get("JEV_MODEL", "jev-1.14.0")


class JevError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    try:
        secrets = json.loads(SECRETS.read_text(encoding="utf-8"))
    except OSError as error:
        raise JevError(f"no TYPESAFE_API_KEY and cannot read {SECRETS}") from error
    key = (secrets.get("typesafe") or {}).get("api_key")
    if not key:
        raise JevError(f"set typesafe.api_key in {SECRETS}")
    return key


def available() -> bool:
    try:
        api_key()
        return True
    except JevError:
        return False


def ask(state, questions: dict, model: str = MODEL, retries: int = 4) -> dict:
    """POST one System One request; return {"answers", "usage", "model"}."""
    body = json.dumps({"model": model, "state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            if error.code in (429, 529) and attempt < retries:
                time.sleep(float(error.headers.get("retry-after") or 2 ** attempt))
                continue
            raise JevError(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise JevError(str(error)) from error
    raise JevError("unreachable")
