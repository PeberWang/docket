# -*- coding: utf-8 -*-
"""Live end-to-end check against the real OpenClaw and OpenCode, fully sandboxed.

    python3 -I tests/e2e_live.py

- ledger, config and the "memory" log live in a temporary DESK_HOME;
- the dispatch comes from a throwaway OpenClaw session (agent:main:e2e-desk-source),
  never the main session, and nothing is delivered to the chat;
- the OpenCode task writes one file in a scratch directory (model: $E2E_MODEL, else OpenCode's default).
Everything it creates is deleted at the end.
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

SANDBOX = Path(tempfile.mkdtemp(prefix="desk-e2e-"))
os.environ["DESK_HOME"] = str(SANDBOX)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import daemon, ledger  # noqa: E402
from desk.opencode import Service  # noqa: E402

SOURCE = "agent:main:e2e-desk-source"
WORK = Path.home() / ".local/share/opencode/scratch/desk-e2e"
TITLE = "案头端到端自检"


def say(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def api(method, path, body=None):
    info = json.loads((SANDBOX / "daemon.json").read_text())
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(info["url"] + path, data=data, method=method, headers={"X-Desk-Token": info["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_for(predicate, label, timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        state = api("GET", "/api/state")
        found = predicate(state)
        if found:
            return found
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {label}: {json.dumps(api('GET', '/api/state'), ensure_ascii=False)[:800]}")


def main():
    (SANDBOX / "config.json").write_text(json.dumps({"watch": [SOURCE], "attribute": False}))
    daemon.MEMORY_DIR = SANDBOX / "memory"
    daemon.MEMORY_DIR.mkdir()
    WORK.mkdir(parents=True, exist_ok=True)

    desk = daemon.Desk()
    threading.Thread(target=desk.watch_events, daemon=True).start()
    threading.Thread(target=daemon.serve, args=(desk,), daemon=True).start()
    time.sleep(2)

    say("asking OpenClaw (sandbox session) to dispatch")
    script = Path.home() / ".openclaw/workspace/skills/opencode-dispatch/scripts/opencode-dispatch"
    message = (
        "（案头自检，不是真实任务，不要发任何聊天消息。）请只做一件事：执行下面这条命令，然后只回复一句「已交办」。\n\n"
        f"cd {WORK} && {script} --detach --auto --title \"{TITLE}\" " + (f"--model {os.environ['E2E_MODEL']} " if os.environ.get("E2E_MODEL") else "") +
        "\"在当前目录创建 hello.txt，内容为一行 hello desk。完成后用一句话说明做了什么。\""
    )
    done = subprocess.run(["openclaw", "agent", "--session-key", SOURCE, "--json", "--timeout", "300", "--message", message], capture_output=True, text=True, timeout=400)
    say("OpenClaw:", (json.loads(done.stdout).get("result") or {}).get("meta", {}).get("finalAssistantVisibleText", done.stderr[-300:])[:200])

    first = wait_for(lambda s: next((d for d in s["delegations"] if d["title"] == TITLE and d["state"] == "awaiting"), None), "first card")
    say("first card:", json.dumps(first["card"], ensure_ascii=False))
    row = ledger.get(desk.db(), first["id"])
    say("context messages picked by the judge:", len(json.loads(row["context"] or "[]")))

    say("deciding: ask for a second line")
    api("POST", f"/api/cards/{first['card']['id']}/decide", {"choices": {}, "reply": "再在 hello.txt 末尾加一行 second round，就可以了。"})
    second = wait_for(
        lambda s: next((d for d in s["delegations"] if d["id"] == first["id"] and d["state"] == "awaiting" and d["card"]["id"] != first["card"]["id"]), None),
        "second card",
    )
    say("second card:", json.dumps(second["card"], ensure_ascii=False), "rounds:", second["rounds"])
    say("hello.txt:", (WORK / "hello.txt").read_text(encoding="utf-8").strip().replace("\n", " | ") if (WORK / "hello.txt").exists() else "(missing)")

    say("closing")
    api("POST", f"/api/delegations/{first['id']}/close", {"reply": ""})
    closed = wait_for(lambda s: next((d for d in s["delegations"] if d["id"] == first["id"] and d["state"] == "closed"), None), "closed")
    say("conclusion:", closed["card"].get("conclusion"))
    memory = list((SANDBOX / "memory").glob("*.md"))
    say("memory entry:", memory[0].read_text(encoding="utf-8").strip().splitlines()[-1] if memory else "(none)")

    # cleanup
    try:
        Service()._request("DELETE", f"/api/session/{first['id']}")
    except Exception as error:
        say("cleanup: cannot delete OpenCode session:", error)
    for key in (SOURCE, ledger.pm_key(first["id"])):
        subprocess.run(["openclaw", "sessions", "delete", key, "--yes"], capture_output=True, text=True)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    say("ok")
    os._exit(0)


if __name__ == "__main__":
    main()
