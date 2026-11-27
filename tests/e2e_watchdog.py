# -*- coding: utf-8 -*-
"""Live regression for the watchdog, against the real OpenClaw, OpenCode and PM, fully sandboxed.

    python3 -I tests/e2e_watchdog.py

From dispatch to the PM's verified report with nobody touching the panel, then once more
through `docket note`: the PM relays, the docket hands it to OpenCode, the watchdog sees the
turn running, the PM verifies again. At the end `docket status` must show every phase change.

Sandboxed like e2e_live.py: a temporary DOCKET_HOME, a throwaway OpenClaw session
(agent:main:e2e-docket-source) as the dispatcher, a scratch directory for the work. Everything
it creates is deleted at the end. Model: $E2E_MODEL, else OpenCode's default.

Run it from an ordinary terminal (Terminal.app, iTerm), not from inside an agent's sandboxed shell:
`openclaw agent` talks to the gateway on 127.0.0.1:18789 and writes under ~/.openclaw, and a
sandbox that blocks either makes it fail with nothing but "The CLI command failed". The script
checks that first and stops at the dispatch with the real exit code, stdout and stderr instead
of waiting out the timeout.
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

SANDBOX = Path(tempfile.mkdtemp(prefix="docket-e2e-watchdog-"))
os.environ["DOCKET_HOME"] = str(SANDBOX)
os.environ["DOCKET_HEALTH_DIR"] = str(SANDBOX / "health")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import cli, daemon, ledger  # noqa: E402
from docket.opencode import Service  # noqa: E402

SOURCE = "agent:main:e2e-docket-source"
WORK = Path.home() / ".local/share/opencode/scratch/docket-e2e-watchdog"
TITLE = "Docket watchdog self-check"


def say(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def api(method, path, body=None):
    info = json.loads((SANDBOX / "daemon.json").read_text())
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(info["url"] + path, data=data, method=method, headers={"X-Docket-Token": info["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for(predicate, label, timeout=900):
    start = time.time()
    while time.time() - start < timeout:
        found = predicate(api("GET", "/api/state"))
        if found:
            return found
        time.sleep(3)
    abort(f"timed out waiting for {label}: {json.dumps(api('GET', '/api/state'), ensure_ascii=False)[:800]}")


REMEDY = (
    "Remedy: run it from an ordinary Terminal.app/iTerm window, outside any agent sandbox, with your real HOME and Homebrew on PATH:\n"
    f"  cd {ROOT} && env HOME=\"$HOME\" PATH=\"/opt/homebrew/bin:$PATH\" /usr/bin/python3 -I tests/e2e_watchdog.py\n"
    "  If it still fails there: `openclaw gateway status` must say \"Connectivity check: ok\" (else `openclaw gateway start`), then `openclaw doctor`."
)


def abort(*lines, code=2):
    """Stop now, say why, and leave nothing behind (daemon threads would keep a plain exit waiting)."""
    for line in lines:
        print(line, file=sys.stderr, flush=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    os._exit(code)


def tail(text, n=1500):
    text = (text or "").strip()
    return ("…" + text[-n:]) if len(text) > n else (text or "(empty)")


def diagnose(stderr="") -> list[str]:
    """What in this environment can stop `openclaw agent`: the binary, HOME, ~/.openclaw, the gateway."""
    found = []
    binary = shutil.which("openclaw")
    if not binary:
        found.append(f"openclaw is not on PATH (PATH={os.environ.get('PATH', '')})")
    state = Path.home() / ".openclaw"
    if not state.is_dir():
        found.append(f"no {state}: HOME={os.environ.get('HOME')} is not the home OpenClaw was installed under")
    elif not os.access(state, os.W_OK):
        found.append(f"{state} is not writable here (sandbox?)")
    if binary:
        try:
            probe = subprocess.run([binary, "gateway", "status", "--json"], capture_output=True, text=True, timeout=60)
            rpc = (json.loads(probe.stdout or "{}").get("rpc") or {}) if probe.returncode == 0 else {}
            if not rpc.get("ok"):
                found.append(f"gateway not reachable from here (exit {probe.returncode}): {tail(probe.stderr or probe.stdout, 300)}")
        except (subprocess.TimeoutExpired, ValueError, OSError) as error:
            found.append(f"gateway probe failed: {error}")
    lowered = (stderr or "").lower()
    for words, meaning in (
        (("econnrefused", "econnreset", "socket hang up"), "the gateway refused or dropped the connection"),
        (("eacces", "eperm", "operation not permitted", "read-only file system"), "the sandbox denied a socket or file operation (network to 127.0.0.1 or a write under ~/.openclaw)"),
        (("unauthorized", "forbidden", "missing scope", "invalid token", "auth failed"), "gateway credentials were rejected (gateway.auth.token in ~/.openclaw/.env)"),
        (("session not found", "unknown session"), "the session key was not accepted"),
    ):
        if any(w in lowered for w in words):
            found.append(f"stderr suggests {meaning}")
    return found or ["nothing wrong found in PATH, HOME, ~/.openclaw or the gateway; the cause is in the stderr above"]


def preflight():
    problems = [p for p in diagnose() if not p.startswith("nothing wrong")]
    if problems:
        abort("cannot reach OpenClaw from this environment, not starting:", *(f"  - {p}" for p in problems), REMEDY)


def dispatch(message):
    command = ["openclaw", "agent", "--session-key", SOURCE, "--json", "--timeout", "300", "--message", message]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=400)
    except (OSError, subprocess.TimeoutExpired) as error:
        abort(f"dispatch failed: {' '.join(command[:6])} … could not run: {error}", *(f"  - {p}" for p in diagnose()), REMEDY)
    try:
        reply = json.loads(done.stdout or "{}")
    except ValueError:
        reply = {}
    if done.returncode != 0 or reply.get("status") not in ("ok", None) or not reply:
        abort(
            f"dispatch failed: openclaw agent exited {done.returncode}, status {reply.get('status')!r}",
            f"--- stderr\n{tail(done.stderr)}",
            f"--- stdout\n{tail(done.stdout)}",
            "--- checks",
            *(f"  - {p}" for p in diagnose(done.stderr)),
            REMEDY,
        )
    return ((reply.get("result") or {}).get("meta") or {}).get("finalAssistantVisibleText") or " ".join(
        p.get("text") or "" for p in (reply.get("result") or {}).get("payloads") or []
    )


def mine(state, handoff=None):
    return next((d for d in state["delegations"] if (d["id"] == handoff if handoff else d["title"] == TITLE)), None)


def main():
    preflight()
    (SANDBOX / "config.json").write_text(json.dumps({"watch": [SOURCE], "attribute": False, "stale_enabled": False}))
    daemon.MEMORY_DIR = SANDBOX / "memory"
    daemon.MEMORY_DIR.mkdir()
    WORK.mkdir(parents=True, exist_ok=True)

    docket = daemon.Docket()
    for target, name in ((docket.watch_events, "events"), (docket.sweep, "sweep")):
        threading.Thread(target=target, name=name, daemon=True).start()
    threading.Thread(target=daemon.serve, args=(docket,), daemon=True).start()
    time.sleep(2)

    say("asking OpenClaw (sandbox session) to dispatch")
    script = Path.home() / ".openclaw/workspace/skills/opencode-dispatch/scripts/opencode-dispatch"
    message = (
        "(A Docket self-check, not a real task: do not send any chat message.) Do one thing only: run the command below, then reply with exactly one line, \"Handed off.\"\n\n"
        f"cd {WORK} && {script} --detach --auto --title \"{TITLE}\" " + (f"--model {os.environ['E2E_MODEL']} " if os.environ.get("E2E_MODEL") else "") +
        "\"Create hello.txt in the current directory containing one line, hello docket. When done, say in one sentence what you did.\""
    )
    said = dispatch(message)
    say("OpenClaw:", said[:200])
    # OpenClaw answered; the handoff must reach the docket within minutes, or it never ran the command.
    wait_for(lambda s: mine(s), f"the dispatch to register (OpenClaw said: {said[:200]!r}; did it run opencode-dispatch?)", timeout=180)

    first = wait_for(lambda s: (d := mine(s)) and d["state"] == "awaiting" and d, "the PM's first report")
    handoff = first["id"]
    say("first card:", first["card"].get("progress"), "| phase:", first["phase"])

    say("passing a word on through docket note")
    result = api("POST", f"/api/delegations/{handoff}/note", {"text": "Append a second line, second round, to hello.txt, and that is all.", "source": "e2e"})
    say("note:", result)
    second = wait_for(lambda s: (d := mine(s, handoff)) and d["state"] == "awaiting" and d["card"]["id"] != first["card"]["id"] and d, "the PM's second report")
    say("second card:", second["card"].get("progress"), "| phase:", second["phase"])
    say("hello.txt:", (WORK / "hello.txt").read_text(encoding="utf-8").strip().replace("\n", " | ") if (WORK / "hello.txt").exists() else "(missing)")

    status = cli.status(handoff)
    print(status, flush=True)
    phases = [r[0][6:] for r in docket.db().execute("select kind from events where delegation_id = ? and kind like 'phase:%' order by id", (handoff,))]
    turns = [dict(r) for r in docket.db().execute("select id, via, attempt, confirmed_at, outcome from turns where delegation_id = ? order by id", (handoff,))]
    problems = []
    if phases.count("running") < 2:
        problems.append(f"expected two confirmed runs, phases were {phases}")
    if any(t["confirmed_at"] is None for t in turns):
        problems.append(f"a turn was never seen running: {turns}")
    if docket.db().execute("select count(*) from events where delegation_id = ? and kind in ('watchdog:stalled', 'docket:failed')", (handoff,)).fetchone()[0]:
        problems.append("the docket stalled or failed along the way")
    for change in ("dispatching", "running", "verifying", "awaiting_user"):
        if change not in phases:
            problems.append(f"phase {change} never seen")

    say("closing")
    api("POST", f"/api/delegations/{handoff}/close", {"reply": ""})
    wait_for(lambda s: (d := mine(s, handoff)) and d["state"] == "closed", "closed", timeout=300)

    try:
        Service()._request("DELETE", f"/api/session/{handoff}")
    except Exception as error:
        say("cleanup: cannot delete OpenCode session:", error)
    for key in (SOURCE, ledger.pm_key(handoff)):
        subprocess.run(["openclaw", "sessions", "delete", key, "--yes"], capture_output=True, text=True)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    if problems:
        say("FAILED:", *problems)
        os._exit(1)
    say("ok: turns", [(t["via"], t["attempt"], t["outcome"]) for t in turns])
    os._exit(0)


if __name__ == "__main__":
    main()
