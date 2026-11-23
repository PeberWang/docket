# -*- coding: utf-8 -*-
"""Hand-run check of a takeover and handback round trip through the real HTTP API and scripts/handback.py.

    /usr/bin/python3 -I tests/e2e_handback.py

Sandboxed: a temporary DOCKET_HOME with its own ledger and daemon.json, the daemon's HTTP
server in this process, a scripted PM and a coding agent that only records what it is
sent. Nothing reaches OpenClaw, OpenCode or the installed Docket. Everything it creates is
deleted at the end.
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

SANDBOX = Path(tempfile.mkdtemp(prefix="docket-handback-"))
os.environ["DOCKET_HOME"] = str(SANDBOX)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import daemon, engines, ledger, pm  # noqa: E402

CARD = {"kind": "report", "progress": "first piece done", "decisions": [], "decided_for_you": []}


def say(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def api(method, path, body=None):
    info = json.loads((SANDBOX / "daemon.json").read_text())
    request = urllib.request.Request(
        info["url"] + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"X-Docket-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def handoff():
    return next(d for d in api("GET", "/api/state")["delegations"] if d["id"] == "ses_e2e")


def wait_for(predicate, label, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        found = predicate(handoff())
        if found:
            return found
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {label}: {json.dumps(handoff(), ensure_ascii=False)[:600]}")


def handback_script():
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/handback.py"), "ses_e2e"],
        capture_output=True, text=True, env={**os.environ, "DOCKET_HOME": str(SANDBOX)}, timeout=60,
    )


def main():
    (SANDBOX / "config.json").write_text(json.dumps({"watch": [], "attribute": False, "locale": "en"}))
    daemon.MEMORY_DIR = SANDBOX / "memory"
    sent, opened, woken = [], [], []
    engines.OpenCode.resume = lambda engine, row, text, model=None: sent.append(text)
    engines.OpenCode.takeover = lambda engine, row: opened.append(row["id"])
    engines.OpenCode.last_reply = lambda engine, row: "the user's own run in OpenCode finished"
    replies = [{"action": "relay", "message": "second piece: write the tests"}]

    def ask(key, message, validate, record=None):
        woken.append(message)
        return replies.pop(0)

    pm.ask = ask
    docket = daemon.Docket()
    threading.Thread(target=daemon.serve, args=(docket,), daemon=True).start()
    for _ in range(50):
        if (SANDBOX / "daemon.json").exists():
            break
        time.sleep(0.1)

    db = docket.db()
    ledger.add_delegation(db, id="ses_e2e", title="handback e2e", directory=None, source_key="k", dispatch_seq=1, dispatched_at=0, brief="", context=[])
    docket.show_card("ses_e2e", "report", CARD)
    say("1. a handoff waiting on the panel:", handoff()["state"])

    api("POST", "/api/delegations/ses_e2e/takeover", {})
    wait_for(lambda d: d["state"] == "taken_over", "taken_over")
    assert opened == ["ses_e2e"], opened
    say("2. taken over, still listed on the panel:", handoff()["state"])

    done = handback_script()
    say("3. scripts/handback.py:", done.stdout.strip() or done.stderr.strip())
    assert done.returncode == 0, done.stderr
    d = wait_for(lambda d: d["state"] == "running" and d, "running after the PM relayed")
    assert sent == ["second piece: write the tests"], sent
    assert "handed it back" in woken[0] and "own run in OpenCode" in woken[0], woken[0][:300]
    assert (d["rounds"], d["handbacks"]) == (0, 1), d
    say("4. back on the docket:", d["state"], f"rounds {d['rounds']}, handbacks {d['handbacks']}, relayed {sent!r}")

    again = handback_script()
    say("5. a second handback:", again.stderr.strip())
    assert again.returncode == 1 and "Traceback" not in again.stderr and len(again.stderr.strip().splitlines()) == 1, again.stderr

    replies.append(dict(CARD, progress="second piece done"))
    docket.on_run_end("ses_e2e", "succeeded", "tests written")
    d = wait_for(lambda d: d["state"] == "awaiting" and d, "the next card")
    say("6. the next run end is a card again:", d["card"]["progress"])
    say("OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
