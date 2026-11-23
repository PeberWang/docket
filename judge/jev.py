"""Minimal TypeSafe System One client (stdlib only).

The model, endpoint and key come from judge/backend.py; the key never goes through a
prompt, a log line or an error message.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from judge.backend import BACKENDS, JudgeError, scrub

ENDPOINT = BACKENDS["systemone"]["endpoint"]
MODEL = BACKENDS["systemone"]["model"]


class JevError(JudgeError):
    pass


def ask(state, questions: dict, model: str = MODEL, endpoint: str = ENDPOINT, key: str = "", retries: int = 4) -> dict:
    """POST one System One request; return {"answers", "usage", "model"}."""
    body = json.dumps({"model": model, "state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = scrub(error.read().decode("utf-8", "replace")[:500], key)
            if error.code in (429, 529) and attempt < retries:
                time.sleep(float(error.headers.get("retry-after") or 2 ** attempt))
                continue
            raise JevError(f"HTTP {error.code}: {detail}") from None
        except urllib.error.URLError as error:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise JevError(scrub(str(error.reason), key)) from None
    raise JevError("unreachable")
