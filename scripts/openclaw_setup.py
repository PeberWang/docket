#!/usr/bin/env python3
"""Wire OpenClaw for Docket, once and idempotently (architecture section 11, steps 3 to 5).

    openclaw_setup.py [--dry-run] [--no-plugin] [--no-pm-agent] [--no-skill]
    openclaw_setup.py --remove [--dry-run]     undo it: unlink the plugin and stop using the PM agent

1. the plugin: its folder in plugins.load.paths (what `plugins install --link` writes, but that command
   needs the Gateway stopped, while the Gateway applies load.paths live), enabled, conversation access
   for its prompt hook, and `docket` added to plugins.allow when that allowlist is set;
2. the PM agent docket-pm: its workspace in Docket's data directory, `openclaw agents add`, its
   tool policy, no first-run BOOTSTRAP.md; then "pm_agent": "docket-pm" in Docket's config.json;
3. the desk-dispatch skill, backing up an installed copy that was changed by hand.

Every change to openclaw.json goes through `openclaw config patch`, which validates it first.
--dry-run prints what would change, validates the patch, and changes nothing.
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "integrations/openclaw/plugin"
PM_AGENT = REPO / "integrations/openclaw/pm-agent"
SKILL = REPO / "integrations/openclaw/desk-dispatch"
AGENT_ID = "docket-pm"
PLUGIN_ID = "docket"


class Setup:
    def __init__(self, data: Path, openclaw_home: Path, dry_run: bool, run=None, out=print, plugin=True, pm_agent=True):
        self.plugin, self.pm_agent = plugin, pm_agent
        self.data = data
        self.home = openclaw_home
        self.dry_run = dry_run
        self.run = run or self._run
        self.out = out
        self.changed: list[str] = []

    # ---------- plumbing ----------

    @staticmethod
    def _run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=180)

    def say(self, text: str):
        self.out(("would " if self.dry_run else "") + text)

    def act(self, what: str, argv: list[str], stdin: str | None = None):
        self.say(what)
        if self.dry_run:
            return None
        done = self.run(argv, stdin)
        if done.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:4])} failed: {(done.stderr or done.stdout)[-400:]}")
        self.changed.append(what)
        return done

    def config(self) -> dict:
        """openclaw.json as it is now ({} when it cannot be read as JSON: then nothing is merged into its lists)."""
        done = self.run(["openclaw", "config", "file"])
        path = Path((done.stdout or "").strip().splitlines()[-1]).expanduser() if done.returncode == 0 and done.stdout.strip() else self.home / "openclaw.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def backup(self, path: Path):
        target = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        self.say(f"back up {path} to {target.name}")
        if not self.dry_run:
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)

    # ---------- the plan ----------

    def patch(self, config: dict) -> dict:
        """The one config patch: objects merge, arrays replace, so plugins.allow is written whole."""
        out: dict = {}
        if self.plugin:
            existing = ((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID)
            # A re-install leaves the person's own choices alone: a plugin they turned off stays off.
            entry = {"hooks": {"allowConversationAccess": True}} if isinstance(existing, dict) and existing.get("enabled") is False \
                else {"enabled": True, "hooks": {"allowConversationAccess": True}}
            plugins: dict = {"entries": {PLUGIN_ID: entry}}
            paths = ((config.get("plugins") or {}).get("load") or {}).get("paths")
            paths = paths if isinstance(paths, list) else []
            if str(PLUGIN) not in paths:
                plugins["load"] = {"paths": [*paths, str(PLUGIN)]}
            allow = (config.get("plugins") or {}).get("allow")
            if isinstance(allow, list) and PLUGIN_ID not in allow:
                plugins["allow"] = [*allow, PLUGIN_ID]
            out["plugins"] = plugins
        if self.pm_agent:
            agent = json.loads((PM_AGENT / "agent.json").read_text(encoding="utf-8"))
            agent.pop("//", None)
            agent["workspace"] = str(self.workspace)
            existing = ((config.get("agents") or {}).get("entries") or {}).get(AGENT_ID)
            if isinstance(existing, dict) and existing.get("tools"):
                agent.pop("tools")  # a tool policy already there (maybe edited by the person) is kept
            out["agents"] = {"entries": {AGENT_ID: agent}}
        return out

    @property
    def workspace(self) -> Path:
        return self.data / "pm-workspace"

    def install_plugin(self, config: dict):
        """The link itself goes into the config patch (plugins.load.paths); this only checks where from."""
        paths = ((config.get("plugins") or {}).get("load") or {}).get("paths") or []
        if str(PLUGIN) in paths:
            self.out(f"plugin already linked from {PLUGIN}")
            return
        if "/.worktrees/" in str(PLUGIN) and not self.dry_run:
            # A link keeps pointing here: a worktree that is removed later would take the plugin with it.
            raise RuntimeError(f"not linking the plugin from a worktree ({PLUGIN}); run the installer from the main checkout")
        self.say(f"link the plugin from {PLUGIN} (plugins.load.paths, in the patch below)")

    def pm_workspace(self):
        """Docket's own files: replaced on upgrade, but a copy someone edited is backed up first."""
        source = PM_AGENT / "workspace"
        for item in sorted(source.iterdir()):
            target = self.workspace / item.name
            if target.exists() and filecmp.cmp(item, target, shallow=False):
                continue
            if target.exists():
                self.backup(target)
            self.say(f"write {target}")
            if not self.dry_run:
                self.workspace.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                self.changed.append(f"write {target}")

    def add_agent(self, config: dict):
        if AGENT_ID in ((config.get("agents") or {}).get("entries") or {}):
            self.out(f"agent {AGENT_ID} exists")
            return
        self.act(f"create the agent {AGENT_ID}",
                 ["openclaw", "agents", "add", AGENT_ID, "--workspace", str(self.workspace), "--non-interactive", "--json"])

    def apply_patch(self, config: dict):
        body = json.dumps(self.patch(config), ensure_ascii=False, indent=2)
        self.out("openclaw.json patch:\n" + body)
        argv = ["openclaw", "config", "patch", "--stdin"]
        if self.dry_run:
            done = self.run([*argv, "--dry-run"], body)
            self.out("the patch validates" if done.returncode == 0 else f"the patch does not validate: {(done.stderr or done.stdout)[-400:]}")
            return
        self.act("patch openclaw.json", argv, body)

    def no_bootstrap(self):
        """A first-run BOOTSTRAP.md starts a getting-to-know-you conversation a PM must never have."""
        path = self.workspace / "BOOTSTRAP.md"
        if path.exists():
            self.say(f"remove {path}")
            if not self.dry_run:
                path.unlink()
                self.changed.append(f"remove {path}")

    def docket_config(self):
        path = self.data / "config.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            self.out(f"{path} is not valid JSON: set \"pm_agent\": \"{AGENT_ID}\" in it by hand")
            return
        if current.get("pm_agent") == AGENT_ID:
            return
        self.say(f"set \"pm_agent\": \"{AGENT_ID}\" in {path} (new handoffs; running ones keep their PM)")
        if not self.dry_run:
            if path.exists():
                self.backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({**current, "pm_agent": AGENT_ID}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.changed.append(f"set pm_agent in {path}")

    def skill(self):
        skills = self.home / "workspace/skills"
        if not skills.is_dir():
            self.out(f"no {skills}: the skill is not installed")
            return
        target = skills / "desk-dispatch"
        record = self.data / "installed-skill.sha256"
        if target.exists():
            installed = tree_hash(target)
            if installed == tree_hash(SKILL):
                self.out("skill desk-dispatch is current")
                return
            if not record.exists() or record.read_text().strip() != installed:
                self.backup(target)  # changed by hand since Docket installed it (or never installed by Docket)
        self.say(f"install the skill desk-dispatch into {target}")
        if not self.dry_run:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(SKILL, target, ignore=shutil.ignore_patterns("__pycache__"))
            (target / "scripts/desk-dispatch").chmod(0o755)
            link = target / "scripts/docket"
            if not link.exists():
                link.symlink_to(REPO / "scripts/docket")
            record.write_text(tree_hash(target) + "\n")
            self.changed.append(f"install the skill into {target}")


    def removal(self, config: dict) -> dict:
        """What --remove patches: the plugin out of load.paths, plugins.allow and plugins.entries. The
        docket-pm agent is left in place (it holds the PMs' session history); the command to delete it
        is printed instead."""
        plugins = config.get("plugins") or {}
        out: dict = {"entries": {PLUGIN_ID: None}}
        paths = (plugins.get("load") or {}).get("paths")
        if isinstance(paths, list):
            out["load"] = {"paths": [p for p in paths if str(p) != str(PLUGIN) and not str(p).rstrip("/").endswith("integrations/openclaw/plugin")]}
        if isinstance(plugins.get("allow"), list):
            out["allow"] = [p for p in plugins["allow"] if p != PLUGIN_ID]
        return {"plugins": out}

    def remove(self, config: dict):
        body = json.dumps(self.removal(config), ensure_ascii=False, indent=2)
        self.out("openclaw.json patch:\n" + body)
        argv = ["openclaw", "config", "patch", "--stdin"]
        if self.dry_run:
            done = self.run([*argv, "--dry-run"], body)
            self.out("the patch validates" if done.returncode == 0 else f"the patch does not validate: {(done.stderr or done.stdout)[-400:]}")
        else:
            self.act("unlink the Docket plugin", argv, body)
        if AGENT_ID in ((config.get("agents") or {}).get("entries") or {}):
            self.out(f"the agent {AGENT_ID} is kept with its PM sessions; to delete it: openclaw agents delete {AGENT_ID}")


def tree_hash(root: Path) -> str:
    """The skill's files, not the docket link the installer adds, nor caches."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-plugin", action="store_true")
    parser.add_argument("--no-pm-agent", action="store_true")
    parser.add_argument("--no-skill", action="store_true")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    data = Path(os.environ.get("DOCKET_HOME", Path.home() / "Library/Application Support/docket"))
    setup = Setup(data, Path(os.environ.get("OPENCLAW_HOME", Path.home() / ".openclaw")), args.dry_run,
                  plugin=not args.no_plugin, pm_agent=not args.no_pm_agent)
    if not shutil.which("openclaw"):
        print("openclaw is not on PATH: nothing to wire")
        return 1
    try:
        config = setup.config()
        if args.remove:
            setup.remove(config)
            return 0
        if not args.no_plugin:
            setup.install_plugin(config)
        if not args.no_pm_agent:
            setup.pm_workspace()
            setup.add_agent(config)
            setup.no_bootstrap()
        if not (args.no_plugin and args.no_pm_agent):
            setup.apply_patch(setup.config() if not args.dry_run else config)
        if not args.no_pm_agent:
            setup.docket_config()
        if not args.no_skill:
            setup.skill()
    except RuntimeError as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print("changed:" if setup.changed else "nothing to change", *setup.changed, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
