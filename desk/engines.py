"""Coding agents the desk can hand work to.

Every engine answers the same five questions: how to start a task, how to know a run
ended, what the agent said last, how to send a follow-up, and how the user takes over.

- opencode: started by `opencode-dispatch --detach` (the inbox convention); runs are
  followed through the OpenCode service's event stream (see desk/daemon.py).
- claude, codex: started by the desk itself as a detached CLI process whose output goes
  to a file; the supervisor notices the process exit and reads the file. A restart of
  the daemon does not kill a run (new session group), and is picked up from the file.

Taking over a CLI task opens Terminal at the project with the agent's resume command.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path

from desk.ledger import HOME
from desk.opencode import Service

RUNS = HOME / "runs"

# Nobody is there to answer approval prompts. Override per engine in config.json:
#   "engines": {"claude": {"args": [...]}, "codex": {"args": [...]}}
DEFAULT_ARGS = {
    # Claude Code docs (headless mode): -p starts in Manual mode unless a mode is set;
    # acceptEdits writes files, the allow list covers shell and web, and
    # --permission-prompts none denies anything else instead of waiting (v2.1.259+).
    "claude": [
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch",
        "--permission-prompts", "none",
    ],
    # Codex docs (non-interactive mode): exec is read-only by default and --full-auto is
    # gone; workspace-write lets it edit the project inside the sandbox.
    "codex": ["--sandbox", "workspace-write", "--skip-git-repo-check"],
    # Follow-ups go through `opencode run -s`, like opencode-dispatch: a prompt sent with the
    # HTTP API would leave the session waiting on permission requests nobody answers.
    "opencode": ["--auto"],
}


class Engine:
    name = ""
    label = ""

    def __init__(self, settings: dict):
        self.args = ((settings.get("engines") or {}).get(self.name) or {}).get("args", DEFAULT_ARGS.get(self.name, []))

    def resume(self, handoff: dict, text: str):
        raise NotImplementedError

    def last_reply(self, handoff: dict) -> str:
        raise NotImplementedError

    def takeover(self, handoff: dict):
        raise NotImplementedError


class OpenCode(Engine):
    name, label = "opencode", "OpenCode"

    def resume(self, handoff, text):
        # `opencode run -s <unknown id>` would start a brand-new session: make sure it exists.
        Service().session(handoff["id"])
        RUNS.mkdir(parents=True, exist_ok=True)
        out = RUNS / f"{handoff['id']}-{uuid.uuid4().hex[:8]}.out"
        with open(out, "wb") as stdout:
            subprocess.Popen(
                ["opencode", "run", "-s", handoff["id"], *self.args, text],
                cwd=handoff.get("directory") or str(Path.home()),
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                start_new_session=True,  # the service's event stream reports when the run ends
            )

    def answer_permission(self, handoff, request_id: str, decision: str):
        Service()._request("POST", f"/api/session/{handoff['id']}/permission/{request_id}/reply", {"decision": decision})

    def last_reply(self, handoff):
        return Service().last_reply(handoff["id"])

    def takeover(self, handoff):
        request = Path.home() / "Library/Application Support/opencode-openclaw-inbox/open-session"
        request.parent.mkdir(parents=True, exist_ok=True)
        import time

        request.write_text(json.dumps({"sessionID": handoff["id"], "at": int(time.time() * 1000)}), encoding="utf-8")
        subprocess.run(["open", "-a", "OpenCode"], capture_output=True)


class Cli(Engine):
    """A coding agent driven through its non-interactive CLI."""

    binary = ""

    def command(self, handoff: dict, prompt: str, first: bool, out: Path) -> list[str]:
        raise NotImplementedError

    def resume_command(self, handoff: dict) -> str:
        raise NotImplementedError

    def parse(self, handoff: dict, out: Path) -> dict:
        """{"outcome": "succeeded" | "failed", "reply": str, "session": str | None}"""
        raise NotImplementedError

    def launch(self, handoff: dict, prompt: str, first: bool) -> dict:
        RUNS.mkdir(parents=True, exist_ok=True)
        out = RUNS / f"{handoff['id']}-{uuid.uuid4().hex[:8]}.out"
        argv = self.command(handoff, prompt, first, out)
        with open(out, "wb") as stdout:
            process = subprocess.Popen(
                argv,
                cwd=handoff.get("directory") or str(Path.home()),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # survives a daemon restart
            )
        return {"pid": process.pid, "out": str(out), "process": process}

    def resume(self, handoff, text):
        raise RuntimeError("the daemon launches CLI follow-ups itself")  # see Desk.relay

    def last_reply(self, handoff):
        return ""  # comes from the run's output file

    def takeover(self, handoff):
        directory = handoff.get("directory") or str(Path.home())
        command = f"cd {shlex.quote(directory)} && {self.resume_command(handoff)}"
        script = command.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", 'tell application "Terminal" to activate', "-e", f'tell application "Terminal" to do script "{script}"'],
            capture_output=True,
        )


class Claude(Cli):
    name, label, binary = "claude", "Claude Code", "claude"

    def command(self, handoff, prompt, first, out):
        session = ["--session-id", handoff["engine_session"]] if first else ["--resume", handoff["engine_session"]]
        model = ["--model", handoff["model"]] if handoff.get("model") else []
        return [self.binary, "-p", prompt, "--output-format", "json", *session, *model, *self.args]

    def resume_command(self, handoff):
        return f"claude --resume {shlex.quote(handoff['engine_session'])}"

    def parse(self, handoff, out):
        text = Path(out).read_text(encoding="utf-8", errors="replace").strip()
        result = None
        for line in reversed(text.splitlines()):  # the JSON result is the last line; warnings may precede it
            try:
                result = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(result, dict):
            return {"outcome": "failed", "reply": text[-2000:], "session": None}
        failed = result.get("is_error") or result.get("subtype") not in (None, "success")
        return {"outcome": "failed" if failed else "succeeded", "reply": result.get("result") or "", "session": result.get("session_id")}


class Codex(Cli):
    name, label, binary = "codex", "Codex", "codex"

    def command(self, handoff, prompt, first, out):
        last = str(out) + ".last"
        model = ["-m", handoff["model"]] if handoff.get("model") else []
        options = ["--json", "-o", last, *model, *self.args]  # exec options go before the resume subcommand
        if first:
            return [self.binary, "exec", *options, prompt]
        return [self.binary, "exec", *options, "resume", handoff["engine_session"], prompt]

    def resume_command(self, handoff):
        return f"codex resume {shlex.quote(handoff['engine_session'])}"

    def parse(self, handoff, out):
        session, reply, failed = None, "", False
        for line in Path(out).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type", "")
            if kind == "thread.started":
                session = event.get("thread_id")
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                reply = event["item"].get("text") or reply
            elif kind in ("turn.failed", "error"):
                failed = True
        last = Path(str(out) + ".last")
        if last.exists():
            reply = last.read_text(encoding="utf-8", errors="replace").strip() or reply
        return {"outcome": "failed" if failed else "succeeded", "reply": reply, "session": session}


ENGINES = {engine.name: engine for engine in (OpenCode, Claude, Codex)}


def get(name: str, settings: dict) -> Engine:
    return ENGINES.get(name or "opencode", OpenCode)(settings)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a child of this process that exited is a zombie until reaped
        finished, _ = os.waitpid(pid, os.WNOHANG)
        return finished == 0
    except ChildProcessError:
        return True
