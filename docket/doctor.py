"""docket doctor: every link Docket depends on, checked once, with the fix for each one that fails.

Read-only: it reads files and asks the services, and changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

from docket import config as docket_config
from docket import ledger

REPO = Path(__file__).resolve().parent.parent
LABEL = "io.github.peberwang.docket"
PLUGIN = REPO / "integrations/openclaw/plugin"
SKILL = REPO / "integrations/openclaw/desk-dispatch"
SETUP = f"/usr/bin/python3 -I {REPO / 'scripts/openclaw_setup.py'}"


class Check:
    def __init__(self, name: str, ok: bool | None, detail: str = "", fix: str = ""):
        self.name, self.ok, self.detail, self.fix = name, ok, detail, fix

    def line(self) -> str:
        mark = {True: "ok  ", False: "FIX ", None: "--  "}[self.ok]
        out = f"{mark}{self.name}" + (f": {self.detail}" if self.detail else "")
        return out + (f"\n      fix: {self.fix}" if self.ok is False and self.fix else "")


def run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def service() -> Check:
    path = ledger.HOME / "daemon.json"
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
        request = urllib.request.Request(found["url"] + "/api/state", headers={"X-Docket-Token": found["token"]})
        state = json.loads(urllib.request.urlopen(request, timeout=5).read())
    except Exception as error:
        return Check("Docket service", False, str(error)[:120], f"launchctl kickstart -k gui/{os.getuid()}/{LABEL}  (log: ~/Library/Logs/docket/daemon.log)")
    live = [d for d in state["delegations"] if d["state"] in ("running", "pending", "awaiting")]
    return Check("Docket service", True, f"pid {found.get('pid')}, {len(live)} handoffs live, {state['awaiting']} waiting for you")


def launch_agents() -> list[Check]:
    out = []
    for job, what in ((LABEL, "service launch agent"), (f"{LABEL}.app", "panel launch agent")):
        done = run(["launchctl", "print", f"gui/{os.getuid()}/{job}"])
        ok = bool(done and done.returncode == 0)
        out.append(Check(what, ok, "loaded" if ok else "not loaded", f"bash {REPO / 'scripts/install.sh'}"))
    return out


def opencode() -> Check:
    if not shutil.which("opencode"):
        return Check("OpenCode", None, "not installed (only needed for OpenCode handoffs)")
    from docket.opencode import Service

    try:
        over_http = Service().drives_over_http()
    except Exception as error:
        return Check("OpenCode", False, f"service not readable: {str(error)[:100]}", "start OpenCode (opencode serve, or open the app)")
    if over_http:
        return Check("OpenCode", True, "driven over HTTP")
    return Check("OpenCode", False, "older than 2.2: handoffs start through opencode-dispatch, with the limits in the README",
                 "update OpenCode to 2.2 or later")


def openclaw_config() -> dict | None:
    done = run(["openclaw", "config", "file"])
    path = Path(done.stdout.strip().splitlines()[-1]).expanduser() if done and done.returncode == 0 and done.stdout.strip() else Path.home() / ".openclaw/openclaw.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def openclaw(settings: dict) -> list[Check]:
    if not shutil.which("openclaw"):
        return [Check("OpenClaw", False, "openclaw is not on PATH", "install OpenClaw; the PM runs in it")]
    config = openclaw_config()
    if config is None:
        return [Check("OpenClaw config", None, "openclaw.json is not plain JSON: plugin and PM agent not checked")]
    plugins = config.get("plugins") or {}
    entry = (plugins.get("entries") or {}).get("docket") or {}
    linked = str(PLUGIN) in ((plugins.get("load") or {}).get("paths") or [])
    allow = plugins.get("allow")
    problems = [p for p, bad in (
        ("not linked from this checkout", not linked),
        ("not enabled", entry.get("enabled") is not True),
        ("missing from plugins.allow", isinstance(allow, list) and "docket" not in allow),
        ("no conversation access (no open-handoff list in prompts)", (entry.get("hooks") or {}).get("allowConversationAccess") is not True),
    ) if bad]
    out = [Check("OpenClaw plugin", not problems, "; ".join(problems) or "linked, enabled, allowed", SETUP)]
    agent = settings.get("pm_agent", "main")
    entries = (config.get("agents") or {}).get("entries") or {}
    if agent == "main":
        out.append(Check("PM agent", None, "PMs run in the main agent (pm_agent \"main\"); docket-pm is not used"
                         + ("" if "docket-pm" not in entries else ", although it exists")))
    elif agent not in entries:
        out.append(Check("PM agent", False, f"pm_agent is {agent!r}, which OpenClaw does not have", SETUP))
    else:
        workspace = Path(str(entries[agent].get("workspace") or "")).expanduser()
        stray = (workspace / "BOOTSTRAP.md").exists()
        ok = (workspace / "AGENTS.md").exists() and not stray
        out.append(Check("PM agent", ok, f"{agent}, workspace {'has a first-run BOOTSTRAP.md' if stray else 'ready' if ok else 'missing'}", SETUP))
    return out


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def skill() -> Check:
    target = Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")) / "workspace/skills/desk-dispatch"
    if not target.exists():
        return Check("desk-dispatch skill", False, "not installed", SETUP)
    current = tree_hash(target) == tree_hash(SKILL)
    return Check("desk-dispatch skill", current, "current" if current else "differs from this checkout", f"{SETUP} --no-plugin --no-pm-agent")


def judge(settings: dict) -> Check:
    from judge import backend

    backend.configure(settings.get("judge"))
    missing = backend.current.missing()
    name = backend.current.settings["backend"]
    if missing:
        return Check("judge", None, f"off ({missing} for {name}): cards come without background, chat is not attributed")
    return Check("judge", True, name)


def checks() -> list[Check]:
    settings = docket_config.load()
    return [service(), *launch_agents(), opencode(), *openclaw(settings), skill(), judge(settings)]


def report() -> tuple[str, int]:
    found = checks()
    failing = sum(1 for c in found if c.ok is False)
    lines = [c.line() for c in found]
    lines.append("" if failing else "all links hold")
    if failing:
        lines.append(f"{failing} to fix")
    return "\n".join(lines).strip(), 1 if failing else 0
