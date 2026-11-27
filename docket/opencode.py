"""Talk to the local OpenCode background service (the one OpenCode Desktop runs).

Discovery: ~/.local/state/opencode/service.json holds the URL and the basic-auth
password (user "opencode"). The file is re-read on every reconnect because the port
changes when OpenCode restarts.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = Path.home() / ".local/state/opencode/service.json"

_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_ids = threading.Lock()
_last = [0, 0]  # the millisecond of the last id, and how many were made in it


def new_id(prefix: str, descending: bool = False) -> str:
    """An id shaped like OpenCode's own: prefix, 12 hex digits of time (and a counter), 14 random
    characters. Sessions sort newest first (descending), messages oldest first, so an id Docket
    picks lines up with the ones OpenCode makes. Docket picks them so a retried request can never
    make a second session or message, and so it knows its own messages when it sees them."""
    with _ids:
        now = int(time.time() * 1000)
        _last[1] = _last[1] + 1 if now == _last[0] else 1
        _last[0] = now
        value = now * 0x1000 + _last[1]
    if descending:
        value = ~value
    stamp = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    return f"{prefix}_{stamp}{''.join(secrets.choice(_BASE62) for _ in range(14))}"


# What the HTTP engine needs from the service (OpenCode 2.2 and later). Older services are driven
# through `opencode run -s` instead (see engines.OpenCode).
NEEDED = ("session.create", "session.prompt", "session.interrupt", "session.update")
_capabilities: dict = {"at": 0, "value": None}
CAPABILITY_TTL = 600  # s: OpenCode can be updated (or rolled back) while Docket runs

# What the docket reacts to. form.created is OpenCode asking a structured question;
# permission.asked should not happen with --auto but is surfaced if it does.
EVENTS = (
    "session.created",
    "session.renamed",
    "session.execution.started",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "form.created",
    "permission.asked",
    # An instruction Docket sent (by the id it chose) has reached the agent: the turn is confirmed.
    "session.inbox.delivered",
)
# Signs of work, for the watchdog: a long build or test run is quiet in the messages, not here.
ACTIVITY = (
    "session.step.started",
    "session.step.ended",
    "session.tool.called",
    "session.tool.progress",
    "session.tool.success",
    "session.tool.failed",
    "session.retry.scheduled",
)


class Service:
    def __init__(self):
        info = json.loads(SERVICE.read_text(encoding="utf-8"))
        self.url = info["url"].rstrip("/")
        token = base64.b64encode(f"opencode:{info['password']}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def _request(self, method: str, path: str, body=None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def get(self, path: str, **params):
        query = urllib.parse.urlencode(params, doseq=True)
        return self._request("GET", path + (f"?{query}" if query else ""))

    def session(self, session_id: str) -> dict:
        result = self.get(f"/api/session/{session_id}")
        return result.get("data", result)

    def messages(self, session_id: str, limit: int = 20) -> list:
        """Newest first."""
        result = self.get(f"/api/session/{session_id}/message", limit=limit)
        return result.get("data", result) if isinstance(result, dict) else result

    @staticmethod
    def text_of(message: dict) -> str:
        parts = [c.get("text", "") for c in (message or {}).get("content") or [] if c.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()

    def last_reply(self, session_id: str) -> str:
        """Text of the session's latest assistant message (reasoning and tool parts dropped)."""
        for message in self.messages(session_id, limit=15):
            if message.get("type") != "assistant":
                continue
            parts = [c.get("text", "") for c in message.get("content") or [] if c.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
        return ""

    def capabilities(self) -> set:
        """The operations this service offers (from its own API description), cached a while."""
        if _capabilities["value"] is not None and time.time() - _capabilities["at"] < CAPABILITY_TTL:
            return _capabilities["value"]
        try:
            spec = self.get("/openapi.json") or {}
            value = {op.get("operationId") for path in (spec.get("paths") or {}).values() for op in path.values() if isinstance(op, dict)}
            prompt = (((spec.get("paths") or {}).get("/api/session/{sessionID}/prompt") or {}).get("post") or {})
            schema = ((prompt.get("requestBody") or {}).get("content") or {}).get("application/json", {}).get("schema") or {}
            if not {"id", "metadata", "delivery"} <= set((schema.get("properties") or {})):
                value.discard("session.prompt")  # a prompt without its own id cannot be confirmed exactly
        except Exception:
            value = set()
        _capabilities.update(at=time.time(), value=value)
        return value

    def drives_over_http(self) -> bool:
        return all(op in self.capabilities() for op in NEEDED)

    def create_session(self, session_id: str, title: str, directory: str | None, metadata: dict | None = None,
                       model: dict | None = None) -> dict:
        """A session with the id Docket chose (asking again returns the same one), archived like the inbox convention."""
        body = {"id": session_id, "title": title, "metadata": metadata or None}
        if directory:
            body["location"] = {"directory": directory}
        if model:
            body["model"] = model
        result = self._request("POST", "/api/session", body)
        session = (result or {}).get("data", result) or {}
        self.archive(session_id)
        return session

    def prompt(self, session_id: str, text: str, message_id: str | None = None, metadata: dict | None = None,
               delivery: str | None = None) -> dict:
        """Hand the session an instruction. With Docket's own id a retry never adds a second one, and
        session.inbox.delivered names it once the agent takes it in. steer reaches a busy agent before
        its next model call; queue waits until its current work is done."""
        body = {"text": text}
        if message_id:
            body["id"] = message_id
        if metadata:
            body["metadata"] = metadata
        if delivery:
            body["delivery"] = delivery
        result = self._request("POST", f"/api/session/{session_id}/prompt", body, timeout=60)
        if metadata and "docket" in metadata:
            # OpenCode unarchives a session that takes a prompt, and the inbox reads an unarchived
            # session as one the person took over: Docket's own instruction keeps it archived.
            self.archive(session_id)
        return (result or {}).get("data", result) or {}

    def archive(self, session_id: str) -> bool:
        try:  # the inbox convention; never worth failing an instruction over
            self._request("PATCH", f"/api/session/{session_id}", {"archived": True})
            return True
        except Exception:
            return False

    def interrupt(self, session_id: str) -> bool:
        """Stop the session's agent; False when nothing was running."""
        result = self._request("POST", f"/api/session/{session_id}/interrupt", {})
        return bool((result or {}).get("interrupted"))

    def inbox(self, session_id: str) -> list:
        """Instructions handed to the session and not delivered yet (queued behind its current work)."""
        result = self.get(f"/api/session/{session_id}/inbox")
        items = result.get("data", result) if isinstance(result, dict) else result
        return items if isinstance(items, list) else []

    def message(self, session_id: str, message_id: str) -> dict | None:
        """One message of the session, or None when it is not (yet) there."""
        try:
            result = self.get(f"/api/session/{session_id}/message/{message_id}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        return (result or {}).get("data", result) if isinstance(result, dict) else result

    def recent_sessions(self, limit: int = 50) -> list:
        result = self.get("/api/session", limit=limit)
        return result.get("data", result) if isinstance(result, dict) else result

    def events(self, types=EVENTS + ACTIVITY):
        """Yield decoded events forever (until the connection drops)."""
        query = urllib.parse.urlencode({"types": list(types)}, doseq=True)
        request = urllib.request.Request(f"{self.url}/api/event?{query}", headers=self.headers)
        with urllib.request.urlopen(request, timeout=90) as response:
            for line in response:
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
