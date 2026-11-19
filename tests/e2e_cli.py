# -*- coding: utf-8 -*-
"""Live check of a CLI engine (Claude Code by default), sandboxed like e2e_live.py.

    python3 -I tests/e2e_cli.py [claude|codex]

Dispatches straight to a temporary Docket service (no chat involved), lets the agent write
one file in a scratch directory, answers the brief once (a follow-up run via resume), and
closes. Everything it creates is deleted at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="desk-cli-"))
os.environ["DESK_HOME"] = str(SANDBOX)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import daemon, ledger  # noqa: E402

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "claude"
WORK = Path.home() / ".local/share/opencode/scratch/desk-cli-e2e"


def say(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def api(method, path, body=None):
    info = json.loads((SANDBOX / "daemon.json").read_text())
    request = urllib.request.Request(
        info["url"] + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"X-Desk-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for(predicate, label, timeout=900):
    start = time.time()
    while time.time() - start < timeout:
        found = predicate(api("GET", "/api/state"))
        if found:
            return found
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {label}: {json.dumps(api('GET', '/api/state'), ensure_ascii=False)[:800]}")


def main():
    (SANDBOX / "config.json").write_text(json.dumps({"watch": [], "attribute": False, "locale": "en"}))
    daemon.MEMORY_DIR = SANDBOX / "memory"
    daemon.MEMORY_DIR.mkdir()
    WORK.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=WORK)  # Codex wants a repository

    desk = daemon.Desk()
    threading.Thread(target=desk.watch_runs, daemon=True).start()
    threading.Thread(target=daemon.serve, args=(desk,), daemon=True).start()
    time.sleep(2)

    model = "haiku" if ENGINE == "claude" else None
    started = api("POST", "/api/dispatch", {
        "engine": ENGINE, "title": "Docket CLI self-check", "directory": str(WORK), "model": model,
        "instruction": "Create hello.txt in the current directory containing one line: hello desk. Then say in one sentence what you did.",
    })
    handoff = started["id"]
    say("dispatched", handoff)

    first = wait_for(lambda s: next((d for d in s["delegations"] if d["id"] == handoff and d["state"] == "awaiting"), None), "first brief")
    say("engine:", first["engine"], "| brief:", json.dumps(first["card"], ensure_ascii=False))
    say("hello.txt:", (WORK / "hello.txt").read_text().strip() if (WORK / "hello.txt").exists() else "(missing)")

    api("POST", f"/api/cards/{first['card']['id']}/decide", {"choices": {}, "reply": "Append a second line to hello.txt: second round."})
    second = wait_for(lambda s: next((d for d in s["delegations"] if d["id"] == handoff and d["state"] == "awaiting" and d["card"]["id"] != first["card"]["id"]), None), "second brief")
    say("second brief:", json.dumps(second["card"], ensure_ascii=False), "| rounds:", second["rounds"])
    say("hello.txt:", (WORK / "hello.txt").read_text().strip().replace("\n", " | ") if (WORK / "hello.txt").exists() else "(missing)")
    row = ledger.get(desk.db(), handoff)
    say("engine session kept across rounds:", row["engine_session"], "| runs:", desk.db().execute("select count(*) from runs where delegation_id = ?", (handoff,)).fetchone()[0])

    api("POST", f"/api/delegations/{handoff}/close", {"reply": ""})
    closed = wait_for(lambda s: next((d for d in s["delegations"] if d["id"] == handoff and d["state"] == "closed"), None), "closed")
    say("conclusion:", closed["card"].get("conclusion"))

    subprocess.run(["openclaw", "sessions", "delete", row["pm_key"], "--yes"], capture_output=True)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    say("ok")
    os._exit(0)


if __name__ == "__main__":
    main()
