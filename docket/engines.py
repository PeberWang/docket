"""Coding agents the docket can hand work to.

Every engine answers the same five questions: how to start a task, how to know a run
ended, what the agent said last, how to send a follow-up, and how the user takes over.

- opencode: driven over the OpenCode service's HTTP API (2.2 and later): Docket creates the
  session with an id it chose (archived, the inbox prefix in the title), sends each instruction
  with its own message id, and follows the run through the event stream (see docket/daemon.py).
  Older services, and a run on another model, go through `opencode run -s` instead; handoffs
  started by `opencode-dispatch --detach` are registered when their session appears.
- claude, codex: started by the docket itself as a detached CLI process whose output goes
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

from docket import dispatches
from docket.ledger import HOME
from docket.opencode import Service, new_id

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
    # For `opencode run -s`: approve the session's own permission requests. Over HTTP there is no
    # client to approve them, so Docket answers each one itself, the same way (Docket.permission_card).
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


def model_ref(model: str | None) -> dict | None:
    """"provider/model" or "provider/model#variant", as the OpenCode CLI takes it, for the HTTP API."""
    if not model or "/" not in model:
        return None
    provider, _, name = model.partition("/")
    name, _, variant = name.partition("#")
    return {"providerID": provider, "id": name, **({"variant": variant} if variant else {})}


def session_of(handoff: dict) -> str:
    """The OpenCode session a handoff works in now: its own, unless the watchdog opened a new one."""
    return handoff.get("engine_session") or handoff["id"]


class OpenCode(Engine):
    name, label = "opencode", "OpenCode"

    def __init__(self, settings: dict):
        super().__init__(settings)
        # "auto" (HTTP when the service offers it), "http" or "cli": config.json "engines": {"opencode": {"transport": …}}
        self.transport = ((settings.get("engines") or {}).get(self.name) or {}).get("transport", "auto")

    def over_http(self, model: str | None = None) -> bool:
        """Whether this instruction goes over HTTP. A run on another model (the fallback after a usage
        limit) goes through the CLI: a prompt cannot pick a model, the session's stays."""
        if self.transport == "cli" or model:
            return False
        return self.transport == "http" or Service().drives_over_http()

    def _spawn(self, handoff, argv: list[str]) -> dict:
        RUNS.mkdir(parents=True, exist_ok=True)
        out = RUNS / f"{handoff['id']}-{uuid.uuid4().hex[:8]}.out"
        workdir = dispatches.existing_dir(handoff.get("directory") or Path.home())
        with open(out, "wb") as stdout:
            process = subprocess.Popen(
                argv, cwd=str(workdir),
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.STDOUT,
                start_new_session=True,  # the service's event stream reports when the run ends
            )
        return {"pid": process.pid, "out": str(out), "process": process}

    def open_session(self, handoff: dict, title: str) -> None:
        """The handoff's first session, with the handoff's own id (HTTP only; asking twice is harmless).
        Its model is the one the handoff names ("provider/model"), else OpenCode's default."""
        directory = str(dispatches.existing_dir(handoff["directory"])) if handoff.get("directory") else None
        Service().create_session(handoff["id"], title, directory, {"docket": {"handoff": handoff["id"]}}, model_ref(handoff.get("model")))

    def resume(self, handoff, text, model: str | None = None, message_id: str | None = None,
               metadata: dict | None = None, delivery: str | None = None) -> dict:
        """Hand the session an instruction. Over HTTP it carries Docket's own message id, so the
        watchdog confirms it by that id; through `opencode run -s` it confirms it by time. Neither
        a request that returned nor a process that started says the work started."""
        session = session_of(handoff)
        if self.over_http(model):
            item = Service().prompt(session, text, message_id, metadata, delivery)
            return {"message": item.get("id") or message_id, "transport": "http"}
        # `opencode run -s <unknown id>` would start a brand-new session: make sure it exists.
        Service().session(session)
        return {**self._spawn(handoff, ["opencode", "run", "-s", session, *(["--model", model] if model else []), *self.args, text]), "transport": "cli"}

    def start_session(self, handoff, title: str, text: str, model: str | None = None, wait: float = 30,
                      message_id: str | None = None, metadata: dict | None = None) -> tuple[str | None, dict]:
        """A new session for the same work (the old one is interrupted and will not take more),
        archived like the first. Returns (session id or None, the run)."""
        if self.over_http(model):
            fresh = new_id("ses", descending=True)
            directory = str(dispatches.existing_dir(handoff["directory"])) if handoff.get("directory") else None
            service = Service()
            service.create_session(fresh, title, directory, {"docket": {"handoff": handoff["id"]}})
            item = service.prompt(fresh, text, message_id, metadata)
            return fresh, {"message": item.get("id") or message_id, "transport": "http"}
        import time

        started = int(time.time() * 1000)
        run = self._spawn(handoff, ["opencode", "run", "--title", title, "--archived", *(["--model", model] if model else []), *self.args, text])
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                for session in Service().recent_sessions(limit=20):
                    if session.get("title") == title and (session.get("time") or {}).get("created", 0) >= started - 5000:
                        return session["id"], {**run, "transport": "cli"}
            except Exception:
                pass
            if run["process"].poll() is not None:
                break
            time.sleep(1)
        return None, {**run, "transport": "cli"}

    def interrupt(self, session: str) -> bool:
        return Service().interrupt(session)

    def answer_permission(self, handoff, request_id: str, decision: str):
        Service()._request("POST", f"/api/session/{session_of(handoff)}/permission/{request_id}/reply", {"decision": decision})

    def last_reply(self, handoff):
        return Service().last_reply(session_of(handoff))

    def takeover(self, handoff):
        request = Path.home() / "Library/Application Support/opencode-openclaw-inbox/open-session"
        request.parent.mkdir(parents=True, exist_ok=True)
        import time

        request.write_text(json.dumps({"sessionID": session_of(handoff), "at": int(time.time() * 1000)}), encoding="utf-8")
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
        workdir = dispatches.existing_dir(handoff.get("directory") or Path.home())
        with open(out, "wb") as stdout:
            process = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # survives a daemon restart
            )
        return {"pid": process.pid, "out": str(out), "process": process}

    def resume(self, handoff, text, model: str | None = None):
        raise RuntimeError("the daemon launches CLI follow-ups itself")  # see Docket.relay

    def last_reply(self, handoff):
        return ""  # comes from the run's output file

    def takeover(self, handoff):
        directory = str(dispatches.expand_dir(handoff.get("directory") or Path.home()))
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
