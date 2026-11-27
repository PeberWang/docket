"""The docket daemon: watches OpenCode, wakes PMs, keeps the ledger, serves the panel.

    /usr/bin/python3 -I docket/daemon.py   (launchd runs this; see scripts/install.sh)

Threads:
  events     OpenCode /api/event -> register delegations, wake the PM when a run ends
  runs       CLI engine processes (Claude Code, Codex) -> wake the PM when one exits
  mentions   polls watched OpenClaw transcripts for new user messages -> judge attribution
  workers    PM calls (seconds to minutes each), one at a time per delegation
  http       127.0.0.1 API for Docket.app (see app/SPEC.md)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config as docket_config  # noqa: E402
from docket import dispatches, engines, i18n, ledger, pm, registry, risk  # noqa: E402
from docket.opencode import ACTIVITY as ACTIVITY_EVENTS  # noqa: E402
from docket.opencode import Service  # noqa: E402
from docket.opencode import new_id as opencode_id  # noqa: E402
from judge import backend, relevance, transcript  # noqa: E402
from judge.transcript import connect as openclaw_connect  # noqa: E402
from judge.transcript import messages as transcript_messages  # noqa: E402

DISCOVERY = ledger.HOME / "daemon.json"
MEMORY_DIR = Path.home() / ".openclaw/workspace/memory"
HEALTH_DIR = Path(os.environ.get("DOCKET_HEALTH_DIR", Path.home() / ".openclaw/workspace/cron-logs/docket"))
MENTION_POLL = 5
# s a judged chat message waits for the main agent to pass it on with docket note itself (its first tool
# call usually comes well inside this). A note that comes later still costs no second round (paid_for).
MENTION_DELAY = 30
# The watchdog (see Docket.watchdog), fixed on purpose: these are not settings. The grace periods
# sit one heartbeat inside the promises they keep: an instruction that has not shown up is sent
# again within 90 s, a turn silent for 10 minutes within those 10 minutes, a PM's stray answer
# is found within 2 minutes of it.
HEARTBEAT = 30                  # seconds between its looks
DISPATCH_GRACE = 55_000         # ms an instruction may take to show up in the session and make it busy
QUIET_AFTER = 9 * 60_000        # ms a running turn may go without a new message or a changed file
ORPHAN_WINDOW = 48 * 3600_000   # ms after a handoff closed that a PM reply outside Docket is still looked at
UNFINISHED = ("failed", "interrupted", "killed")  # run endings that never count as a delivery
LIVE = ("running", "pending", "awaiting")
# What a session's outcome looks like once no run is going on any more.
TERMINAL_OUTCOMES = ("succeeded", "failed", "interrupted")
# OpenCode permission actions and tool names, sorted by whether they can change files.
READ_ACTIONS = {"read", "glob", "grep", "list"}
WRITE_ACTIONS = {"edit", "write", "patch", "apply_patch", "multiedit"}

log = logging.getLogger("docket")


class Docket:
    def __init__(self):
        self.settings_mtime = docket_config.mtime()
        self.settings = docket_config.load()
        self.locale = i18n.resolve(self.settings.get("locale"))
        self.t = i18n.Strings(self.locale)
        backend.configure(self.settings.get("judge"))
        missing = backend.current.missing()
        if missing:
            log.warning("judge off (%s for %s): no background picking, no main-thread attribution", missing, backend.current.settings["backend"])
        self.local = threading.local()
        self.workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pm")
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()
        self.jobs: dict[str, int] = {}  # PM jobs submitted and not yet finished, per handoff
        self.run_ends: dict[str, tuple] = {}  # the newest run end per handoff whose write_card has not started yet
        pm.configure(self.locale, self.settings.get("pm_model"), self.settings.get("pm_fallback_model"))
        self.settings_guard = threading.Lock()
        db = self.db()
        if not db.execute("select value from kv where key = 'installed_at'").fetchone():
            db.execute("insert into kv values ('installed_at', ?)", (str(ledger.now()),))
        # When the unanswered-card sweep first ran with stale_enabled; its clocks never start earlier (see sweep_stale).
        since = db.execute("select value from kv where key = 'stale_since'").fetchone()
        self.stale_since = int(since[0]) if since else None

    # ---------- plumbing ----------

    @property
    def judging(self) -> bool:
        return backend.available()

    @property
    def acting(self) -> bool:
        """The watchdog sends instructions again and stalls them ("watchdog": "act"); otherwise it only observes."""
        return self.settings.get("watchdog") == "act"

    def observe(self, delegation_id: str, turn, would: str, why: str, **detail):
        """Observe mode: what the watchdog would have done, logged once per turn and reason (watchdog:observed)."""
        if ledger.claim(self.db(), f"observed:{turn['id']}:{why}"):
            ledger.log(self.db(), delegation_id, "watchdog:observed", {"turn": turn["id"], "attempt": turn["attempt"], "would": would, "why": why, **detail})
            log.info("watchdog (observing): %s would %s (%s)", delegation_id, would, why)

    def was_observed(self, turn_id: int, why: str) -> bool:
        return bool(self.db().execute("select 1 from kv where key = ?", (f"observed:{turn_id}:{why}",)).fetchone())

    def reload_settings(self, force: bool = False) -> bool:
        """Pick up config.json when it changed: the PM's models and the judge apply from the next call.

        The locale stays what it was at start (the panel and the PM prompts follow it).
        """
        with self.settings_guard:
            stamp = docket_config.mtime()
            if not force and stamp == self.settings_mtime:
                return False
            self.settings_mtime = stamp
            settings = docket_config.load()
            pm.configure(self.locale, settings.get("pm_model"), settings.get("pm_fallback_model"))
            backend.configure(settings.get("judge"))
            self.settings = settings
        judge = backend.current.settings
        log.info("settings reloaded: pm_model=%s judge=%s/%s", settings.get("pm_model") or "default", judge["backend"], judge["model"] or "-")
        return True

    def settings_view(self) -> dict:
        """What the settings window shows: never a key, only whether one is there and where from."""
        return {
            "pm_model": self.settings.get("pm_model"),
            "pm_fallback_model": self.settings.get("pm_fallback_model"),
            "judge": backend.current.status(),
            # As written in config.json, so the window shows defaults as placeholders rather than saving them.
            "judge_config": self.settings.get("judge") or {},
            "applies": "next_poll",
        }

    def save_settings(self, changes: dict) -> dict:
        docket_config.save(changes)
        self.reload_settings(force=True)
        return self.settings_view()

    def db(self):
        if not hasattr(self.local, "conn"):
            self.local.conn = ledger.connect()
        return self.local.conn

    def lock(self, delegation_id: str) -> threading.Lock:
        with self.locks_guard:
            return self.locks.setdefault(delegation_id, threading.Lock())

    def busy(self, delegation_id: str) -> bool:
        """A PM job for this handoff is running or waiting for a worker."""
        with self.locks_guard:
            return self.jobs.get(delegation_id, 0) > 0

    def submit(self, delegation_id: str, job, *args):
        """Queue a PM job for a handoff. It is written down first (audit S6): a restart runs it again
        instead of leaving the person's answer counted and never carried out."""
        with self.locks_guard:
            self.jobs[delegation_id] = self.jobs.get(delegation_id, 0) + 1
        name = getattr(job, "__name__", "")
        recorded = ledger.add_job(self.db(), delegation_id, name, list(args)) if getattr(job, "__self__", None) is self else None

        def run():
            try:
                self.run_job(delegation_id, job, args, recorded)
            finally:
                with self.locks_guard:
                    self.jobs[delegation_id] -= 1

        return self.workers.submit(run)

    def resume_jobs(self):
        """Jobs a restart cut off: run them again for live handoffs (their handoff's lock keeps order),
        and drop the ones whose handoff has ended."""
        db = self.db()
        for row in ledger.job_rows(db):
            ledger.drop_job(db, row["id"])
            handoff = self.delegation(row["delegation_id"])
            # A closing's conclusion still runs on a closed handoff: that is where its memory entry comes from.
            may_run = handoff and (handoff["state"] in LIVE or row["name"] == "conclude")
            job = getattr(self, row["name"], None) if may_run else None
            if job is None:
                ledger.log(db, row["delegation_id"], "job:dropped", {"job": row["name"], "args": row["args"][:200],
                                                                     "state": handoff["state"] if handoff else None})
                continue
            ledger.log(db, row["delegation_id"], "job:resumed", {"job": row["name"], "args": row["args"][:200]})
            log.info("resumed %s for %s after the restart", row["name"], row["delegation_id"])
            self.submit(row["delegation_id"], job, *json.loads(row["args"]))

    def run_job(self, delegation_id: str, job, args, job_id: int | None = None):
        with self.lock(delegation_id):
            try:
                job(*args)
            except dispatches.MissingDirectory as error:
                log.warning("project directory not found for %s: %r -> %s", delegation_id, error.raw, error.expanded)
                self.fail_card(delegation_id, self.t("missing_dir", raw=error.raw, expanded=error.expanded))
            except pm.PMFailed as error:
                log.warning("PM failed for %s: %s", delegation_id, error)
                raw = " ".join(error.raw.split())[:120] or "-"
                self.fail_card(delegation_id, self.t("pm_failed", raw=raw), job, args, str(error))
            except Exception as error:  # a PM failure must not take the daemon down
                log.exception("job failed for %s", delegation_id)
                self.fail_card(delegation_id, self.t("docket_fault", error=str(error)[:200]), job, args, str(error))
            finally:
                ledger.drop_job(self.db(), job_id)  # done, failed and shown, or handed to a card's retry

    def ask_pm(self, row: dict, message: str, validate):
        """pm.ask, with every unusable reply kept verbatim in the events for later.

        Only the docket wakes a PM and carries out what it answers. So before each wake the PM
        is told whether its previous action landed, and messages that reached its session from
        anywhere else since the last wake are folded into this one (its replies to them were
        never carried out). The wake's time window is logged: that is how the orphan sweep tells
        the docket's own turns in the PM's transcript from anyone else's.
        """
        def record(attempt, model, raw, errors):
            ledger.log(self.db(), row["id"], "pm:invalid", {"attempt": attempt, "model": model, "errors": errors, "raw": raw[:8000]})

        folded = self.fold_orphans(row)
        head = pm.record_lines(self.last_exec_line(row["id"]), folded)
        start = ledger.now()
        try:
            prompt = "\n\n".join(part for part in (pm.contract(), head, message) if part)
            return pm.ask(row["pm_key"], prompt, validate, record=record)
        finally:
            ledger.log(self.db(), row["id"], "pm:wake", {"start": start, "end": ledger.now()})

    def executed(self, delegation_id: str, kind: str, **detail):
        """What became of a PM's action: exec:relay | exec:card | exec:close | exec:fault."""
        ledger.log(self.db(), delegation_id, f"exec:{kind}", detail or None)

    def last_exec_line(self, delegation_id: str) -> str | None:
        """One sentence for the PM on whether its previous action landed."""
        db = self.db()
        last = db.execute(
            "select at, kind, detail from events where delegation_id = ? and kind like 'exec:%' order by id desc limit 1", (delegation_id,)
        ).fetchone()
        if not last:
            return None
        kind, detail, at = last["kind"][5:], json.loads(last["detail"] or "{}"), self.clock(last["at"])
        if kind == "relay" and detail.get("blocked"):
            return f"relay, refused by Docket at {at}: the round budget is spent."
        if kind == "relay":
            turn = db.execute("select * from turns where id = ?", (detail.get("turn"),)).fetchone() if detail.get("turn") else None
            turn = turn and db.execute("select * from turns where delegation_id = ? and id >= ? order by id desc limit 1", (delegation_id, turn["id"])).fetchone()
            if not turn:
                return f"relay, handed to the coding agent at {at}."
            text = f"relay, handed to OpenCode at {at}"
            if turn["attempt"] > 1:
                text += f" (sent again by the docket's watchdog, attempt {turn['attempt']})"
            if turn["confirmed_at"]:
                text += f"; OpenCode was seen working on it at {self.clock(turn['confirmed_at'])}"
            else:
                text += "; OpenCode has not been seen working on it"
            if turn["ended_at"]:
                text += f"; the run ended {turn['outcome']} at {self.clock(turn['ended_at'])}"
            return text + "."
        if kind == "card":
            card = db.execute("select * from cards where id = ?", (detail.get("card"),)).fetchone() if detail.get("card") else None
            if not card:
                return f"card, refused by Docket at {at} (the round budget is spent)." if detail.get("blocked") else f"card, at {at}."
            resolution = json.loads(card["resolution"] or "{}")
            how = resolution.get("action") or ("answered" if "choices" in resolution or resolution.get("reply") else None)
            return f"card, put on the panel at {at}; " + (f"it was {how}." if card["resolved_at"] else "it is still waiting.")
        if kind == "close":
            return f"close, the handoff was closed at {at}."
        return f"it could not be carried out ({detail.get('error') or 'docket fault'}), at {at}; Docket showed a failure card."

    @staticmethod
    def clock(ms: int) -> str:
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%H:%M")

    def show_card(self, delegation_id: str, kind: str, body: dict, reopen: bool = False):
        """Queue the card if one is already waiting; the panel never swaps an unanswered card.

        Once the user has spent every round no card gets through but the limit card,
        whatever the PM's prompt says. A taken-over or closed handoff takes no card unless
        `reopen` says the card is meant to open it again. Returns None when the card was refused.
        """
        row = ledger.get(self.db(), delegation_id)
        if row and row["state"] in ledger.STICKY and not reopen:
            ledger.log(self.db(), delegation_id, "card:dropped", {"kind": kind, "state": row["state"], "progress": str(body.get("progress") or "")[:200]})
            return None
        # The one exception: confirming a relay a round already paid for.
        if row and row["rounds"] >= ledger.ROUND_LIMIT and kind != "limit" and not body.get("round_spent"):
            ledger.log(self.db(), delegation_id, "card:blocked", {"kind": kind, "progress": str(body.get("progress") or "")[:200]})
            self.enter_limit(delegation_id)
            return None
        card_id, queued = ledger.push_card(self.db(), delegation_id, kind, body)
        if queued:
            ledger.log(self.db(), delegation_id, "card:queued", {"card": card_id, "kind": kind})
            if not ledger.open_card(self.db(), delegation_id):  # only queued cards were left: the oldest goes up
                ledger.next_queued(self.db(), delegation_id)
        # A card is on show either way, so the panel must show it: never leave the handoff pending.
        ledger.set_state(self.db(), delegation_id, "awaiting", force=reopen)
        return card_id

    def advance(self, delegation_id: str):
        """After a card is answered without a PM job to follow: put the handoff where its facts say."""
        self.settle(delegation_id)

    def settle(self, delegation_id: str, wake: bool = True) -> str | None:
        """The state a live handoff's facts imply (architecture, invariant 4): a card on show (or the next
        queued one) means awaiting, an instruction in flight means running. When neither applies, nothing
        runs and nothing is asked, so the PM is woken once to say what comes next, at no cost to the user."""
        db = self.db()
        row = ledger.get(db, delegation_id)
        if not row or row["state"] in ("closed", "taken_over", "limit"):
            return row["state"] if row else None
        if ledger.open_card(db, delegation_id) or ledger.next_queued(db, delegation_id):
            ledger.set_state(db, delegation_id, "awaiting")
            return "awaiting"
        # The docket's own record of an instruction in flight first; a session seen busy (someone else's run) second.
        if ledger.open_turn(db, delegation_id) or self.session_busy(dict(row)) is True:
            ledger.set_state(db, delegation_id, "running")
            return "running"
        if not wake:
            return row["state"]
        ledger.log(db, delegation_id, "settle:next-step", {"was": row["state"]})
        ledger.set_state(db, delegation_id, "pending")
        self.submit(delegation_id, self.next_step, delegation_id)
        return "pending"

    def resume_after_permission(self, delegation_id: str):
        """A permission request answered: the run that waited on it goes on and ends with its own
        event, so the next queued card goes up, or the handoff is running again."""
        db = self.db()
        nxt = ledger.open_card(db, delegation_id) or ledger.next_queued(db, delegation_id)
        ledger.set_state(db, delegation_id, "awaiting" if nxt else "running")

    def next_step(self, delegation_id: str):
        """Nothing runs and nothing waits: the PM puts up where things stand, or closes."""
        row = self.delegation(delegation_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        action = self.ask_pm(row, pm.next_step_wake(row["rounds"]), pm.validate_next_step)
        self.carry_out(delegation_id, action, via="next-step")

    # Jobs a failure card can run again: their arguments are plain strings, kept in the card.
    RETRYABLE = ("write_card", "act", "wrap_up", "relay", "settle_card", "redispatch", "rescue_orphan", "recover", "unfinished")

    def fail_card(self, delegation_id: str, message: str, job=None, args=(), error: str | None = None,
                  extra: dict | None = None, question: str | None = None, options: list | None = None, reopen: bool = False):
        """The docket's own failure: a card that stays open with a retry. It never closes the handoff.

        Stalled work (`extra` {"stalled": True}) is shown the same way: the panel marks both. On a
        taken-over or closed handoff the failure is recorded but shown only when `reopen` says so."""
        db = self.db()
        row = ledger.get(db, delegation_id)
        if not row:
            return
        if row["state"] in ledger.STICKY and not reopen:
            ledger.log(db, delegation_id, "docket:failed", {"error": (error or message)[:500], "job": getattr(job, "__name__", None), "shown": False})
            return
        body = {"progress": message[:300], "decisions": [], "decided_for_you": [], "docket_fault": True, **(extra or {})}
        name = getattr(job, "__name__", None)
        if name in self.RETRYABLE and getattr(job, "__self__", None) is self:
            body["pm_retry"] = {"job": name, "args": list(args)}
            body["decisions"] = [{
                "id": "retry",
                "question": question or self.t("pm_retry_question"),
                "options": options or [{"id": "yes", "label": self.t("pm_retry"), "recommended": True}],
            }]
        ledger.log(db, delegation_id, "docket:failed", {"error": (error or message)[:500], "job": name, **({"stalled": True} if body.get("stalled") else {})})
        self.executed(delegation_id, "fault", error=(error or message)[:300], job=name)
        if row["rounds"] >= ledger.ROUND_LIMIT:
            # The budget is spent, but the docket's own fault must still be seen and retried:
            # it takes the limit card's place, and the retry puts the limit card back.
            db.execute(
                "update cards set resolved_at = ?, resolution = ? where delegation_id = ? and resolved_at is null and kind = 'limit'",
                (ledger.now(), json.dumps({"action": "superseded", "note": "docket failure"}), delegation_id),
            )
            ledger.add_card(db, delegation_id, "failure", body)
            ledger.set_state(db, delegation_id, "awaiting", force=reopen)
            return
        self.show_card(delegation_id, "failure", body, reopen=reopen)

    def enter_limit(self, delegation_id: str):
        """The last round is spent: put up the limit card and withdraw every other waiting card."""
        row = ledger.get(self.db(), delegation_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        if self.db().execute("select 1 from cards where delegation_id = ? and kind = 'limit' and resolved_at is null", (delegation_id,)).fetchone():
            if row["state"] != "limit":
                ledger.set_state(self.db(), delegation_id, "limit")
            return
        self.db().execute(
            "update cards set resolved_at = ?, resolution = ? where delegation_id = ? and resolved_at is null",
            (ledger.now(), json.dumps({"action": "superseded", "note": "limit"}), delegation_id),
        )
        ledger.add_card(self.db(), delegation_id, "limit", {"progress": self.t("limit", n=ledger.ROUND_LIMIT), "decisions": [], "decided_for_you": []})
        ledger.set_state(self.db(), delegation_id, "limit")

    def delegation(self, delegation_id: str) -> dict | None:
        row = ledger.get(self.db(), delegation_id)
        return dict(row) if row else None

    def engine(self, row: dict) -> engines.Engine:
        return engines.get(row.get("engine"), self.settings)

    # ---------- CLI engines: the docket starts and follows the process ----------

    def dispatch_cli(self, engine_name: str, title: str, directory: str, instruction: str, brief: str, model: str | None) -> str:
        engine = engines.get(engine_name, self.settings)
        if not isinstance(engine, engines.Cli):
            raise ValueError(f"{engine_name} is not a CLI engine")
        import uuid

        try:
            directory = str(dispatches.existing_dir(directory))
        except dispatches.MissingDirectory as error:
            log.warning("dispatch directory not found: %r -> %s", error.raw, error.expanded)
            raise ValueError(self.t("missing_dir", raw=error.raw, expanded=error.expanded)) from error
        handoff_id = f"{engine.name}_{uuid.uuid4().hex[:20]}"
        session = str(uuid.uuid4()) if engine.name == "claude" else None
        ledger.add_delegation(
            self.db(), id=handoff_id, title=title, directory=directory, source_key=None, dispatch_seq=None,
            dispatched_at=ledger.now(), brief=brief, context=[], engine=engine.name, engine_session=session, model=model,
        )
        prompt = instruction if not brief else f"{instruction}\n\n<file name=\"brief.md\">\n{brief}\n</file>"
        self.launch(handoff_id, prompt, first=True)
        threading.Thread(target=self.attach_context, args=(handoff_id, title, instruction, brief), daemon=True).start()
        return handoff_id

    def dispatch_opencode(self, title: str, directory: str, instruction: str, brief: str, model: str | None) -> str:
        """Start an OpenCode handoff over HTTP: Docket makes the session (the handoff's own id, archived,
        the inbox prefix in the title) and hands it the first instruction the way it sends every other.
        The ledger row comes first, so the session appearing is never a second registration (audit 4A-7)
        and the first run's events are never missed (W1). LookupError: this OpenCode cannot be driven
        over HTTP, and the caller starts the handoff with opencode-dispatch instead."""
        engine = engines.get("opencode", self.settings)
        if not engine.over_http():
            raise LookupError("this OpenCode is driven through opencode-dispatch")
        try:
            directory = str(dispatches.existing_dir(directory))
        except dispatches.MissingDirectory as error:
            log.warning("dispatch directory not found: %r -> %s", error.raw, error.expanded)
            raise ValueError(self.t("missing_dir", raw=error.raw, expanded=error.expanded)) from error
        db = self.db()
        handoff_id = opencode_id("ses", descending=True)
        ledger.add_delegation(db, id=handoff_id, title=title, directory=directory, source_key=None, dispatch_seq=None,
                              dispatched_at=ledger.now(), brief=brief, context=[], engine="opencode", model=model)
        try:
            engine.open_session(self.delegation(handoff_id), f"{self.settings['prefix']}{title}")
        except Exception as error:  # nothing exists in OpenCode yet: the caller falls back to opencode-dispatch
            db.execute("delete from delegations where id = ?", (handoff_id,))
            ledger.log(db, None, "dispatch:failed", {"handoff": handoff_id, "title": title, "error": str(error)[:300]})
            raise LookupError(f"OpenCode did not open the session: {error}") from error
        prompt = instruction if not brief else f"{instruction}\n\n<file name=\"brief.md\">\n{brief}\n</file>"
        try:
            self.send(handoff_id, prompt, via="dispatch")
        except RuntimeError as error:  # observing, a failed send is not sent again: say so on a card with a retry
            turn = ledger.last_turn(db, handoff_id)
            self.fail_card(handoff_id, self.t("docket_fault", error=str(error)[:200]),
                           self.redispatch if turn else None, (handoff_id, turn["id"]) if turn else (), str(error))
        threading.Thread(target=self.attach_context, args=(handoff_id, title, instruction, brief), daemon=True).start()
        log.info("dispatched %s (%s) over HTTP", title, handoff_id)
        return handoff_id

    def attach_context(self, handoff_id: str, title: str, instruction: str, brief: str):
        """Find the dispatch in the watched chat and let the judge pick the background."""
        dispatch = None
        for _ in range(6):
            dispatch, _unavailable = self.find_dispatch(title, ledger.now())
            if dispatch:
                break
            time.sleep(5)
        if not dispatch:
            return
        try:
            context = registry.build_context({**dispatch, "instruction": instruction or dispatch["instruction"], "brief": brief or dispatch["brief"]}, title, self.settings)
        except Exception as error:
            log.warning("judge failed for %s: %s", handoff_id, error)
            context = []
        self.db().execute(
            "update delegations set source_key = ?, dispatch_seq = ?, dispatched_at = ?, context = ? where id = ?",
            (dispatch["session_key"], dispatch["seq"], dispatch["time"], json.dumps(context, ensure_ascii=False), handoff_id),
        )

    def launch(self, handoff_id: str, prompt: str, first: bool):
        row = self.delegation(handoff_id)
        run = self.engine(row).launch(row, prompt, first)
        self.processes = getattr(self, "processes", {})
        self.processes[run["pid"]] = run["process"]
        self.db().execute("insert into runs (delegation_id, pid, out, started_at) values (?, ?, ?, ?)", (handoff_id, run["pid"], run["out"], ledger.now()))
        ledger.set_state(self.db(), handoff_id, "running")

    def sweep(self):
        """Missed events must not leave a handoff 'running' forever.

        Every minute each running OpenCode handoff is compared with the session's real state;
        a run that ended after the docket last touched the handoff is treated as a run end.
        Every HEARTBEAT the watchdog looks at each instruction in flight and each PM's transcript.
        """
        beat = 0
        while True:
            for name, check in (("watchdog", self.watchdog), ("pm orphan", self.sweep_pm_orphans)):
                try:
                    check()
                except Exception:
                    log.exception("%s sweep failed", name)
            if beat % max(1, 60 // HEARTBEAT) == 0:
                self.sweep_minute()
            if beat % max(1, 600 // HEARTBEAT) == 0:
                try:
                    self.write_health()
                except Exception:
                    log.exception("health summary failed")
            beat += 1
            time.sleep(HEARTBEAT)

    def sweep_minute(self):
        for name, check in (("orphan", self.sweep_orphans), ("stale", self.sweep_stale), ("limit", self.sweep_limit_expiry),
                            ("running", self.sweep_running)):
            try:
                check()
            except Exception:
                log.exception("%s sweep failed", name)

    def sweep_running(self):
        for row in self.db().execute("select * from delegations where state = 'running' and engine = 'opencode'").fetchall():
            row = dict(row)
            turn = ledger.open_turn(self.db(), row["id"])
            if turn and not turn["confirmed_at"]:
                continue  # not yet seen in the session: the watchdog's to judge, never a run end
            try:
                session = Service().session(engines.session_of(row))
            except Exception as error:
                if "404" in str(error) or "NotFound" in str(error):
                    log.info("sweep: session %s is gone", engines.session_of(row))
                    # A technical fault never closes a handoff: the user closes or takes over.
                    self.fail_card(row["id"], self.t("session_gone"), error="session not found")
                continue
            times = session.get("time") or {}
            busy = (times.get("busy") or 0) > (times.get("idle") or 0)
            idle = times.get("idle") or 0
            if busy or not idle or idle < row["updated_at"] - 60_000 or (turn and idle < turn["started_at"]):
                continue
            log.info("sweep: %s ended without an event (%s)", row["id"], session.get("outcome"))
            ledger.log(self.db(), row["id"], "sweep", {"outcome": session.get("outcome")})
            self.on_run_end(row["id"], session.get("outcome") or "succeeded", session=engines.session_of(row))

    ORPHAN_AFTER = 2 * 3600_000  # ms a card may sit unanswered where the panel cannot show it
    def sweep_orphans(self):
        """A card nobody can see must not hold the queue forever.

        The panel shows a card only while the handoff is awaiting. An open card older than
        ORPHAN_AFTER on a handoff that is running or pending, with no PM job of its own in
        flight, is an orphan: it is superseded and the next queued card goes up.
        """
        db = self.db()
        cutoff = ledger.now() - self.ORPHAN_AFTER
        rows = db.execute(
            "select distinct d.id from cards c join delegations d on d.id = c.delegation_id"
            " where c.resolved_at is null and c.queued = 0 and c.created_at < ? and c.kind not in ('limit', 'closed')"
            " and d.state in ('running', 'pending')",
            (cutoff,),
        ).fetchall()
        for (delegation_id,) in rows:
            if self.busy(delegation_id):
                continue
            with self.lock(delegation_id):
                stale = db.execute(
                    "select id from cards where delegation_id = ? and resolved_at is null and queued = 0 and created_at < ? and kind not in ('limit', 'closed')",
                    (delegation_id, cutoff),
                ).fetchall()
                for card in stale:
                    ledger.resolve_card(db, card["id"], {"action": "superseded", "note": "orphan"})
                ledger.log(db, delegation_id, "sweep:orphan", {"cards": [c["id"] for c in stale]})
                log.info("sweep: superseded orphan cards %s on %s", [c["id"] for c in stale], delegation_id)
                if ledger.open_card(db, delegation_id):
                    ledger.set_state(db, delegation_id, "awaiting")
                else:
                    self.advance(delegation_id)

    # ---------- one writer to OpenCode, and a watchdog on every instruction ----------

    def delegation_for_session(self, session_id: str) -> dict | None:
        """The handoff an OpenCode session belongs to: its own id, or the session the watchdog moved it to."""
        db = self.db()
        row = ledger.get(db, session_id) or db.execute(
            "select * from delegations where engine_session = ? order by updated_at desc limit 1", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def track(self, delegation_id: str, session: str, message: str, via: str, started: int | None = None,
              attempt: int = 1, parent: int | None = None, run: dict | None = None) -> int:
        """Start following an instruction handed to OpenCode: dispatching until the watchdog sees it.
        Over HTTP the run names the message id Docket gave it (`run["message"]`)."""
        db = self.db()
        for old in db.execute("select id from turns where delegation_id = ? and ended_at is null", (delegation_id,)).fetchall():
            ledger.end_turn(db, old["id"], "superseded")
        run = run or {}
        turn = db.execute(
            "insert into turns (delegation_id, session, message, via, attempt, parent, started_at, pid, out, message_id, transport)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (delegation_id, session, message, via, attempt, parent, started or ledger.now(), run.get("pid"), run.get("out"),
             run.get("message"), run.get("transport")),
        ).lastrowid
        if run.get("process"):
            self.turn_processes = getattr(self, "turn_processes", {})
            self.turn_processes[turn] = run["process"]
        ledger.log(db, delegation_id, "turn:sent", {"turn": turn, "session": session, "via": via, "attempt": attempt, "pid": run.get("pid"),
                                                    **({"message_id": run["message"], "transport": run.get("transport")} if run.get("message") else {})})
        ledger.set_phase(db, delegation_id, "dispatching", turn=turn)
        return turn

    def send(self, delegation_id: str, message: str, model: str | None = None, via: str = "relay", text: str | None = None,
             attempt: int = 1, parent: int | None = None, new_session: bool = False, delivery: str | None = None) -> int | None:
        """The one way an instruction reaches the coding agent (relays, fallbacks, resends, retries).

        `message` is the instruction as the PM wrote it; `text` what is actually sent, when the
        watchdog adds a note to a resend. For OpenCode the turn is followed by the watchdog: the
        process starting is not the work starting. A send that fails at once stays
        awaiting_dispatch and is resent after DISPATCH_GRACE like any turn that never showed up.
        """
        db = self.db()
        row = self.delegation(delegation_id)
        engine = self.engine(row)
        if isinstance(engine, engines.Cli):
            self.launch(delegation_id, text or message, first=False)
            return None
        ledger.set_phase(db, delegation_id, "awaiting_dispatch", via=via, attempt=attempt)
        session, run, error = engines.session_of(row), None, None
        # Docket's own id and mark on the message: a retry never sends it twice, its delivery is
        # confirmed by that id, and any other instruction in the session is someone else's.
        message_id = opencode_id("msg")
        metadata = {"docket": {"handoff": delegation_id, "via": via, "attempt": attempt}}
        try:
            if new_session:
                fresh, run = engine.start_session(row, self.continuation_title(row), text or message, model,
                                                  message_id=message_id, metadata=metadata)
                if not fresh:
                    raise RuntimeError("the new OpenCode session did not appear")
                db.execute("update delegations set engine_session = ? where id = ?", (fresh, delegation_id))
                ledger.log(db, delegation_id, "session:replaced", {"from": session, "to": fresh})
                session = fresh
            else:
                run = engine.resume(row, text or message, model, message_id=message_id, metadata=metadata, delivery=delivery)
        except Exception as failure:
            error = str(failure)[:300]
        run = run if isinstance(run, dict) else {}
        if error:
            # Recorded as a turn all the same, so the watchdog resends it like one that never showed up.
            turn = db.execute(
                "insert into turns (delegation_id, session, message, via, attempt, parent, started_at, pid, out) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (delegation_id, session, message, via, attempt, parent, ledger.now(), run.get("pid"), run.get("out")),
            ).lastrowid
            for old in db.execute("select id from turns where delegation_id = ? and ended_at is null and id != ?", (delegation_id, turn)).fetchall():
                ledger.end_turn(db, old["id"], "superseded")
            ledger.log(db, delegation_id, "turn:send-failed", {"turn": turn, "error": error, "attempt": attempt})
            log.warning("send to %s failed: %s", delegation_id, error)
            if not self.acting:
                # Nobody sends it again while the watchdog only observes: the failure shows on a card at once.
                ledger.end_turn(db, turn, "send_failed")
                raise RuntimeError(error)
        else:
            turn = self.track(delegation_id, session, message, via, attempt=attempt, parent=parent, run=run)
        ledger.set_state(db, delegation_id, "running")
        return turn

    def continuation_title(self, row: dict) -> str:
        """A new session for the same handoff: the old title (inbox prefix and all), marked as a continuation."""
        try:
            title = Service().session(engines.session_of(row)).get("title") or row["title"]
        except Exception:
            title = row["title"]
        base = title.split(" ⟳", 1)[0]
        count = self.db().execute("select count(*) from events where delegation_id = ? and kind = 'session:replaced'", (row["id"],)).fetchone()[0]
        return f"{base} ⟳{count + 1}"

    def watchdog(self):
        """Every HEARTBEAT: is each instruction handed to OpenCode really being worked on?

          not seen within DISPATCH_GRACE (no user message of this turn in the session, or never busy)
              -> sent again once; then stalled, with a card
          seen, then QUIET_AFTER without a new message or a changed file in the project
              -> stopped and sent again once, in a new session that is told what the last one left
          ended interrupted, killed or failed -> the same (see on_run_end)
        A handoff with a PM job in flight is left for the next look.
        """
        db = self.db()
        for turn_id, process in list(getattr(self, "turn_processes", {}).items()):
            if process.poll() is not None:  # reaped: an `opencode run` that exited leaves no zombie behind
                self.turn_processes.pop(turn_id, None)
        rows = db.execute(
            "select d.* from delegations d where d.engine = 'opencode' and d.state not in ('closed', 'taken_over')"
            " and exists (select 1 from turns t where t.delegation_id = d.id and t.ended_at is null)"
        ).fetchall()
        for row in rows:
            row = dict(row)
            if self.busy(row["id"]):
                continue
            turn = ledger.open_turn(db, row["id"])
            if not turn:
                continue
            try:
                self.check_turn(row, turn)
            except Exception:
                log.exception("watchdog failed on %s", row["id"])

    def check_turn(self, row: dict, turn):
        db, now = self.db(), ledger.now()
        if not turn["confirmed_at"]:
            seen = self.confirm_turn(row, turn)
            if seen == "queued":  # handed over and waiting in OpenCode's inbox for the current work to end
                return
            if seen or now - turn["started_at"] < DISPATCH_GRACE:
                return
            if seen is None and not self.unreachable_too_long(row["id"]):
                return  # cannot read OpenCode at all: never resend blind until that has lasted a grace period
            outcome = self.ended_unseen(turn)
            if outcome:
                # It ran and finished before the docket saw it start (registered late, or its message has
                # scrolled out of the newest ones): a run end, for the PM to verify, never a resend.
                db.execute("update turns set confirmed_at = ? where id = ? and confirmed_at is null", (now, turn["id"]))
                ledger.log(db, row["id"], "watchdog:ended-unseen", {"turn": turn["id"], "outcome": outcome})
                return self.on_run_end(row["id"], outcome, session=turn["session"])
            if not self.acting:
                return self.observe(row["id"], turn, "send it again", "not started", after_ms=now - turn["started_at"])
            if not ledger.end_turn(db, turn["id"], "not_started"):
                return
            ledger.log(db, row["id"], "watchdog:not-started", {"turn": turn["id"], "attempt": turn["attempt"], "after_ms": now - turn["started_at"]})
            ledger.set_phase(db, row["id"], "awaiting_dispatch", why="not started")
            ledger.set_state(db, row["id"], "pending")
            self.submit(row["id"], self.recover, row["id"], turn["id"], self.t("why_not_started"), None)
            return
        if row["state"] != "running":
            # A card or the PM has it (a permission request, a question): OpenCode is not expected to move.
            db.execute("update turns set last_activity = ? where id = ?", (now, turn["id"]))
            return
        ended = self.ended_unseen(turn)
        if ended:
            # It ran and finished, and its end event never reached the docket (missed while it was
            # down, or registered after the run): a run end, never a quiet turn sent again (audit W2).
            ledger.log(db, row["id"], "watchdog:ended-unseen", {"turn": turn["id"], "outcome": ended})
            return self.on_run_end(row["id"], ended, session=turn["session"])
        last = turn["last_activity"] or turn["confirmed_at"]
        # Once a quiet turn has been noted in observe mode, its project is not walked every heartbeat.
        quiet_noted = not self.acting and self.was_observed(turn["id"], "quiet")
        activity = self.turn_activity(row, turn, look_at_files=not quiet_noted and now - last >= QUIET_AFTER // 2)
        if activity and activity > last:
            db.execute("update turns set last_activity = ? where id = ?", (activity, turn["id"]))
            return
        if now - last < QUIET_AFTER:
            return
        if not self.acting:
            return self.observe(row["id"], turn, "stop it and send it again in a new session", "quiet", quiet_ms=now - last)
        if not ledger.end_turn(db, turn["id"], "quiet"):
            return
        ledger.log(db, row["id"], "watchdog:quiet", {"turn": turn["id"], "attempt": turn["attempt"], "quiet_ms": now - last})
        ledger.set_phase(db, row["id"], "awaiting_dispatch", why="quiet")
        ledger.set_state(db, row["id"], "pending")
        self.submit(row["id"], self.recover, row["id"], turn["id"], self.t("why_quiet", minutes=QUIET_AFTER // 60_000), True)

    def ended_unseen(self, turn) -> str | None:
        """The session's outcome when it went busy after the turn was handed over and is idle again, else None."""
        try:
            session = Service().session(turn["session"])
        except Exception:
            return None
        times = session.get("time") or {}
        busy, idle = times.get("busy") or 0, times.get("idle") or 0
        outcome = session.get("outcome")
        if outcome in TERMINAL_OUTCOMES and idle > busy >= turn["started_at"] - 2000:
            return outcome
        return None

    def unreachable_too_long(self, delegation_id: str) -> bool:
        """OpenCode could not be read for a whole grace period: count that as not started too."""
        key = f"unreachable:{delegation_id}"
        since = self.db().execute("select value from kv where key = ?", (key,)).fetchone()
        if not since:
            self.db().execute("insert or replace into kv values (?, ?)", (key, str(ledger.now())))
            return False
        return ledger.now() - int(since[0]) >= DISPATCH_GRACE

    @staticmethod
    def message_times(message: dict) -> int:
        times = message.get("time") or {}
        return max([int(v) for v in times.values() if isinstance(v, (int, float))] or [0])

    def confirm_turn(self, row: dict, turn) -> bool | str | None:
        """Is the turn in the session? Over HTTP: its message, by the id Docket gave it, is there
        ("queued" while it still waits in the session's inbox behind other work). Through the CLI:
        a user message from when it was sent, and the session busy since (or already done with it).
        None when OpenCode cannot be read."""
        if turn["message_id"] and turn["transport"] == "http":
            return self.confirm_by_id(row, turn)
        try:
            service = Service()
            session = service.session(turn["session"])
            messages = service.messages(turn["session"], limit=10)
        except Exception as error:
            if "404" in str(error) or "NotFound" in str(error):
                return False
            log.debug("watchdog: cannot read %s: %s", turn["session"], error)
            return None
        self.db().execute("delete from kv where key = ?", (f"unreachable:{row['id']}",))
        since = turn["started_at"] - 2000
        asked = any(m.get("type") == "user" and ((m.get("time") or {}).get("created") or 0) >= since for m in messages or [])
        times = session.get("time") or {}
        busy, idle = times.get("busy") or 0, times.get("idle") or 0
        if not (asked and (busy > idle or busy >= since)):
            return False
        self.mark_running(row["id"], turn)
        return True

    def mark_running(self, delegation_id: str, turn, how: str | None = None):
        db = self.db()
        if db.execute("update turns set confirmed_at = ?, last_activity = ? where id = ? and confirmed_at is null", (ledger.now(), ledger.now(), turn["id"])).rowcount:
            ledger.log(db, delegation_id, "turn:running", {"turn": turn["id"], "attempt": turn["attempt"], "via": turn["via"],
                                                          "after_ms": ledger.now() - turn["started_at"], **({"by": how} if how else {})})
            if self.delegation(delegation_id)["state"] == "running":
                ledger.set_phase(db, delegation_id, "running", turn=turn["id"])

    def confirm_by_id(self, row: dict, turn) -> bool | str | None:
        try:
            service = Service()
            if service.message(turn["session"], turn["message_id"]):
                self.mark_running(row["id"], turn, "message id")
                return True
            queued = any(item.get("id") == turn["message_id"] for item in service.inbox(turn["session"]))
        except Exception as error:
            if "404" in str(error) or "NotFound" in str(error):
                return False
            log.debug("watchdog: cannot read %s: %s", turn["session"], error)
            return None
        self.db().execute("delete from kv where key = ?", (f"unreachable:{row['id']}",))
        return "queued" if queued else False

    def turn_activity(self, row: dict, turn, look_at_files: bool) -> int:
        """The newest sign of work: a message in the session, or (when it has been quiet a while) a file in the project."""
        newest = 0
        try:
            service = Service()
            newest = max([self.message_times(m) for m in service.messages(turn["session"], limit=5) or []] or [0])
            times = service.session(turn["session"]).get("time") or {}
            newest = max(newest, int(times.get("updated") or 0))
        except Exception as error:
            log.debug("watchdog: cannot read %s: %s", turn["session"], error)
        if look_at_files:
            project = self.project_dir(row)
            if project:
                newest = max(newest, project_files(project, 0)[0])
        return newest

    def stop_turn(self, turn):
        """Stop the run a turn started, if it still goes: its process, and the session if it is still busy."""
        process = getattr(self, "turn_processes", {}).pop(turn["id"], None)
        pid = turn["pid"]
        if pid and (process.poll() is None if process else engines.alive(pid)):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            ledger.log(self.db(), turn["delegation_id"], "turn:killed", {"turn": turn["id"], "pid": pid})
        try:
            service = Service()
            times = service.session(turn["session"]).get("time") or {}
            if (times.get("busy") or 0) > (times.get("idle") or 0):  # stuck at work: an idle session is left as it is
                # Remembered first: the interrupted event it causes is Docket's own, not a run end (audit W4).
                self.db().execute("insert or replace into kv values (?, ?)", (f"own_interrupt:{turn['session']}", str(ledger.now())))
                # OpenCode 2.2 stops a run with /interrupt; /abort does not exist, so stopping never worked before.
                service.interrupt(turn["session"])
                ledger.log(self.db(), turn["delegation_id"], "turn:aborted", {"turn": turn["id"], "session": turn["session"]})
        except Exception as error:
            log.debug("abort %s: %s", turn["session"], error)

    def session_dead(self, session: str) -> bool:
        """An OpenCode session that takes no more instructions: interrupted, or gone."""
        try:
            return Service().session(session).get("outcome") == "interrupted"
        except Exception as error:
            return "404" in str(error) or "NotFound" in str(error)

    def files_left(self, row: dict, turn) -> str:
        project = self.project_dir(row)
        changed = project_files(project, turn["started_at"])[1] if project else []
        if not changed:
            return self.t("no_files")
        return "\n".join(f"- {name}" for name in changed[:30]) + ("\n- …" if len(changed) > 30 else "")

    def resend_text(self, row: dict, turn, why: str, new_session: bool) -> str:
        if new_session:
            return self.t("resend_new", session=turn["session"], why=why, files=self.files_left(row, turn), message=turn["message"])
        return self.t("resend_same", why=why, message=turn["message"])

    def unfinished(self, delegation_id: str, turn_id: int, event: str, reply: str | None):
        """A run ended interrupted, killed or failed: not a delivery. A usage limit goes to the
        fallback model as before; anything else is sent again once (see recover)."""
        row = self.delegation(delegation_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        if event == "failed":
            if reply is None:
                try:
                    reply = self.engine(row).last_reply(row)
                except Exception:
                    reply = ""
            if self.retry_with_fallback(delegation_id, reply):
                return
        self.recover(delegation_id, turn_id, self.t("why_unfinished", outcome=event), event != "failed")

    def recover(self, delegation_id: str, turn_id: int, why: str, new_session: bool | None):
        """The watchdog's one automatic resend of an instruction, or the stall that follows it.

        Same session when the instruction never got there; a new one when the session is
        interrupted or was stuck (it would not take more), told which files the last turn left.
        """
        db = self.db()
        row = self.delegation(delegation_id)
        turn = db.execute("select * from turns where id = ?", (turn_id,)).fetchone()
        if not row or not turn or row["state"] in ("closed", "taken_over"):
            return
        self.stop_turn(turn)
        if turn["attempt"] >= 2:
            return self.stall(delegation_id, turn_id, why)
        if new_session is None:
            new_session = self.session_dead(turn["session"])
        ledger.log(db, delegation_id, "watchdog:resend", {"turn": turn_id, "why": why, "new_session": bool(new_session)})
        log.info("watchdog: %s: %s; sending again%s", delegation_id, why, " in a new session" if new_session else "")
        self.send(delegation_id, turn["message"], via="resend", text=self.resend_text(row, turn, why, new_session),
                  attempt=turn["attempt"] + 1, parent=turn_id, new_session=bool(new_session))

    def stall(self, delegation_id: str, turn_id: int, why: str):
        db = self.db()
        ledger.set_phase(db, delegation_id, "stalled", turn=turn_id, why=why)
        ledger.log(db, delegation_id, "watchdog:stalled", {"turn": turn_id, "why": why})
        log.warning("watchdog: %s stalled: %s", delegation_id, why)
        self.fail_card(delegation_id, self.t("stalled", why=why), self.redispatch, (delegation_id, turn_id),
                       error=why, extra={"stalled": True}, question=self.t("stalled_question"))

    def redispatch(self, delegation_id: str, turn_id: int):
        """The user pressed retry on a stalled card: the same instruction once more, with a fresh resend behind it."""
        db = self.db()
        row = self.delegation(delegation_id)
        turn = db.execute("select * from turns where id = ?", (turn_id,)).fetchone()
        if not row or not turn or row["state"] in ("closed", "taken_over"):
            return
        latest = ledger.last_turn(db, delegation_id)
        new_session = self.session_dead(engines.session_of(row))
        why = {"not_started": self.t("why_not_started"), "quiet": self.t("why_quiet", minutes=QUIET_AFTER // 60_000)}.get(
            latest["outcome"], self.t("why_unfinished", outcome=latest["outcome"] or "?"))
        ledger.log(db, delegation_id, "watchdog:retry", {"turn": turn_id, "new_session": new_session})
        self.send(delegation_id, turn["message"], via="retry", text=self.resend_text(row, latest, why, new_session), parent=turn_id, new_session=new_session)

    # ---------- PM turns nobody carried out ----------

    def pm_messages(self, conn, row: dict) -> list:
        session_id = transcript.session_id_for(conn, row["pm_key"])
        return transcript_messages(conn, session_id) if session_id else []

    def wake_windows(self, delegation_id: str) -> list[tuple[int, int]]:
        return [
            (d["start"], d["end"]) for d in (
                json.loads(r[0]) for r in self.db().execute("select detail from events where delegation_id = ? and kind = 'pm:wake'", (delegation_id,))
            )
        ]

    def pm_orphans(self, conn, row: dict) -> tuple[list[dict], int | None]:
        """Turns in the PM's transcript the docket did not start, since its cursor: ([orphan], new cursor).

        A message inside a docket wake window is the docket's own. Anything else (a sessions_send
        from the main chat, someone typing into the session) is grouped with the PM's replies to
        it; a group is settled once a reply holds a valid action, or it has been quiet ORPHAN_SETTLE.
        The first look at a PM never goes back: the cursor starts at its latest message.
        """
        key = f"pm_cursor:{row['id']}"
        stored = self.db().execute("select value from kv where key = ?", (key,)).fetchone()
        messages = self.pm_messages(conn, row)
        latest = max((m.seq for m in messages), default=0)
        if stored is None:
            return [], latest
        cursor = int(stored[0])
        windows = self.wake_windows(row["id"])
        ours = lambda m: any(start - 1000 <= m.time <= end + 3000 for start, end in windows)  # noqa: E731
        groups, current, mark = [], None, cursor
        for m in messages:
            if m.seq <= cursor:
                continue
            if ours(m):
                current = None
                continue
            if m.role == "user":
                current = {"user": m, "replies": []}
                groups.append(current)
            elif m.text.strip():
                if current is None:
                    current = {"user": None, "replies": []}
                    groups.append(current)
                current["replies"].append(m)
        found = []
        for group in groups:
            action = None
            for reply in reversed(group["replies"]):
                value, errors = pm.pick(reply.text, pm.validate_action)
                if not errors:
                    action = pm.normalize(value)  # the same shape a docket wake's answer gets: no control fields
                    break
            seqs = [m.seq for m in ([group["user"]] if group["user"] else []) + group["replies"]]
            last_time = max(m.time for m in ([group["user"]] if group["user"] else []) + group["replies"])
            if action is None and ledger.now() - last_time < self.ORPHAN_SETTLE:
                break  # the PM may still be answering it: look again next time, from here
            user = group["user"]
            source, text = external_source(user.text) if user else ("outside Docket", "")
            found.append({"seq": max(seqs), "time": last_time, "source": source, "text": text, "action": action,
                          "reply": group["replies"][-1].text[:2000] if group["replies"] else ""})
            mark = max(seqs)
        # Past our own messages too, up to the first group still open.
        open_from = next((min(m.seq for m in ([g["user"]] if g["user"] else []) + g["replies"]) for g in groups[len(found):]), None)
        ceiling = (open_from - 1) if open_from is not None else latest
        mark = max(mark, ceiling)
        return found, mark

    ORPHAN_SETTLE = 80_000  # ms a PM reply without an action is given to finish before it counts as an answer

    def set_pm_cursor(self, delegation_id: str, seq: int | None):
        if seq is not None:
            self.db().execute("insert or replace into kv values (?, ?)", (f"pm_cursor:{delegation_id}", str(seq)))

    def fold_orphans(self, row: dict) -> list[dict]:
        """Before a docket wake: whatever reached the PM from elsewhere since the last one, for the PM to take into account."""
        try:
            conn = openclaw_connect()
        except Exception:
            return []
        try:
            found, mark = self.pm_orphans(conn, row)
        except Exception as error:
            log.debug("orphan fold failed for %s: %s", row["id"], error)
            return []
        finally:
            conn.close()
        self.set_pm_cursor(row["id"], mark)
        if not found:
            return []
        ledger.log(self.db(), row["id"], "orphan:folded", {"seqs": [f["seq"] for f in found], "sources": [f["source"] for f in found]})
        return [{"time": self.clock(f["time"]), "source": f["source"], "text": f["text"] or "(none)",
                 "action": json.dumps(f["action"], ensure_ascii=False) if f["action"] else (f["reply"][:300] or "(no reply)")} for f in found]

    def sweep_pm_orphans(self):
        """A PM answered someone other than the docket, and nothing carried it out (today's 13:39 case).

        Every HEARTBEAT, each live handoff's PM transcript (and those of handoffs closed in the
        last ORPHAN_WINDOW) is read past its cursor. A settled orphan's action is carried out the
        docket's way when that is safe; otherwise, or when the handoff had already ended, it is
        stalled on a card that says so, with a button to carry it out.
        """
        db = self.db()
        rows = db.execute(
            "select * from delegations where state != 'taken_over' and (state != 'closed' or updated_at >= ?)", (ledger.now() - ORPHAN_WINDOW,)
        ).fetchall()
        if not rows:
            return
        try:
            conn = openclaw_connect()
        except Exception:
            return
        try:
            for row in rows:
                row = dict(row)
                if self.busy(row["id"]):
                    continue
                try:
                    found, mark = self.pm_orphans(conn, row)
                except Exception as error:
                    log.debug("orphan scan failed for %s: %s", row["id"], error)
                    continue
                job = None
                with self.lock(row["id"]):
                    if self.busy(row["id"]):
                        continue  # a docket wake started meanwhile: it folds these in itself
                    self.set_pm_cursor(row["id"], mark)
                    if found:
                        job = self.rescue(row, found)
                if job:  # submitted outside the lock: the job takes it itself
                    self.submit(row["id"], *job)
        finally:
            conn.close()

    def rescue(self, row: dict, found: list[dict]):
        """Under the lock: log the orphans, put up a card or return the job that carries the action out."""
        db = self.db()
        for orphan in found:
            ledger.log(db, row["id"], "orphan:found", {"seq": orphan["seq"], "source": orphan["source"], "text": orphan["text"][:300],
                                                       "action": (orphan["action"] or {}).get("action")})
        latest = next((f for f in reversed(found) if f["action"]), None)
        state = self.delegation(row["id"])["state"]
        if not latest:
            # Asked from outside, answered without an action: the docket asks the PM again, its own way.
            text = found[-1]["text"]
            if text and state not in ("closed", "limit", "taken_over") and self.acting:
                ledger.log(db, row["id"], "orphan:rewake", {"seq": found[-1]["seq"]})
                ledger.set_state(db, row["id"], "pending")
                return (self.act, row["id"], pm.note_wake(text, found[-1]["source"], row["rounds"]), None, "orphan", None, state)
            ledger.log(db, row["id"], "orphan:ignored", {"seq": found[-1]["seq"], "state": state, **({} if self.acting else {"observing": True})})
            return None
        action = latest["action"]
        if state in ("closed", "limit"):
            # Nothing may go out for a handoff that has ended or spent its budget: dropped, never asked about.
            ledger.log(db, row["id"], "orphan:dropped", {"seq": latest["seq"], "state": state, "action": action["action"]})
            return None
        # The words came in through the main agent, and the PM answered them: carry the answer out like
        # any other. Never a card asking the person whether they meant it (2026-11-27): they asked the
        # main agent to do it, and the user has no attention to spare for confirming it. The risk checks
        # in carry_out still hold, and a busy session takes the instruction at its next step.
        ledger.log(db, row["id"], "orphan:auto", {"seq": latest["seq"], "action": action["action"]})
        ledger.set_state(db, row["id"], "pending")
        return (self.rescue_orphan, row["id"], json.dumps(action, ensure_ascii=False))

    def rescue_orphan(self, delegation_id: str, action_json: str):
        """Carry out a PM's answer that reached nobody, as if the docket had woken it."""
        db = self.db()
        row = self.delegation(delegation_id)
        if not row or row["state"] == "taken_over":
            return
        action = json.loads(action_json)
        if action["action"] in ("relay", "close"):
            # The PM moved on: a card of its own still up is answered by this action.
            for card in db.execute("select id, body from cards where delegation_id = ? and resolved_at is null and kind not in ('limit', 'closed')", (delegation_id,)).fetchall():
                if not json.loads(card["body"]).get("pending_permission"):
                    ledger.resolve_card(db, card["id"], {"action": "superseded", "note": "orphan"})
        ledger.log(db, delegation_id, "orphan:executed", {"action": action["action"]})
        self.carry_out(delegation_id, action, via="orphan")

    # ---------- the user, the main chat or another session passes a word on ----------

    def find(self, handoff: str) -> dict:
        """A handoff by id, or by a unique prefix of it with or without ses_ (as docket status prints them)."""
        row = self.delegation(handoff)
        if row:
            return row
        bare = handoff.removeprefix("ses_")
        rows = self.db().execute("select id from delegations where id like ? or id like ? order by updated_at desc", (f"{handoff}%", f"ses_{bare}%")).fetchall()
        if len(rows) != 1:
            raise ValueError(self.t("err_missing"))
        return self.delegation(rows[0]["id"])

    def note(self, handoff: str, text: str, source: str = "docket note") -> dict:
        """docket note / the panel's "pass a word on": the one way in for anyone but the docket.

        The text is logged, then the PM is woken the docket's way and its action carried out.
        A card of the PM's own that is up is answered by it, and that answer is the round; passing
        words on with no card to answer costs none. A closed handoff opens again; a taken-over
        one is refused.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError(self.t("err_note_empty"))
        row = self.find(handoff)
        db = self.db()
        if row["state"] == "taken_over":
            raise ValueError(self.t("err_taken_over"))
        if row["state"] == "limit" or row["rounds"] >= ledger.ROUND_LIMIT:
            self.enter_limit(row["id"])
            raise ValueError(self.t("err_limit"))
        ledger.log(db, row["id"], "note", {"text": text[:2000], "source": source})
        card = ledger.open_card(db, row["id"])
        body = json.loads(card["body"]) if card else {}
        # A card is answered only if the PM says these words answer it (settles_card), and that is the
        # round; passing words on costs none (2026-11-27). The card stays up until then.
        rounds = row["rounds"]
        chat = self.claim_chat_message(source)
        was = row["state"]
        ledger.set_state(db, row["id"], "pending", force=True)  # a note opens a closed handoff again
        shown = body if card is not None and self.answerable(card) else None
        self.submit(row["id"], self.act, row["id"], pm.note_wake(text, source, rounds, shown), text, "note", None, was)
        return {"id": row["id"], "rounds": rounds}

    def claim_chat_message(self, source: str | None) -> str | None:
        """A note passed on from a chat takes that chat's latest user message, so the judge's reading of
        the same words is not acted on a second time (see take_mention). Returns that message as
        "<transcript session>:<seq>", or None (no chat named, nothing in it, or a chat that cannot be
        read: best effort)."""
        if not source or not source.startswith("agent:"):
            return None
        try:
            conn = openclaw_connect()
            try:
                session_id = transcript.session_id_for(conn, source)
                history = transcript_messages(conn, session_id, since=ledger.now() - 600_000) if session_id else []
            finally:
                conn.close()
        except Exception as error:
            log.debug("note: chat %s unreadable: %s", source, error)
            return None
        users = [m for m in history if m.role == "user"]
        if not users:
            return None
        chat = f"{session_id}:{users[-1].seq}"
        ledger.claim(self.db(), f"chat_claim:{chat}")
        return chat

    def paid_for(self, delegation_id: str, chat: str) -> bool:
        """Whether a round was already spent here on this chat message (by a note or by the judge's reading)."""
        rows = self.db().execute("select detail from events where delegation_id = ? and kind = 'chat:paid'", (delegation_id,)).fetchall()
        return any(json.loads(detail or "{}").get("chat") == chat for (detail,) in rows)

    # ---------- the daily health summary ----------

    def write_health(self, day: dt.date | None = None):
        """~/.openclaw/workspace/cron-logs/docket/<date>/health.md (and .json): how often work got stuck
        today, how often the docket got it moving again by itself, and how often a person had to step in."""
        days = [day] if day else [dt.date.today() - dt.timedelta(days=1), dt.date.today()]
        for when in days:
            start = int(dt.datetime.combine(when, dt.time()).timestamp() * 1000)
            report = health(self.db(), start, start + 86400_000)
            folder = HEALTH_DIR / f"{when:%Y-%m-%d}"
            if when != dt.date.today() and (folder / "health.json").exists():
                continue  # a past day is written once more after midnight, then left alone
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "health.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "health.md").write_text(health_text(when, report), encoding="utf-8")

    # ---------- cards nobody answers on the panel ----------

    def minutes_ms(self, name: str) -> int:
        value = self.settings.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            value = docket_config.DEFAULTS[name]  # 0 would close every idle card at once
        return int(float(value) * 60_000)

    def sweep_stale(self):
        """A card must not keep asking a question nobody is going to answer on the panel.

        On each awaiting handoff, with the coding agent idle and no PM job in flight, the card
        on show leaves the panel one of three ways, tried in this order:
          1. the user settled it in the main chat: the judge reads what they wrote after the card
             went up (relevance.card_status); its PM closes on the answer or relays it, or says
             why a card made moot is void;
          2. nobody answered for quiet_expire_minutes: the docket closes it itself, noted as expired;
          3. nobody answered for quiet_wake_minutes: its PM is woken once, to close it or ask more
             specifically. Silence never picks an option for the user.
        See card_clock for when the wait starts. Permission requests are left alone: OpenCode
        waits on them, and a run's end expires them.
        """
        if not self.settings.get("stale_enabled"):
            return
        db = self.db()
        if self.stale_since is None:  # the first sweep with the feature on: no clock starts earlier
            db.execute("insert or ignore into kv values ('stale_since', ?)", (str(ledger.now()),))
            self.stale_since = int(db.execute("select value from kv where key = 'stale_since'").fetchone()[0])
        rows = db.execute(
            "select distinct d.id from cards c join delegations d on d.id = c.delegation_id"
            " where c.resolved_at is null and c.queued = 0 and c.kind not in ('limit', 'closed') and d.state = 'awaiting'"
        ).fetchall()
        for (delegation_id,) in rows:
            if self.busy(delegation_id):
                continue
            try:
                self.check_stale(delegation_id)
            except Exception:
                log.exception("stale check failed for %s", delegation_id)

    def card_clock(self, card, body: dict) -> int:
        """When a card's wait started: when it went up on the panel (a re-asked card keeps the
        first one's), moved on by a chat message still weighing its question, never before stale_since."""
        start = int(body.get("quiet_since") or ledger.shown_at(card))
        moved = self.db().execute("select value from kv where key = ?", (f"quiet_extend:{card['id']}",)).fetchone()
        return max(start, int(moved[0]) if moved else 0, self.stale_since or 0)

    def check_stale(self, delegation_id: str):
        db = self.db()
        row = self.delegation(delegation_id)
        card = ledger.open_card(db, delegation_id)
        if not row or row["state"] != "awaiting" or not card or card["kind"] in ("limit", "closed"):
            return
        body = json.loads(card["body"])
        if body.get("pending_permission") or body.get("docket_fault"):
            return  # OpenCode waits on a permission; the docket's own fault waits for a retry: neither expires
        # A PM's own question or report can be settled in the chat or re-asked; a relay confirmation can only expire.
        askable = not body.get("pending_relay")
        waited = ledger.now() - self.card_clock(card, body)
        expire = waited >= self.minutes_ms("quiet_expire_minutes")
        wake = askable and waited >= self.minutes_ms("quiet_wake_minutes") and not body.get("quiet_woken") \
            and not db.execute("select 1 from kv where key = ?", (f"quiet_woken:{card['id']}",)).fetchone()
        chat = self.chat_after(row, card) if askable else None
        if not (chat or expire or wake):
            return
        if self.session_busy(row) is not False:
            return  # a run is going on, or its state cannot be read: the card is not outliving anything
        verdict = self.judge_card(row, card, body, chat) if chat else None
        if verdict and verdict["status"] == "discussing":
            return  # the user is still weighing it: its clock now starts at that message
        lock = self.lock(delegation_id)
        if not lock.acquire(blocking=False):
            return  # a PM job holds it; look again on the next sweep instead of waiting it out
        job = None
        try:
            current = ledger.open_card(db, delegation_id)
            if self.busy(delegation_id) or not current or current["id"] != card["id"] or ledger.get(db, delegation_id)["state"] != "awaiting":
                return  # answered, or a PM job started, while the judge was reading
            if verdict and verdict["status"] != "open":
                job = self.take_settled(delegation_id, card, verdict)
                self.mark_judged(card["id"], verdict["mark"])
            elif expire:
                self.quiet_expire(delegation_id, card["id"], waited)
            elif wake:
                db.execute("insert or replace into kv values (?, ?)", (f"quiet_woken:{card['id']}", str(ledger.now())))
                ledger.log(db, delegation_id, "stale:quiet-wake", {"card": card["id"], "after_ms": waited})
                job = (self.quiet_wake, (delegation_id, card["id"]))
        finally:
            lock.release()
        if job:  # submitted outside the lock: the job takes it itself
            self.submit(delegation_id, job[0], *job[1])

    def session_busy(self, row: dict) -> bool | None:
        """Is the coding agent at work on this handoff? None when its state cannot be read."""
        if isinstance(self.engine(row), engines.Cli):
            return bool(self.db().execute("select 1 from runs where delegation_id = ? and finished_at is null", (row["id"],)).fetchone())
        try:
            session = Service().session(engines.session_of(row))
        except Exception as error:
            if "404" in str(error) or "NotFound" in str(error):
                return False  # the session is gone: nothing runs any more
            log.info("stale: cannot read session %s: %s", row["id"], error)
            return None
        times = session.get("time") or {}
        return (times.get("busy") or 0) > (times.get("idle") or 0)

    def chat_after(self, row: dict, card, since: int | None = None) -> dict | None:
        """The main chat since the card went up (or `since`), if a user message in it is new to the judge.

        {"session": transcript session id, "messages": both speakers, oldest first}. None when
        there is nothing to judge: the judge or attribution is off, the handoff has no source
        chat, or the transcript cannot be read (the mention poll reports that).
        """
        if not (self.settings.get("attribute") and self.judging and row.get("source_key")):
            return None
        try:
            conn = openclaw_connect()
            try:
                session_id = transcript.session_id_for(conn, row["source_key"])
                history = transcript_messages(conn, session_id, since=since or ledger.shown_at(card)) if session_id else []
            finally:
                conn.close()
        except Exception as error:
            log.debug("stale: transcript unreadable: %s", error)
            return None
        users = [m for m in history if m.role == "user"]
        if not users:
            return None
        seen = self.db().execute("select value from kv where key = ?", (f"stale_seq:{card['id']}",)).fetchone()
        seen_session, _, seen_seq = (seen[0] if seen else "").rpartition(":")
        if seen_session == session_id and users[-1].seq <= int(seen_seq or 0):
            return None
        return {"session": session_id, "messages": history}

    def mark_judged(self, card_id: int, mark: str):
        """The chat up to `mark` ("session:seq") has been read against this card."""
        self.db().execute("insert or replace into kv values (?, ?)", (f"stale_seq:{card_id}", mark))

    @staticmethod
    def message_view(m) -> dict:
        return {"id": f"m{m.seq}", "role": m.role, "time": dt.datetime.fromtimestamp(m.time / 1000).strftime("%m-%d %H:%M"), "text": m.text}

    def judge_task(self, row: dict) -> dict:
        """A handoff as the judge sees it: its brief and the instructions OpenCode has been sent."""
        relayed = [e["detail"] for e in self.db().execute("select detail from events where delegation_id = ? and kind = 'relayed' order by id", (row["id"],))]
        return {"id": row["id"], "title": row["title"], "instruction": "", "brief": row["brief"] or "", "rounds": [json.loads(r)["message"] for r in relayed if r]}

    def judge_card(self, row: dict, card, body: dict, chat: dict) -> dict | None:
        """The judge's reading of a card against the chat after it; None when it could not answer.

        Open and discussing readings are marked read at once; answered and superseded ones only
        once the card has been acted on, so a reading that could not be acted on is taken again.
        """
        messages = chat["messages"]
        try:
            answer, _ = relevance.card_status(self.judge_task(row), body, [self.message_view(m) for m in messages], float(self.settings["stale_threshold"]))
        except Exception as error:  # the judge's failure is never the sweep's: try again on the next new message
            log.warning("stale: judge failed on card %s: %s", card["id"], error)
            return None
        db = self.db()
        mark = f"{chat['session']}:{[m for m in messages if m.role == 'user'][-1].seq}"
        ledger.log(db, row["id"], "stale:judged", {"card": card["id"], **{k: answer[k] for k in ("status", "probability", "message", "choices")}})
        said = next((m for m in messages if f"m{m.seq}" == answer["message"]), None)
        if answer["status"] == "open" or not said:
            self.mark_judged(card["id"], mark)
            return {**answer, "status": "open"}
        if answer["status"] == "discussing":
            db.execute("insert or replace into kv values (?, ?)", (f"quiet_extend:{card['id']}", str(said.time)))
            ledger.log(db, row["id"], "stale:discussing", {"card": card["id"], "seq": said.seq})
            self.mark_judged(card["id"], mark)
        quote = {"session": chat["session"], "seq": said.seq, "at": said.time, "time": self.message_view(said)["time"], "text": said.text[:500]}
        return {**answer, "quote": quote, "mark": mark}

    def take_settled(self, delegation_id: str, card, verdict: dict):
        """Under the lock: take the settled card off the panel; return the PM job that follows it."""
        db = self.db()
        quote = verdict["quote"]
        # One chat message is acted on once: the mention poll may have taken it for this or another handoff.
        if not ledger.claim(db, f"chat_claim:{quote['session']}:{quote['seq']}"):
            ledger.log(db, delegation_id, "stale:claimed", {"card": card["id"], "seq": quote["seq"]})
            return None
        answered = verdict["status"] == "answered"
        resolution = {
            "action": "answered-in-chat" if answered else "superseded",
            "by": "chat-match",
            "quote": quote["text"],
            "seq": quote["seq"],
            "said_at": quote["at"],
            "probability": round(verdict["probability"], 3),
        }
        if answered:
            # Answering in the chat is taking part: one round, spent with the card it answers, as a mention is.
            resolution.update(choices=verdict["choices"], reply=quote["text"], via="chat")
            if ledger.get(db, delegation_id)["rounds"] >= ledger.ROUND_LIMIT:
                ledger.log(db, delegation_id, "stale:blocked", {"card": card["id"], "seq": quote["seq"]})
                self.enter_limit(delegation_id)
                return None
            if ledger.spend_round(db, delegation_id, card["id"], resolution) is None:
                return None
        elif not ledger.resolve_open(db, card["id"], resolution):
            return None
        ledger.log(db, delegation_id, f"stale:{verdict['status']}", {"card": card["id"], "seq": quote["seq"], "choices": verdict["choices"], "probability": resolution["probability"]})
        log.info("stale: card %s on %s %s in the main chat (m%s)", card["id"], delegation_id, verdict["status"], quote["seq"])
        ledger.set_state(db, delegation_id, "pending")
        return self.settle_card, (delegation_id, card["id"], verdict["status"])

    def settle_card(self, delegation_id: str, card_id: int, mode: str):
        """The PM closes on a card the chat settled, or relays the answer; a moot card it calls void."""
        db = self.db()
        row = self.delegation(delegation_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        card = db.execute("select * from cards where id = ?", (card_id,)).fetchone()
        resolution = json.loads(card["resolution"] or "{}")
        quote = {"text": resolution.get("quote", ""), "time": dt.datetime.fromtimestamp((resolution.get("said_at") or 0) / 1000).strftime("%m-%d %H:%M")}
        message = pm.resolution_wake(mode, json.loads(card["body"]), quote, resolution.get("choices") or {}, row["rounds"])
        action = self.ask_pm(row, message, pm.validate_settle(mode))
        now = self.delegation(delegation_id)["state"]
        if now in ("closed", "taken_over"):
            ledger.log(db, delegation_id, "pm:dropped", {"action": action["action"], "state": now})
            return
        ledger.amend_resolution(db, card_id, {"basis": action.get("basis"), "settled": action["action"]})
        ledger.log(db, delegation_id, "stale:settled", {"card": card_id, "mode": mode, "action": action["action"], "basis": action.get("basis")})
        self.carry_out(delegation_id, action, via="chat")

    def quiet_wake(self, delegation_id: str, card_id: int):
        """Nobody answered for quiet_wake_minutes: the PM closes the card or asks more specifically, once.

        Not retryable: when the PM fails, the card simply stays up, and the hard cap still closes it.
        """
        db = self.db()
        row = self.delegation(delegation_id)
        card = db.execute("select * from cards where id = ?", (card_id,)).fetchone()
        if not row or row["state"] in ("closed", "taken_over") or not card or card["resolved_at"] is not None:
            return
        body = json.loads(card["body"])
        start = int(body.get("quiet_since") or ledger.shown_at(card))
        woken = db.execute("select value from kv where key = ?", (f"quiet_woken:{card_id}",)).fetchone()
        message = pm.quiet_wake(body, round((ledger.now() - start) / 3600_000, 1), self.minutes_ms("quiet_expire_minutes") / 3600_000, row["rounds"])
        try:
            action = self.ask_pm(row, message, pm.validate_settle("quiet"))
        except pm.PMFailed as error:
            log.warning("stale: quiet wake failed for %s: %s", delegation_id, error)
            ledger.log(db, delegation_id, "stale:pm-failed", {"card": card_id, "error": str(error)[:300]})
            self.executed(delegation_id, "fault", error=str(error)[:300], job="quiet_wake", note="the card stays up")
            return
        now = self.delegation(delegation_id)["state"]
        if now in ("closed", "taken_over"):
            ledger.log(db, delegation_id, "pm:dropped", {"action": action["action"], "state": now})
            return
        # The user may have taken the card up in the chat while the PM was thinking: their words win.
        chat = self.chat_after(row, card, since=int(woken[0]) if woken else None)
        verdict = self.judge_card(row, card, body, chat) if chat else None
        if verdict and verdict["status"] != "open":
            ledger.log(db, delegation_id, "stale:dropped", {"card": card_id, "action": action["action"], "chat": verdict["status"]})
            if verdict["status"] != "discussing":
                job = self.take_settled(delegation_id, card, verdict)
                self.mark_judged(card_id, verdict["mark"])
                if job:
                    self.submit(delegation_id, job[0], *job[1])
            return
        closing = action["action"] == "close"
        if not ledger.resolve_open(db, card_id, {"action": "expired" if closing else "superseded", "by": "quiet-wake"}):
            # The user answered on the panel while the PM was thinking: their answer stands, the PM's goes.
            ledger.log(db, delegation_id, "stale:dropped", {"card": card_id, "action": action["action"]})
            return
        ledger.log(db, delegation_id, "stale:quiet-settled", {"card": card_id, "action": action["action"]})
        if closing:
            self.finish(delegation_id, action["conclusion"], action.get("remaining"))
            self.executed(delegation_id, "close", by="quiet-wake")
        else:
            # The new question keeps the first one's clock and is not woken for again.
            shown = self.show_card(delegation_id, action["card"].get("kind", "question"), {**action["card"], "quiet_since": start, "quiet_woken": True})
            self.executed(delegation_id, "card", card=shown)
        self.check_limit(delegation_id)

    def sweep_limit_expiry(self):
        """The wrap-up card at the limit is not a question waiting forever: nothing more can run, and
        the handoff note is already written. Unanswered for quiet_expire_minutes, it closes the handoff
        with its own conclusion, so no handoff sits at the limit for days (2026-11-27)."""
        db = self.db()
        for row in db.execute("select * from delegations where state = 'limit'").fetchall():
            card = ledger.open_card(db, row["id"])
            if not card or card["kind"] != "limit":
                continue
            waited = ledger.now() - self.card_clock(card, json.loads(card["body"]))
            if waited < self.minutes_ms("quiet_expire_minutes"):
                continue
            note = json.loads(card["body"]).get("wrap_up") or {}
            ledger.log(db, row["id"], "limit:expired", {"card": card["id"], "after_ms": waited})
            log.info("limit card %s on %s unanswered for %d hours, closed", card["id"], row["id"], waited // 3600_000)
            self.finish(row["id"], note.get("conclusion") or self.t("closed_fallback"), note.get("remaining"),
                        extra={"auto_expired": {"by": "limit-expiry", "reason": "no reply", "after_ms": waited}})

    def quiet_expire(self, delegation_id: str, card_id: int, waited: int):
        """Under the lock: nobody answered for quiet_expire_minutes. The docket closes it and says so."""
        db = self.db()
        note = {"by": "quiet-expiry", "reason": "no reply", "after_ms": waited}
        if not ledger.resolve_open(db, card_id, {"action": "auto-expired", **note}):
            return
        ledger.log(db, delegation_id, "stale:quiet-expired", {"card": card_id, "after_ms": waited})
        log.info("stale: card %s on %s unanswered for %d hours, closed", card_id, delegation_id, waited // 3600_000)
        self.finish(delegation_id, self.t("quiet_expired", hours=round(waited / 3600_000)), extra={"auto_expired": note})

    def watch_runs(self):
        db = self.db()
        while True:
            try:
                for run in db.execute("select * from runs where finished_at is null").fetchall():
                    process = getattr(self, "processes", {}).get(run["pid"])
                    running = process.poll() is None if process else engines.alive(run["pid"])
                    if running:
                        continue
                    db.execute("update runs set finished_at = ? where id = ?", (ledger.now(), run["id"]))
                    row = self.delegation(run["delegation_id"])
                    if not row:
                        continue
                    result = self.engine(row).parse(row, Path(run["out"]))
                    if result.get("session") and not row.get("engine_session"):
                        db.execute("update delegations set engine_session = ? where id = ?", (result["session"], row["id"]))
                    self.on_run_end(row["id"], result["outcome"], result["reply"])
            except Exception:
                log.exception("run watch failed")
            time.sleep(3)

    # ---------- registration ----------

    def find_dispatch(self, title: str, created: int):
        """(dispatch or None, the Unavailable error if the transcript could not be read)."""
        try:
            return registry.find_dispatch(title, created, self.settings), None
        except transcript.Unavailable as error:
            return None, error

    def maybe_register(self, session_id: str, attempts: int = 6):
        """A session that appeared in OpenCode: a handoff opencode-dispatch started (the inbox prefix in
        its title and a matching dispatch in a watched chat) is registered, once. session.created and
        session.renamed both arrive for one dispatch, so a second look while the first is still
        searching the chat is dropped (audit 4A-7)."""
        with self.locks_guard:
            registering = self.__dict__.setdefault("registering", set())
            if session_id in registering:
                return
            registering.add(session_id)
        try:
            self.register(session_id, attempts)
        finally:
            with self.locks_guard:
                registering.discard(session_id)

    def register(self, session_id: str, attempts: int):
        db = self.db()
        if self.delegation_for_session(session_id):
            return  # known (Docket made it, or registered it already), or a session the watchdog opened to carry a handoff on
        try:
            session = Service().session(session_id)
        except Exception:
            log.warning("cannot read session %s", session_id)
            return
        title = session.get("title") or ""
        prefix = self.settings["prefix"]
        if not title.startswith(prefix.strip()):
            return
        name = dispatches.strip_prefix(title, prefix)
        created = (session.get("time") or {}).get("created") or ledger.now()
        dispatch = unavailable = None
        for attempt in range(attempts):  # the transcript write can lag the session by a moment
            dispatch, unavailable = self.find_dispatch(name, created)
            if dispatch:
                break
            if attempt + 1 < attempts:
                time.sleep(5)
        if not dispatch:
            if unavailable:
                log.warning("cannot check whether %s is a dispatch: %s", title, unavailable)
                ledger.log(db, None, "skipped", {"session": session_id, "title": title, "reason": "transcript unavailable", "error": str(unavailable)})
            else:
                log.info("not a watched dispatch, skipping: %s", title)
                ledger.log(db, None, "skipped", {"session": session_id, "title": title, "reason": "no matching dispatch in watched sessions"})
            return
        name = dispatch.get("title") or name  # the dispatch's own title, without a dispatcher's marker
        try:
            context = registry.build_context(dispatch, name, self.settings)
        except Exception as error:
            log.warning("judge failed for %s: %s", session_id, error)
            context = []
        raw = dispatch.get("directory_raw") or dispatch["directory"] or (session.get("location") or {}).get("directory")
        missing = None
        try:
            directory = str(dispatches.existing_dir(raw)) if raw else None
        except dispatches.MissingDirectory as error:
            directory, missing = None, error  # never store a path nothing can run in
        ledger.add_delegation(
            db,
            id=session_id,
            title=name,
            directory=directory,
            source_key=dispatch["session_key"],
            dispatch_seq=dispatch["seq"],
            dispatched_at=dispatch["time"],
            brief=dispatch["brief"],
            context=context,
        )
        if missing:
            log.warning("dispatch directory not found for %s: %r -> %s", session_id, missing.raw, missing.expanded)
            ledger.log(db, session_id, "directory:missing", {"raw": missing.raw, "expanded": missing.expanded})
            self.fail_card(session_id, self.t("missing_dir", raw=missing.raw, expanded=missing.expanded))  # stays open; never closed for it
            return
        # The dispatch script printing "dispatched" is not the work starting: the watchdog confirms it.
        instruction = dispatch.get("instruction") or ""
        if dispatch.get("brief"):
            instruction = f"{instruction}\n\n<file name=\"brief.md\">\n{dispatch['brief']}\n</file>".strip()
        self.track(session_id, session_id, instruction or name, "dispatch", started=created)
        log.info("registered %s (%s) with %d context messages", name, session_id, len(context))

    # ---------- OpenCode finished a run ----------

    def on_run_end(self, session_id: str, event: str, reply: str | None = None, session: str | None = None, run_end: bool = True):
        """A run ended (`session_id` is the handoff; `session` the OpenCode session the end came from).

        With a turn in flight, the end is checked against it first: an end from a session the
        handoff has left, or from before this turn reached the session (a run killed before it
        began), is not this turn's. A run that ended interrupted, killed or failed is never a
        delivery: the watchdog sends it again once, then stalls it (see recover).
        """
        row = self.delegation(session_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        db = self.db()
        if session and row.get("engine") == "opencode" and session != engines.session_of(row):
            ledger.log(db, row["id"], "run-end:old-session", {"session": session, "event": event})
            return
        if event == "interrupted" and self.own_interrupt(session or engines.session_of(row)):
            # Docket stopped it to send the work again (stop_turn): that is not a run end (audit W4).
            ledger.log(db, row["id"], "run-end:own-interrupt", {"session": session})
            return
        turn = ledger.open_turn(db, row["id"]) if run_end and event != "question" and row.get("engine") == "opencode" else None
        if turn and not turn["confirmed_at"] and turn["transport"] == "http" and self.confirm_turn(row, turn) == "queued":
            # The work before Docket's queued instruction ended; OpenCode takes that instruction up next,
            # and the PM verifies once it is done (its reply covers both).
            ledger.log(db, row["id"], "run-end:queued-next", {"turn": turn["id"], "event": event})
            return
        if turn:
            if not turn["confirmed_at"] and self.confirm_turn(row, turn) is False:
                if self.acting:
                    ledger.log(db, row["id"], "turn:stale-end", {"turn": turn["id"], "event": event})
                    return
                # Observing: a turn can look unconfirmed only because its message scrolled out of the newest
                # ones, so the end counts as this turn's and reaches the PM, as before the watchdog.
                self.observe(row["id"], turn, "ignore this run end", "unconfirmed end", event=event)
            if not ledger.end_turn(db, turn["id"], event):
                return
            ledger.log(db, row["id"], "turn:ended", {"turn": turn["id"], "outcome": event, "attempt": turn["attempt"]})
            # Stopped by someone in OpenCode (Docket did not stop it): a person's act, so the PM tells the
            # person where things stand and nothing is sent again on its own (audit W8).
            if event in UNFINISHED and event != "interrupted" and row["state"] != "limit":
                if self.acting:
                    self.expire_permissions(row["id"])
                    ledger.set_phase(db, row["id"], "awaiting_dispatch", why=event)
                    ledger.set_state(db, row["id"], "pending")
                    self.submit(row["id"], self.unfinished, row["id"], turn["id"], event, reply)
                    return
                # Observing: an unfinished run gets the PM's card, and the user decides what follows.
                self.observe(row["id"], turn, "send it again", event)
            ledger.set_phase(db, row["id"], "verifying")  # also when the turn was never seen running
        self.expire_permissions(session_id)
        ledger.set_state(self.db(), session_id, "pending")
        # One PM reading per batch of run ends (audit 4A-2): while a write_card still waits for its turn,
        # a newer run end only replaces what it will read, instead of queueing a second card.
        with self.locks_guard:
            waiting = session_id in self.run_ends
            self.run_ends[session_id] = (event, reply)
        if waiting:
            ledger.log(self.db(), session_id, "job:coalesced", {"job": "write_card", "event": event})
            return
        self.submit(session_id, self.write_card, session_id, event, reply)

    OWN_INTERRUPT_WINDOW = 120_000  # ms an interrupted event can follow Docket's own /interrupt

    def own_interrupt(self, session: str) -> bool:
        """Whether Docket itself interrupted this session a moment ago (stop_turn). Taken once."""
        db = self.db()
        found = db.execute("select value from kv where key = ?", (f"own_interrupt:{session}",)).fetchone()
        if not found:
            return False
        db.execute("delete from kv where key = ?", (f"own_interrupt:{session}",))
        return ledger.now() - int(found[0]) < self.OWN_INTERRUPT_WINDOW

    def expire_permissions(self, session_id: str):
        for stale in self.db().execute(
            "select id, body from cards where delegation_id = ? and resolved_at is null", (session_id,)
        ).fetchall():
            if json.loads(stale["body"]).get("pending_permission"):  # the run is over; it can no longer be answered
                ledger.resolve_card(self.db(), stale["id"], {"action": "expired"})
                ledger.log(self.db(), session_id, "permission:expired", {"card": stale["id"]})

    LIMIT_MARKERS = ("session limit", "usage limit", "quota", "rate limit exceeded", "resets at", "hit your limit")

    def retry_with_fallback(self, delegation_id: str, reply: str) -> bool:
        """A run that stopped on a usage limit is retried once on the fallback model."""
        row = self.delegation(delegation_id)
        fallback = self.settings.get("fallback_model")
        if not row or row.get("engine") != "opencode" or not fallback:
            return False
        if not any(marker in (reply or "").lower() for marker in self.LIMIT_MARKERS):
            return False
        last = self.db().execute(
            "select detail from events where delegation_id = ? and kind = 'relayed' order by id desc limit 1", (delegation_id,)
        ).fetchone()
        if not last or not last[0]:
            return False
        message = json.loads(last[0])["message"]
        if "fallback" in (json.loads(last[0]).get("via") or ""):
            return False  # already retried once
        log.warning("usage limit on %s, retrying on %s", delegation_id, fallback)
        self.db().execute(
            "insert into events (delegation_id, at, kind, detail) values (?, ?, 'relayed', ?)",
            (delegation_id, ledger.now(), json.dumps({"message": message, "via": "fallback", "model": fallback}, ensure_ascii=False)),
        )
        self.send(delegation_id, message, fallback, via="fallback")
        return True

    def write_card(self, session_id: str, event: str, reply: str | None = None):
        with self.locks_guard:  # the newest run end that came in while this job waited (see on_run_end)
            event, reply = self.run_ends.pop(session_id, (event, reply))
        row = self.delegation(session_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        if reply is None:
            reply = self.engine(row).last_reply(row)
        if row["state"] == "limit":  # the budget is spent: a handoff note, never another question
            return self.wrap_up(session_id, event, reply)
        if event in ("failed", "interrupted") and self.retry_with_fallback(session_id, reply):
            return
        # The PM verifies the run, then moves the work on itself when the brief settles what comes next,
        # and asks the person only what needs them (architecture, section 3). Its own continuations are
        # bounded: past continue_limit in a row it must show the person where things stand.
        left = max(0, self.continue_limit() - self.continues(session_id))
        last = ledger.last_turn(self.db(), session_id)
        outside = last["message"] if last and last["via"] == "foreign" else None  # see foreign()
        stopped = event == "interrupted"  # a person stopped it: a card, never another instruction (audit W8)
        if not self.pm_has_acted(session_id):
            message = pm.first_wake(row, json.loads(row["context"] or "[]"), event, reply, left, outside, stopped)
        else:
            message = pm.later_wake(event, reply, row["rounds"], left, outside, stopped)
        action = self.ask_pm(row, message, pm.validate_verify(left > 0 and not stopped, pm.STOPPED if stopped else None))
        if "action" not in action:  # a brief on its own, the older answer shape: a card
            action = {"action": "card", "card": action}
        now = self.delegation(session_id)["state"]
        if now in ("closed", "taken_over"):  # taken over or closed while the PM was reading the run
            ledger.log(self.db(), session_id, "pm:dropped", {"action": action["action"], "state": now})
            return
        self.carry_out(session_id, action, via="continue" if action["action"] == "relay" else None)

    def pm_has_acted(self, delegation_id: str) -> bool:
        """Whether a PM answer was ever carried out here: its first wake carries the rules and the background."""
        db = self.db()
        if db.execute("select 1 from events where delegation_id = ? and kind in ('exec:relay', 'exec:card', 'exec:close') limit 1", (delegation_id,)).fetchone():
            return True
        # Ledgers from before exec events: any card that is not the docket's own fault was the PM's.
        return any(not json.loads(r[0]).get("docket_fault") for r in db.execute("select body from cards where delegation_id = ?", (delegation_id,)))

    def wrap_up(self, delegation_id: str, event: str = "succeeded", reply: str | None = None):
        """At the limit the PM writes what got done and what is left; the limit card carries it."""
        row = self.delegation(delegation_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        action = self.ask_pm(row, pm.wrap_up_wake(event, reply, row["rounds"]), pm.validate_wrap_up)
        note = self.conclusion_text(action["conclusion"], action.get("remaining"))
        self.db().execute(
            "update cards set resolved_at = ?, resolution = ? where delegation_id = ? and resolved_at is null",
            (ledger.now(), json.dumps({"action": "superseded", "note": "wrap-up"}), delegation_id),
        )
        progress = f"{self.t('limit', n=ledger.ROUND_LIMIT)} {note}"
        ledger.add_card(self.db(), delegation_id, "limit", {"progress": progress, "decisions": [], "decided_for_you": [], "wrap_up": action})
        ledger.set_state(self.db(), delegation_id, "limit")
        ledger.log(self.db(), delegation_id, "wrap_up", {"conclusion": action["conclusion"], "remaining": action.get("remaining") or []})
        self.executed(delegation_id, "card", limit=True)

    # ---------- the user acted in the panel ----------

    def decide(self, card_id: int, choices: dict, reply: str, replay: bool = False):
        """The user answered a card. Every answer is one round, spent in the same transaction
        that resolves the card; what follows from it (a relay, a permission reply) is not another."""
        db = self.db()
        card = db.execute("select * from cards where id = ?", (card_id,)).fetchone()
        if not card or (card["resolved_at"] is not None and not replay):
            raise ValueError(self.t("err_answered"))
        row = self.delegation(card["delegation_id"])
        if row["state"] in ("closed", "taken_over"):
            raise ValueError(self.t("err_over"))
        body = json.loads(card["body"])
        resolution = {"choices": choices, "reply": reply}
        if body.get("orphan") and choices.get("retry") == "no":  # the user leaves the PM's stray answer undone
            if not ledger.resolve_open(db, card_id, {**resolution, "action": "declined"}):
                raise ValueError(self.t("err_answered"))
            ledger.log(db, row["id"], "orphan:declined", {"card": card_id})
            if body["orphan"].get("was") == "closed":
                ledger.set_state(db, row["id"], "closed")
            else:
                ledger.set_phase(db, row["id"], "awaiting_user")
                self.check_limit(row["id"]) or self.settle(row["id"])
            return
        if body.get("pm_retry"):  # the docket's own failure: running it again is not a round
            job = body["pm_retry"].get("job")
            if job not in self.RETRYABLE:  # only the docket's own jobs, whatever a card claims
                raise ValueError(self.t("err_answered"))
            if not replay:
                ledger.resolve_card(db, card_id, {**resolution, "action": "retry"})
            ledger.log(db, row["id"], "docket:retry", {"job": job, "card": card_id})
            ledger.set_state(db, row["id"], "pending")
            self.submit(row["id"], getattr(self, job), *body["pm_retry"]["args"])
            return
        if row["state"] == "limit" or card["kind"] == "limit":
            raise ValueError(self.t("err_limit"))
        rounds = row["rounds"]
        if replay or body.get("round_spent"):
            # A replay re-runs a past answer; a mention's confirmation was paid for by the mention.
            if not replay:
                ledger.resolve_card(db, card_id, resolution)
        else:
            if rounds >= ledger.ROUND_LIMIT:
                self.enter_limit(row["id"])
                raise ValueError(self.t("err_limit"))
            rounds = ledger.spend_round(db, row["id"], card_id, resolution)
            if rounds is None:
                raise ValueError(self.t("err_answered"))
        if body.get("pending_permission"):  # answering OpenCode's permission request, not the PM
            decision = "once" if choices.get("permission") == "once" else "reject"
            try:
                self.engine(row).answer_permission(row, body["pending_permission"], decision)
            except Exception as error:
                # The run may have finished in the same moment: say so plainly instead of a 500.
                ledger.log(db, row["id"], "permission:stale", {"error": str(error)[:300]})
                self.check_limit(row["id"]) or self.resume_after_permission(row["id"])
                raise ValueError(self.t("permission_expired")) from error
            self.check_limit(row["id"]) or self.resume_after_permission(row["id"])
            return
        if body.get("pending_relay"):  # confirming an instruction the docket held back for a yes
            if choices.get("relay") == "yes":
                ledger.set_state(db, row["id"], "pending")
                self.submit(row["id"], self.relay, row["id"], body["pending_relay"] + (f"\n\nThe user added: {reply}" if reply else ""))
            else:
                # Declined: nothing runs because of it. A waiting card goes up, or the PM says what next.
                if self.check_limit(row["id"]):
                    self.submit(row["id"], self.wrap_up, row["id"], "succeeded", None)
                else:
                    self.settle(row["id"])
            return
        # The person's own words, for the check on instructions that reach beyond the project.
        picked = [o["label"] for d in body.get("decisions") or [] for o in d.get("options") or [] if choices.get(d.get("id")) == o.get("id")]
        said = "; ".join(picked + ([reply] if reply else [])) or None
        ledger.set_state(db, row["id"], "pending")
        self.submit(row["id"], self.act, row["id"], pm.decision_wake(body, choices, reply, rounds), said)

    def check_limit(self, delegation_id: str) -> bool:
        """After the user's last round has been carried out, the handoff goes to the limit."""
        row = ledger.get(self.db(), delegation_id)
        if row and row["rounds"] >= ledger.ROUND_LIMIT and row["state"] not in ("closed", "taken_over"):
            self.enter_limit(delegation_id)
            return True
        return False

    def replay(self, card_id: int):
        """Run an answered card's decision again, e.g. after a bug stopped it the first time."""
        db = self.db()
        card = db.execute("select * from cards where id = ?", (card_id,)).fetchone()
        if not card or not card["resolution"]:
            raise ValueError(self.t("err_answered"))
        resolution = json.loads(card["resolution"])
        if "choices" not in resolution:
            raise ValueError(self.t("err_answered"))
        # Undo the previous failure card so the panel shows the retry, not the error.
        db.execute(
            "update cards set resolved_at = ?, resolution = ? where delegation_id = ? and resolved_at is null",
            (ledger.now(), json.dumps({"action": "superseded", "note": "replayed"}), card["delegation_id"]),
        )
        ledger.resolve_card(db, card_id, {**resolution, "replayed_at": ledger.now()})
        self.decide(card_id, resolution.get("choices") or {}, resolution.get("reply") or "", replay=True)

    def act(self, delegation_id: str, message: str, said: str | None = None, via: str | None = None, chat: str | None = None,
            was: str | None = None):
        """Wake the PM and carry out its action.

        `said` holds the person's own words behind this wake (a card's answer, a note, a chat
        message): an instruction that reaches beyond the project goes out only when they ask for it.
        `via` says where the wake came from (mention, note, handback, orphan, next-step). A chat
        mention (`chat`, "<transcript session>:<seq>") costs the person a round only when the PM
        takes it up, never when it holds it, and never twice for one message.
        """
        row = self.delegation(delegation_id)
        if via == "handback":  # may not close on its own nor skip the brief's reserved items (see pm.handback_wake)
            validate = pm.validate_handback(row["rounds"], row["brief"])
        elif via in ("mention", "note", "orphan"):
            validate = pm.validate_input
        else:
            validate = pm.validate_action
        action = self.ask_pm(row, message, validate)
        kind = action["action"]
        if action.get("checklist") is not None:
            ledger.log(self.db(), delegation_id, "pm:checklist", {"via": via, "items": action["checklist"]})
        now = self.delegation(delegation_id)["state"]
        if now in ("closed", "taken_over"):  # taken over (again) or closed while the PM was thinking
            ledger.log(self.db(), delegation_id, "pm:dropped", {"action": kind, "state": now})
            return
        if via in ("mention", "note") and kind != "hold" and action.get("settles_card"):
            self.words_settle_card(delegation_id, said, chat)
        self.carry_out(delegation_id, action, via, said)
        if kind == "hold" and was and self.delegation(delegation_id)["state"] == "pending":
            # Only the wake needed "pending": the person said something for the record and nothing follows.
            ledger.log(self.db(), delegation_id, "note:kept", {"was": was})
            ledger.set_state(self.db(), delegation_id, was, force=True)

    @staticmethod
    def answerable(card) -> bool:
        """A card the person's words can answer: the PM's own, not the docket's (permission, confirmation, fault)."""
        if card is None or card["kind"] in ("limit", "closed"):
            return False
        body = json.loads(card["body"])
        return not (body.get("pending_permission") or body.get("pending_relay") or body.get("docket_fault"))

    def words_settle_card(self, delegation_id: str, said: str | None, chat: str | None = None) -> bool:
        """The person's words answered the card on the panel: that is the round (2026-11-27). False when
        there was no card to answer, or it was answered on the panel in the same moment."""
        db = self.db()
        card = ledger.open_card(db, delegation_id)
        if not self.answerable(card):
            return False
        if chat and self.paid_for(delegation_id, chat):  # the same message already answered it through another path
            ledger.log(db, delegation_id, "mention:paid", {"chat": chat})
            return True
        if ledger.spend_round(db, delegation_id, card["id"], {"reply": said, "via": "chat"}) is None:
            return False
        if chat:
            ledger.log(db, delegation_id, "chat:paid", {"chat": chat, "via": "words"})
        return True

    def carry_out(self, delegation_id: str, action: dict, via: str | None = None, said: str | None = None):
        """Do what a PM answered, and record that it was done (exec:*)."""
        kind = action["action"]
        if kind == "relay":
            danger = risk.of(action["message"])
            if danger and not risk.asked_for(danger, said):
                # Reaches beyond the project and the person did not ask for it: their yes first.
                shown = self.confirm_relay(delegation_id, action["message"], danger, paid=said is not None and via != "orphan")
                self.executed(delegation_id, "card", card=shown, risk=danger)
            else:
                turn = self.relay(delegation_id, action["message"], via=via, delivery=action.get("delivery"))
                self.executed(delegation_id, "relay", turn=turn or None, **({"via": via} if via else {}), **({"blocked": True} if turn is False else {}))
        elif kind == "card":
            shown = self.show_card(delegation_id, action["card"].get("kind", "question"), action["card"])
            self.executed(delegation_id, "card", card=shown, **({} if shown else {"blocked": True}))
        elif kind == "hold":
            ledger.log(self.db(), delegation_id, "pm:hold", {"via": via, "note": (action.get("note") or "")[:300]})
            self.executed(delegation_id, "hold")  # act() puts the state back when the wake needed "pending"
        else:
            self.finish(delegation_id, action["conclusion"], action.get("remaining"))
            self.executed(delegation_id, "close")
        if self.check_limit(delegation_id) and kind == "card":
            # Nothing goes out to carry the last round on, so the wrap-up would never be written:
            # ask the PM for the handoff note now, so the limit card carries it (2026-11-27).
            self.submit(delegation_id, self.wrap_up, delegation_id, "succeeded", None)

    def confirm_relay(self, delegation_id: str, message: str, danger: str, paid: bool):
        """A card asking the person whether an instruction that reaches beyond the project may go out.
        It costs no round when the round that led to it was already spent (`paid`)."""
        t = self.t
        ledger.log(self.db(), delegation_id, "relay:held", {"risk": danger, "message": message[:300]})
        card = {
            "kind": "question",
            "progress": t("risky_progress", risk=t(f"risk_{danger}"), text=" ".join(message.split())[:100]),
            "decisions": [{"id": "relay", "question": t("risky_question"), "options": [
                {"id": "yes", "label": t("confirm_yes"), "recommended": False},
                {"id": "no", "label": t("confirm_no"), "recommended": True},
            ]}],
            "decided_for_you": [],
            "pending_relay": message,
            "round_spent": paid,
            "risk": danger,
        }
        return self.show_card(delegation_id, "question", card)

    def continues(self, delegation_id: str) -> int:
        """Relays the PM made on its own after a run, since the person last took part."""
        db = self.db()
        since = db.execute("select max(id) from events where delegation_id = ? and kind = 'round'", (delegation_id,)).fetchone()[0] or 0
        rows = db.execute("select detail from events where delegation_id = ? and kind = 'relayed' and id > ?", (delegation_id, since)).fetchall()
        return sum(1 for (detail,) in rows if json.loads(detail or "{}").get("via") == "continue")

    def continue_limit(self) -> int:
        value = self.settings.get("continue_limit")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else docket_config.DEFAULTS["continue_limit"]

    def relay(self, delegation_id: str, message: str, model: str | None = None, via: str | None = None, delivery: str | None = None):
        """Hand an instruction to the coding agent. Rounds are the person's card answers; what the PM
        relays on its own is bounded separately by continue_limit (see continues)."""
        turn = self.send(delegation_id, message, model, via=via or "relay", delivery=delivery)
        ledger.log(self.db(), delegation_id, "relayed", {"message": message[:500], **({"via": via} if via else {}), **({"delivery": delivery} if delivery else {})})
        # A card still on show (one left from before a handback), or one waiting behind the card just answered.
        if ledger.open_card(self.db(), delegation_id) or ledger.next_queued(self.db(), delegation_id):
            ledger.set_state(self.db(), delegation_id, "awaiting")
        self.check_limit(delegation_id)
        return turn

    def retry(self, delegation_id: str):
        """Send the last relayed instruction again, on the fallback model."""
        row = self.delegation(delegation_id)
        fallback = self.settings.get("fallback_model")
        last = self.db().execute(
            "select detail from events where delegation_id = ? and kind = 'relayed' order by id desc limit 1", (delegation_id,)
        ).fetchone()
        if not row or not fallback or not last or not last[0]:
            raise ValueError(self.t("err_missing"))
        if row["state"] in ("closed", "taken_over"):
            raise ValueError(self.t("err_over"))
        if ledger.open_card(self.db(), delegation_id):
            raise ValueError(self.t("err_open_card"))
        message = json.loads(last[0])["message"]
        self.send(delegation_id, message, fallback, via="fallback")
        ledger.log(self.db(), delegation_id, "relayed", {"message": message, "via": "fallback", "model": fallback})
        return fallback

    def close(self, delegation_id: str, reply: str):
        """The person closes the handoff on the panel. That is final the moment they say it: the handoff
        closes now and the PM writes the conclusion after (see conclude), so a PM that is slow or fails
        can only make the conclusion plainer, never keep open what the person closed."""
        row = self.delegation(delegation_id)
        if not row or row["state"] == "closed":
            raise ValueError(self.t("err_closed"))
        card = ledger.open_card(self.db(), delegation_id)
        note = json.loads(card["body"]).get("wrap_up") if card and card["kind"] == "limit" else None
        if card:
            ledger.resolve_card(self.db(), card["id"], {"action": "close", "reply": reply})
        if note and not reply:  # the PM already wrote the handoff note when the budget ran out
            self.finish(delegation_id, note["conclusion"], note.get("remaining"))
            self.executed(delegation_id, "close", by="user")
            return
        closed = self.finish(delegation_id, self.t("closed_fallback"), extra={"drafting": True}, memory=False)
        if closed is None:  # closed in the same moment by something else: that closing stands
            raise ValueError(self.t("err_closed"))
        self.executed(delegation_id, "close", by="user")
        self.submit(delegation_id, self.conclude, delegation_id, reply, row["state"] == "limit", closed)

    def conclude(self, delegation_id: str, reply: str, at_limit: bool, card_id: int):
        """After the person closed a handoff: the PM's one-sentence conclusion takes the placeholder's
        place on the closed card and goes to the memory log. Without a usable answer the plain one stays."""
        row = self.delegation(delegation_id)
        conclusion, remaining = self.last_progress(delegation_id) or self.t("closed_fallback"), None
        try:
            if at_limit:
                message = pm.wrap_up_wake("closed", None, row["rounds"])
                message += f"\n\nThey added when closing:\n{pm.user_words('panel', reply.strip())}" if reply.strip() else ""
            else:
                message = pm.close_wake(reply)
            action = self.ask_pm(row, message, pm.validate_close)
            conclusion, remaining = action["conclusion"], action.get("remaining")
        except Exception as error:  # the closing stands and reaches the memory log; only the conclusion stays plain
            log.warning("conclusion for %s: %s", delegation_id, error)
            ledger.log(self.db(), delegation_id, "conclude:failed", {"error": str(error)[:300]})
        text = self.conclusion_text(conclusion, remaining)
        ledger.set_body(self.db(), card_id, {"progress": text, "conclusion": text, "remaining": remaining or [],
                                             "decisions": [], "decided_for_you": []})
        self.write_memory(self.delegation(delegation_id), text)

    def last_progress(self, delegation_id: str) -> str | None:
        """Where the PM last said things stood: the fallback conclusion when it cannot write one."""
        for (body,) in self.db().execute("select body from cards where delegation_id = ? and kind != 'closed' order by id desc", (delegation_id,)):
            body = json.loads(body)
            if not (body.get("docket_fault") or body.get("pending_permission") or body.get("pending_relay")) and body.get("progress"):
                return str(body["progress"])[:pm._limits()["conclusion"]]
        return None

    def conclusion_text(self, conclusion: str, remaining: list | None) -> str:
        items = [r.strip() for r in remaining or [] if r and r.strip()]
        if not items:
            return conclusion
        separator = "；" if self.locale == "zh" else "; "
        return f"{conclusion} {self.t('remaining', items=separator.join(items))}"

    def finish(self, delegation_id: str, conclusion: str, remaining: list | None = None, extra: dict | None = None,
               memory: bool = True) -> int | None:
        """The only way a handoff closes: the PM judged it done, the user closed it, the budget ran out,
        or nobody answered its card for quiet_expire_minutes (`extra` then says so on the closed card).

        Once per closing, whoever gets there first: one conclusion card, one memory entry (`memory=False`
        leaves that entry to whoever writes the final conclusion, see conclude). Returns the conclusion
        card, or None when the handoff was closed already. Open cards are withdrawn and an instruction
        still in flight is ended as closed, so a note that opens the handoff again starts clean."""
        db = self.db()
        row = self.delegation(delegation_id)
        if not row:
            return None
        closings = db.execute("select count(*) from cards where delegation_id = ? and kind = 'closed'", (delegation_id,)).fetchone()[0]
        if row["state"] == "closed" or not ledger.claim(db, f"finish:{delegation_id}:{closings}"):
            ledger.log(db, delegation_id, "finish:again", {"conclusion": (conclusion or "")[:200]})
            return None
        conclusion = self.conclusion_text(conclusion, remaining)
        for card in db.execute("select id from cards where delegation_id = ? and resolved_at is null", (delegation_id,)).fetchall():
            ledger.resolve_card(db, card["id"], {"action": "closed"})
        for turn in db.execute("select id from turns where delegation_id = ? and ended_at is null", (delegation_id,)).fetchall():
            ledger.end_turn(db, turn["id"], "closed")
        body = {"progress": conclusion, "conclusion": conclusion, "remaining": remaining or [], "decisions": [], "decided_for_you": []}
        card_id = ledger.add_conclusion(db, delegation_id, {**body, **(extra or {})})
        ledger.set_state(db, delegation_id, "closed")
        if memory:
            self.write_memory(row, conclusion)
        return card_id

    def write_memory(self, row: dict, conclusion: str):
        """Conclusions flow back into OpenClaw's daily log so the main thread knows."""
        today = dt.datetime.now()
        path = MEMORY_DIR / f"{today:%Y-%m-%d}.md"
        t = self.t
        colon = "：" if self.locale == "zh" else ": "
        entry = (
            f"\n#### {today:%H:%M} [{t('memory_title', title=row['title'])}]\n\n"
            f"- {t('memory_kind', id=row['id'], rounds=row['rounds'])}\n"
            f"- **{t('memory_body')}**{colon}{conclusion}\n"
        )
        try:
            if not path.exists():
                path.write_text(f"# {today:%Y-%m-%d}\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(entry)
        except OSError as error:
            log.warning("cannot write memory: %s", error)

    def rewrite(self, delegation_id: str):
        """Have the PM write the open card again, e.g. after the language changed."""
        row = self.delegation(delegation_id)
        card = ledger.open_card(self.db(), delegation_id)
        if not row or not card or row["state"] != "awaiting" or card["kind"] in ("limit", "closed"):
            raise ValueError(self.t("err_answered"))
        if json.loads(card["body"]).get("pending_relay"):
            raise ValueError(self.t("err_answered"))
        ledger.resolve_card(self.db(), card["id"], {"action": "superseded"})
        event = {"failure": "failed", "question": "question"}.get(card["kind"], "succeeded")
        last_run = self.db().execute("select out from runs where delegation_id = ? order by id desc limit 1", (delegation_id,)).fetchone()
        reply = self.engine(row).parse(row, Path(last_run["out"]))["reply"] if last_run else None
        self.on_run_end(delegation_id, event, reply, run_end=False)

    def takeover(self, delegation_id: str):
        row = self.delegation(delegation_id)
        if not row:
            raise ValueError(self.t("err_missing"))
        card = ledger.open_card(self.db(), delegation_id)
        if card:
            ledger.resolve_card(self.db(), card["id"], {"action": "takeover"})
        if row["state"] != "closed":
            ledger.set_state(self.db(), delegation_id, "taken_over")
        for turn in self.db().execute("select id from turns where delegation_id = ? and ended_at is null", (delegation_id,)).fetchall():
            ledger.end_turn(self.db(), turn["id"], "taken_over")  # the user drives it now: nothing for the watchdog
        self.engine(row).takeover(row)

    def handback(self, delegation_id: str) -> str:
        """The user hands a taken-over handoff back: the PM carries on at its own pace.

        Only from taken_over; anything else is refused, a second click included. Rounds are
        not reset (see ledger.hand_back) and handing back costs none. The PM is told in one
        short message; how to split the rest and what to push next is its call. Its first
        relay after a handback does not count against the relay gate, since no answer paid for it.
        """
        row = self.delegation(delegation_id)
        if not row:
            raise ValueError(self.t("err_missing"))
        state = ledger.hand_back(self.db(), delegation_id)
        if state is None:
            raise ValueError(self.t("err_not_taken_over"))
        log.info("handed back %s (%s)", delegation_id, state)
        message = pm.handback_wake(self.latest_reply(row), row["rounds"], row["brief"])
        self.submit(delegation_id, self.act, delegation_id, message, None, "handback")
        return state

    def latest_reply(self, row: dict) -> str:
        """The coding agent's last reply, best effort: the user may have worked in the session meanwhile."""
        try:
            reply = self.engine(row).last_reply(row)
            if reply:
                return reply
            last_run = self.db().execute("select out from runs where delegation_id = ? order by id desc limit 1", (row["id"],)).fetchone()
            return self.engine(row).parse(row, Path(last_run["out"]))["reply"] if last_run else ""
        except Exception as error:
            log.debug("last reply unavailable for %s: %s", row["id"], error)
            return ""

    # ---------- main-thread mentions ----------

    def watch_mentions(self):
        db = self.db()
        while True:
            try:
                self.reload_settings()
                if self.settings["attribute"] and self.judging:
                    self.scan_mentions(db)
                    self.transcript_state(None)
            except transcript.Unavailable as error:
                self.transcript_state(error)
            except Exception:
                log.exception("mention scan failed")
            time.sleep(MENTION_POLL)

    def transcript_state(self, error):
        """One readable line when the transcript database goes away or comes back, never a stack every poll."""
        reason = str(error) if error else None
        if reason == getattr(self, "transcript_fault", None):
            return
        if reason:
            log.warning("%s; mentions and new dispatches wait until it opens again (run scripts/selfcheck.py)", reason)
            ledger.log(self.db(), None, "transcript:unavailable", {"path": str(error.path), "source": error.source, "error": str(error.error)})
        elif getattr(self, "transcript_fault", None):
            log.info("the OpenClaw transcript database opens again")
        self.transcript_fault = reason

    def scan_mentions(self, db):
        live = [dict(r) for r in db.execute(f"select * from delegations where state in {LIVE}").fetchall()]
        conn = openclaw_connect()
        try:
            # One entry per session: its heartbeat window is the same conversation, not another one.
            sessions = [(key, sid) for sid, key in transcript.session_keys(conn).items() if docket_config.watched(self.settings, key)]
            for key, session_id in sessions:
                cursor_key = f"cursor:{session_id}"
                stored = db.execute("select value from kv where key = ?", (cursor_key,)).fetchone()
                latest = conn.execute("select max(seq) from transcript_events where session_id = ?", (session_id,)).fetchone()[0] or 0
                if stored is None:  # first sight: start from now, never replay history
                    db.execute("insert or replace into kv values (?, ?)", (cursor_key, str(latest)))
                    continue
                since_seq = int(stored[0])
                if latest <= since_seq:
                    continue
                db.execute("insert or replace into kv values (?, ?)", (cursor_key, str(latest)))
                if not live:
                    continue
                history = transcript_messages(conn, session_id, since=ledger.now() - 86400_000)
                for index, message in enumerate(history):
                    if message.seq > since_seq and message.seq <= latest and message.role == "user":
                        self.attribute(message, history[max(0, index - 3) : index], live, session_id)
        finally:
            conn.close()

    def attribute(self, message, recent, live, session_id: str | None = None):
        view = self.message_view
        active = [self.judge_task(row) for row in live if row["dispatched_at"] < message.time]
        if not active:
            return
        answer, _ = relevance.attribute(view(message), [view(m) for m in recent], active)
        moves = relevance.should_move(answer, self.settings["move_threshold"])
        # Only a reading that moves the message belongs to a handoff's own record.
        ledger.log(self.db(), answer.get("choice") if moves else None, "attributed",
                   {"seq": message.seq, **{k: answer[k] for k in ("choice", "belongs", "new_work")}, "moved": moves})
        if not moves:
            return
        delegation_id = answer["choice"]
        row = self.delegation(delegation_id)
        log.info("main-thread message m%s belongs to %s", message.seq, row["title"])
        # The main agent usually passes the same words on with docket note within seconds, and that
        # deliberate path wins: the judge's reading is acted on only if nobody took the message meanwhile.
        self.later(MENTION_DELAY, self.take_mention, message, delegation_id, session_id)

    def later(self, seconds: float, fn, *args):
        timer = threading.Timer(seconds, fn, args)
        timer.daemon = True
        timer.start()

    def take_mention(self, message, delegation_id: str, session_id: str | None):
        """MENTION_DELAY after the judge attributed a chat message: wake the PM on it, unless a note,
        the unanswered-card sweep or another handoff took the same message first. One message, one round."""
        if session_id and not ledger.claim(self.db(), f"chat_claim:{session_id}:{message.seq}"):
            ledger.log(self.db(), delegation_id, "mention:claimed", {"seq": message.seq})
            return
        row = self.delegation(delegation_id)
        if not row or row["state"] not in LIVE:
            return
        if row["rounds"] >= ledger.ROUND_LIMIT:
            ledger.log(self.db(), delegation_id, "mention:blocked", {"seq": message.seq})
            self.enter_limit(delegation_id)
            return
        # The PM decides what the message means. Nothing is spent or answered here: steering the work
        # costs no round, and the card is answered only if the PM says these words settle it. The handoff
        # keeps its state meanwhile: the panel shows what is true.
        card = ledger.open_card(self.db(), delegation_id)
        up = json.loads(card["body"]) if self.answerable(card) else None
        chat = f"{session_id}:{message.seq}" if session_id else None
        ledger.log(self.db(), delegation_id, "mention", {"seq": message.seq, "chat": chat, "card": card["id"] if up else None})
        self.submit(delegation_id, self.act, delegation_id, pm.mention_wake(message.text, row["rounds"], up), message.text, "mention", chat)

    # ---------- OpenCode events ----------

    def watch_events(self):
        while True:
            try:
                for event in Service().events():
                    self.handle(event)
            except Exception as error:
                log.debug("event stream dropped: %s", error)
            time.sleep(3)

    ACTIVITY_EVERY = 15_000  # ms: signs of work are written to the turn at most this often per session

    def handle(self, event: dict):
        kind = event.get("type", "")
        data = event.get("data") or {}
        # Most events carry data.sessionID; form and permission events may nest it one level down.
        nested = next((v for v in data.values() if isinstance(v, dict) and "sessionID" in v), {})
        session_id = data.get("sessionID") or nested.get("sessionID")
        if not session_id:
            return
        if kind in ACTIVITY_EVENTS:
            return self.on_activity(session_id)
        if kind == "session.inbox.delivered":
            return self.on_delivered(session_id, data.get("inboxID"))
        if kind in ("session.created", "session.renamed"):
            threading.Thread(target=self.maybe_register, args=(session_id,), daemon=True).start()
            return
        # A handoff the watchdog moved to a new session hears from that session.
        row = self.delegation_for_session(session_id)
        if not row:
            return
        if kind.startswith("session.execution.") and kind != "session.execution.started":
            self.on_run_end(row["id"], kind.rsplit(".", 1)[-1], session=session_id)
        elif kind == "session.execution.started":
            if row["state"] in ("pending", "awaiting") and not ledger.open_card(self.db(), row["id"]):
                ledger.set_state(self.db(), row["id"], "running")
            turn = ledger.open_turn(self.db(), row["id"])
            if turn and not turn["confirmed_at"] and turn["session"] == session_id:
                self.confirm_turn(self.delegation(row["id"]), turn)
        elif kind == "form.created":
            self.on_run_end(row["id"], "question", session=session_id)
        elif kind == "permission.asked":
            self.permission_card(row["id"], data if str(data.get("id", "")).startswith("per_") else nested)

    def on_activity(self, session_id: str):
        """A step or tool call in a session: work is going on, whatever the messages and files say (audit W6)."""
        now = ledger.now()
        seen = getattr(self, "activity_at", None)
        if seen is None:
            seen = self.activity_at = {}
        if now - seen.get(session_id, 0) < self.ACTIVITY_EVERY:
            return
        seen[session_id] = now
        self.db().execute("update turns set last_activity = ? where session = ? and ended_at is null and confirmed_at is not null", (now, session_id))

    def on_delivered(self, session_id: str, message_id: str | None):
        """An instruction reached the agent in a handoff's session: Docket's own (by its id), or someone else's."""
        row = self.delegation_for_session(session_id)
        if not row or not message_id or row["state"] in ("closed", "taken_over"):
            return
        db = self.db()
        mine = db.execute("select * from turns where delegation_id = ? and message_id = ?", (row["id"], message_id)).fetchone()
        if mine:
            if mine["ended_at"] is None:
                self.mark_running(row["id"], mine, "delivered")
            return
        try:
            message = Service().message(session_id, message_id) or {}
        except Exception as error:
            log.debug("delivered %s: cannot read the message: %s", message_id, error)
            message = {}
        if (message.get("metadata") or {}).get("docket"):
            return  # Docket's own, from a turn that has since been replaced
        text = Service.text_of(message)
        turn = ledger.open_turn(db, row["id"])
        if turn and turn["transport"] != "http" and not turn["message_id"] and text and text[:200] == (turn["message"] or "")[:200]:
            # Sent through `opencode run -s`, which picks its own message id: Docket's turn after all.
            db.execute("update turns set message_id = ? where id = ?", (message_id, turn["id"]))
            return self.mark_running(row["id"], turn, "delivered")
        self.foreign(row, session_id, message_id, text, turn)

    def foreign(self, row: dict, session_id: str, message_id: str, text: str, turn):
        """An instruction someone else wrote into the handoff's session (the main agent running opencode
        itself, a person typing in OpenCode). Recorded, and followed as a turn when none is open, so its
        end still reaches the PM, which is told the run was not Docket's (audit 4A-4)."""
        db = self.db()
        ledger.log(db, row["id"], "turn:foreign", {"session": session_id, "message_id": message_id, "text": text[:300],
                                                  "during": turn["id"] if turn else None})
        log.warning("an instruction reached %s outside Docket: %s", row["title"], text[:120])
        if turn:
            return
        absorbed = self.track(row["id"], session_id, text or "(not readable)", "foreign", run={"message": message_id, "transport": "http"})
        self.mark_running(row["id"], ledger.open_turn(db, row["id"]), "delivered")
        if self.delegation(row["id"])["state"] == "pending" and not self.busy(row["id"]):
            ledger.set_state(db, row["id"], "running")
        ledger.log(db, row["id"], "turn:absorbed", {"turn": absorbed})

    @staticmethod
    def _destructive(resources: str) -> bool:
        lowered = resources.lower()
        return any(marker in lowered for marker in ("rm -rf", "rm ", "sudo ", "chmod -r", "mv ", "trash", "/etc/", "/usr/", "/system/"))

    def project_dir(self, row: dict) -> Path | None:
        """The handoff's own directory, never the whole Code workspace around it."""
        directory = row.get("directory")
        if not directory:
            args = self.engine(row).args or []
            directory = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--dir", "--cwd", "-C")), None)
        # Rows stored before variables were expanded still hold "$HOME/…": read them the same way.
        return dispatches.expand_dir(directory) if directory else None

    def source_tool(self, session_id: str, source: dict | None) -> str | None:
        """The tool call behind a request: OpenCode's external_directory says which folder, not what for."""
        if not source or source.get("type") != "tool":
            return None
        try:
            for message in Service().messages(session_id, limit=10):
                for part in message.get("content") or []:
                    if part.get("type") == "tool" and part.get("id") == source.get("id"):
                        return part.get("name")
        except Exception as error:
            log.debug("tool lookup failed: %s", error)
        return None

    def permission_tier(self, session_id: str, row: dict, request: dict) -> tuple[str, str]:
        """("auto" | "ask", reason). Interrupting the user is budgeted: only for writes and deletes
        the machine cannot vouch for. Anything it cannot place is treated as dangerous."""
        action = request.get("action")
        raw = [str(r) for r in request.get("resources") or []]
        if not raw:
            return "ask", "no resources"
        if action in READ_ACTIONS:
            direction = "read"
        elif action in WRITE_ACTIONS:
            direction = "write"
        elif action == "external_directory":
            tool = self.source_tool(session_id, request.get("source"))
            meta = request.get("metadata") or {}
            direction = "write" if tool in WRITE_ACTIONS or meta.get("filepath") else "read" if tool in READ_ACTIONS else None
        elif action == "shell":
            direction = "shell"
        else:
            return "ask", f"unknown action {action}"
        # Reading cannot destroy anything, so the path markers in _destructive (/etc/, /usr/) only guard changes.
        if direction != "read" and self._destructive(" ".join(raw)):
            return "ask", "destructive"
        project = self.project_dir(row)
        if direction == "shell":
            # Paths a command reaches outside the project arrive as their own external_directory request.
            return ("auto", "project shell") if project else ("ask", "shell, project unknown")
        if project:
            # External folders arrive as globs ("/etc/*"); drop the glob, keep a bare "/" as the root.
            paths = [Path(p[:-2] if p.endswith("/*") and len(p) > 2 else p.rstrip("*") or ".").expanduser() for p in raw]
            if all((path if path.is_absolute() else project / path).resolve().is_relative_to(project) for path in paths):
                return "auto", f"inside project, {direction or 'unknown'}"
        if direction == "read":
            return "auto", "outside project, read only"
        if direction is None:
            return "ask", "outside project, direction unknown"
        return "ask", "outside project, write"

    def unattended(self, row: dict) -> bool:
        """Whether a run approves its own ordinary requests: `--auto`, or an OpenCode session Docket
        drives over HTTP, where there is no client to answer them (Docket answers, see permission_card)."""
        engine = self.engine(row)
        if "--auto" in (engine.args or []):
            return True
        return isinstance(engine, engines.OpenCode) and engine.over_http()

    def permission_card(self, session_id: str, request: dict):
        """A run that still asks for permission must not wait silently: ask on Docket."""
        row = self.delegation(session_id)
        if not row or row["state"] in ("closed", "taken_over") or not request.get("id"):
            return
        t = self.t
        # A run started with --auto was an explicit "let it approve its own requests". OpenCode
        # still asks for paths outside the project, and those requests expire within seconds.
        # Approve what the machine can vouch for (anything in the project, reads anywhere) and
        # log it; leave writes outside the project and deletes alive for the popup and the card.
        resources = " ".join(str(r) for r in request.get("resources") or [])
        tier, reason = self.permission_tier(session_id, row, request) if self.unattended(row) else ("ask", "not unattended")
        entry = {"action": request.get("action"), "resources": resources[:200], "reason": reason}
        if tier == "auto":
            try:
                self.engine(row).answer_permission(row, request["id"], "once")
                ledger.log(self.db(), session_id, "permission:auto", entry)
                return
            except Exception as error:
                log.debug("auto-approve failed: %s", error)
        ledger.log(self.db(), session_id, "permission:ask", entry)
        temp = any(marker in resources for marker in ("/tmp", "/var/folders", "TMPDIR"))
        delete = "rm -rf" in resources or "rm " in resources[:40]
        if delete:
            action = t("permission_delete_temp") if temp else t("permission_delete_files")
            risk = t("permission_risk_delete")
        elif request.get("action") == "external_directory" and reason == "outside project, read only":
            action, risk = t("permission_read_files"), t("permission_risk_read")
        else:
            action, risk = t("permission_run_command"), t("permission_risk_change")
        progress = t("permission_sentence", action=action, risk=risk)
        card = {
            "kind": "question",
            "progress": progress,
            "decisions": [{
                "id": "permission",
                "question": t("permission_question"),
                "options": [
                    {"id": "once", "label": t("permission_once"), "recommended": True},
                    {"id": "reject", "label": t("permission_reject"), "recommended": False},
                ],
            }],
            "decided_for_you": [],
            "pending_permission": request["id"],
        }
        self.show_card(session_id, "question", card)

    def reconcile(self):
        """Catch up on what happened while the daemon was down."""
        # Cards left waiting on a handoff that was closed since (an older build could leave them).
        for card in self.db().execute(
            "select c.id from cards c join delegations d on d.id = c.delegation_id "
            "where c.resolved_at is null and d.state = 'closed' and c.kind != 'closed'"
        ).fetchall():
            ledger.resolve_card(self.db(), card["id"], {"action": "closed"})
        # Closed cards an older build left unresolved: records of an ending, never cards that wait.
        conclusions = self.db().execute(
            "select c.id from cards c join delegations d on d.id = c.delegation_id "
            "where c.resolved_at is null and d.state = 'closed' and c.kind = 'closed'"
        ).fetchall()
        for card in conclusions:
            ledger.resolve_card(self.db(), card["id"], {"action": "conclusion", "note": "reconcile"})
        if conclusions:
            ledger.log(self.db(), None, "reconcile:conclusions", {"cards": len(conclusions)})
            log.info("reconcile: resolved %d closed cards an older build left open", len(conclusions))
        self.prune_stale_keys()
        self.close_overdue()
        installed = int(self.db().execute("select value from kv where key = 'installed_at'").fetchone()[0])
        try:
            service = Service()
            sessions = service.recent_sessions(limit=40)
        except Exception as error:
            log.warning("reconcile: OpenCode not reachable: %s", error)
            return
        prefix = self.settings["prefix"].strip()
        for session in sessions:
            created = (session.get("time") or {}).get("created", 0)
            if created >= installed and (session.get("title") or "").startswith(prefix):
                self.maybe_register(session["id"], attempts=1)
        for row in self.db().execute("select * from delegations where state in ('running', 'pending') and engine = 'opencode'").fetchall():
            # A PM job cut off by a restart leaves the handoff pending: put it back where it belongs.
            if row["state"] == "pending" and ledger.open_card(self.db(), row["id"]):
                ledger.set_state(self.db(), row["id"], "awaiting")
                continue
            try:
                session = service.session(engines.session_of(dict(row)))
            except Exception:
                continue
            times = session.get("time") or {}
            busy = (times.get("busy") or 0) > (times.get("idle") or 0)
            if busy:
                if row["state"] == "pending":
                    ledger.set_state(self.db(), row["id"], "running")
            elif session.get("outcome") and (row["state"] == "pending" or (times.get("idle") or 0) > row["updated_at"]):
                self.on_run_end(row["id"], session["outcome"], session=engines.session_of(dict(row)))

    def prune_stale_keys(self):
        """The sweep's per-card notes go with their card; message claims after a week."""
        db = self.db()
        for (key,) in db.execute("select key from kv where key like 'stale_seq:%' or key like 'quiet_woken:%' or key like 'quiet_extend:%'").fetchall():
            card = db.execute("select resolved_at from cards where id = ?", (int(key.split(":", 1)[1]),)).fetchone()
            if not card or card[0] is not None:
                db.execute("delete from kv where key = ?", (key,))
        db.execute("delete from kv where key like 'chat_claim:%' and cast(value as integer) < ?", (ledger.now() - 7 * 86400_000,))

    def close_overdue(self):
        """A pending handoff left far longer than any PM job takes was not cut off by this
        restart: its work most likely went on without the docket. It is closed with a note,
        never woken. Running ones are left to reconcile, since a process may still be at it."""
        db = self.db()
        cutoff = ledger.now() - float(self.settings["reconcile_max_age_hours"]) * 3600_000
        for row in db.execute(
            "select * from delegations where state = 'pending' and engine = 'opencode' and updated_at < ?", (cutoff,)
        ).fetchall():
            hours = (ledger.now() - row["updated_at"]) // 3600_000
            ledger.log(db, row["id"], "orphaned", {"dispatched_at": row["dispatched_at"], "updated_at": row["updated_at"], "hours": hours})
            log.info("reconcile: closing %s, pending for %d hours", row["id"], hours)
            self.finish(row["id"], self.t("closed_overdue", hours=hours))


# ---------- helpers for the watchdog ----------

_EXTERNAL = re.compile(r"^\[Inter-session message\]([^\n]*)\n(?:This content was routed by OpenClaw[^\n]*\n)?", re.S)


def external_source(text: str) -> tuple[str, str]:
    """(who sent it, what they said) for a message that reached a PM's session from outside the docket.
    OpenClaw marks a sessions_send with a header line ("[Inter-session message] sourceSession=… sourceTool=…")."""
    match = _EXTERNAL.match(text or "")
    if not match:
        return "outside Docket", (text or "").strip()
    fields = dict(re.findall(r"(\w+)=(\S+)", match.group(1)))
    source = fields.get("sourceSession") or "another session"
    if fields.get("sourceTool"):
        source += f" via {fields['sourceTool']}"
    return source, text[match.end():].strip()


SKIP_DIRS = {".git", "node_modules", ".build", "build", "DerivedData", "__pycache__", ".venv", "venv", "dist", "target", ".next", ".cache"}


def project_files(root: Path, since_ms: int, limit: int = 20000) -> tuple[int, list[str]]:
    """(newest mtime in ms, files changed since `since_ms`, relative) under a project, build output left out."""
    newest, changed, seen = 0, [], 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            seen += 1
            if seen > limit:
                return newest, sorted(changed)
            try:
                mtime = int(os.stat(os.path.join(folder, name)).st_mtime * 1000)
            except OSError:
                continue
            newest = max(newest, mtime)
            if since_ms and mtime >= since_ms:
                changed.append(os.path.relpath(os.path.join(folder, name), root))
    return newest, sorted(changed)


def health(db, start: int, end: int) -> dict:
    """One day of the watchdog, from the events: stuck, healed by itself, needed a person."""
    def events(*kinds):
        marks = ",".join("?" * len(kinds))
        return [dict(r) for r in db.execute(
            f"select delegation_id, at, kind, detail from events where at >= ? and at < ? and kind in ({marks}) order by id", (start, end, *kinds))]

    def detail(e):
        return json.loads(e["detail"] or "{}")

    stuck = events("watchdog:not-started", "watchdog:quiet", "orphan:found")
    # An interrupted run is a person's stop, not a fault (see on_run_end): it is not counted as stuck.
    stuck += [e for e in events("turn:ended") if detail(e).get("outcome") in ("failed", "killed")]
    stuck += [e for e in events("docket:failed") if not detail(e).get("stalled")]  # the PM's own faults
    healed = [e for e in events("turn:running") if detail(e).get("via") == "resend"] + events("orphan:auto", "orphan:folded")
    human = events("docket:failed")
    observed = events("watchdog:observed")
    titles = {r["id"]: r["title"] for r in db.execute("select id, title from delegations")}

    def lines(items):
        return [{"handoff": e["delegation_id"], "title": titles.get(e["delegation_id"], ""), "at": dt.datetime.fromtimestamp(e["at"] / 1000).strftime("%H:%M:%S"),
                 "kind": e["kind"], "detail": detail(e)} for e in items]

    return {"stuck": len(stuck), "healed": len(healed), "needed_a_person": len(human), "observed": len(observed),
            "details": {"stuck": lines(stuck), "healed": lines(healed), "needed_a_person": lines(human), "observed": lines(observed)}}


def health_text(day, report: dict) -> str:
    out = [f"# Docket health {day:%Y-%m-%d}", "",
           f"- stuck: {report['stuck']}", f"- healed by itself: {report['healed']}", f"- needed a person: {report['needed_a_person']}",
           f"- the watchdog would have acted (observe mode): {report.get('observed', 0)}"]
    for name, title in (("stuck", "Stuck"), ("healed", "Healed by itself"), ("needed_a_person", "Needed a person"), ("observed", "Would have acted (observe mode)")):
        items = report["details"][name]
        if items:
            out += ["", f"## {title}"] + [f"- {i['at']} {i['kind']} · {i['title'] or i['handoff']} · {json.dumps(i['detail'], ensure_ascii=False)[:200]}" for i in items]
    return "\n".join(out) + "\n"


# ---------- HTTP API for Docket.app ----------

def serve(docket: Docket):
    token = secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            if self.headers.get("X-Docket-Token") == token:
                return True
            self.reply(401, {"error": "bad token"})
            return False

        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/api/state":
                items = ledger.snapshot(docket.db())
                return self.reply(200, {"locale": docket.locale, "awaiting": sum(1 for d in items if d["state"] == "awaiting"), "delegations": items})
            if self.path == "/api/settings":
                docket.reload_settings()
                return self.reply(200, docket.settings_view())
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            parts = self.path.strip("/").split("/")
            try:
                body = self.body()
                if parts == ["api", "settings"]:
                    return self.reply(200, docket.save_settings(body))
                if parts == ["api", "dispatch"]:
                    title = (body.get("title") or "").strip()
                    if not title or not (body.get("instruction") or body.get("brief")):
                        return self.reply(400, {"error": "title and instruction are required"})
                    start = docket.dispatch_opencode if (body.get("engine") or "opencode") == "opencode" else (
                        lambda *a: docket.dispatch_cli(body.get("engine"), *a))
                    try:
                        handoff = start(title, body.get("directory") or str(Path.home()),
                                        body.get("instruction") or "", body.get("brief") or "", body.get("model"))
                    except LookupError as error:  # desk-dispatch starts it with opencode-dispatch instead
                        return self.reply(409, {"error": str(error), "fallback": "opencode-dispatch"})
                    return self.reply(202, {"ok": True, "id": handoff})
                if parts[:2] == ["api", "cards"] and len(parts) == 4 and parts[3] == "decide":
                    choices = body.get("choices") or {}
                    reply = (body.get("reply") or "").strip()
                    if not choices and not reply:
                        return self.reply(400, {"error": docket.t("err_empty")})
                    docket.decide(int(parts[2]), choices, reply)
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "close":
                    docket.close(parts[2], (body.get("reply") or "").strip())
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "takeover":
                    docket.takeover(parts[2])
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "handback":
                    return self.reply(202, {"ok": True, "state": docket.handback(parts[2])})
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "note":
                    source = (body.get("source") or "panel").strip()[:80]
                    return self.reply(202, {"ok": True, **docket.note(parts[2], body.get("text") or "", source)})
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "retry":
                    return self.reply(202, {"ok": True, "model": docket.retry(parts[2])})
                elif parts[:2] == ["api", "cards"] and len(parts) == 4 and parts[3] == "replay":
                    docket.replay(int(parts[2]))
                elif parts[:2] == ["api", "delegations"] and len(parts) == 4 and parts[3] == "rewrite":
                    docket.rewrite(parts[2])
                else:
                    return self.reply(404, {"error": "not found"})
            except ValueError as error:
                return self.reply(409, {"error": str(error)})
            except Exception as error:
                log.exception("request failed")
                return self.reply(500, {"error": str(error)})
            self.reply(202, {"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    DISCOVERY.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "token": token, "pid": os.getpid()}), encoding="utf-8")
    DISCOVERY.chmod(0o600)
    log.info("serving on %s", server.server_port)
    server.serve_forever()


def single_instance():
    """Only one daemon per data directory (audit C11): a second one would send every instruction twice.
    The lock goes away with the process, so a crashed daemon never blocks the next start."""
    import fcntl

    ledger.HOME.mkdir(parents=True, exist_ok=True)
    handle = open(ledger.HOME / "daemon.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning("another docket is running on %s: this one stops", ledger.HOME)
        print("another docket is running", file=sys.stderr)
        sys.exit(0)
    return handle  # held open for the process's life


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    lock = single_instance()  # noqa: F841: held until the process exits
    docket = Docket()
    docket.resume_jobs()
    docket.reconcile()
    threading.Thread(target=docket.watch_events, name="events", daemon=True).start()
    threading.Thread(target=docket.watch_mentions, name="mentions", daemon=True).start()
    threading.Thread(target=docket.watch_runs, name="runs", daemon=True).start()
    threading.Thread(target=docket.sweep, name="sweep", daemon=True).start()
    serve(docket)


if __name__ == "__main__":
    main()
