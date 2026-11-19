"""Talk to the local OpenCode background service (the one OpenCode Desktop runs).

Discovery: ~/.local/state/opencode/service.json holds the URL and the basic-auth
password (user "opencode"). The file is re-read on every reconnect because the port
changes when OpenCode restarts.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = Path.home() / ".local/state/opencode/service.json"

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

    def prompt(self, session_id: str, text: str):
        """Continue a session (what `opencode run -s` does, without a CLI process)."""
        return self._request("POST", f"/api/session/{session_id}/prompt", {"text": text}, timeout=60)

    def recent_sessions(self, limit: int = 50) -> list:
        result = self.get("/api/session", limit=limit)
        return result.get("data", result) if isinstance(result, dict) else result

    def events(self, types=EVENTS):
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
