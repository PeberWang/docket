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
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config as docket_config  # noqa: E402
from docket import dispatches, engines, i18n, ledger, pm, registry  # noqa: E402
from docket.opencode import Service  # noqa: E402
from judge import jev, relevance  # noqa: E402
from judge.transcript import connect as openclaw_connect  # noqa: E402
from judge.transcript import messages as transcript_messages  # noqa: E402

DISCOVERY = ledger.HOME / "daemon.json"
MEMORY_DIR = Path.home() / ".openclaw/workspace/memory"
MENTION_POLL = 5
LIVE = ("running", "pending", "awaiting")
# OpenCode permission actions and tool names, sorted by whether they can change files.
READ_ACTIONS = {"read", "glob", "grep", "list"}
WRITE_ACTIONS = {"edit", "write", "patch", "apply_patch", "multiedit"}

log = logging.getLogger("docket")


class Docket:
    def __init__(self):
        self.settings = docket_config.load()
        self.locale = i18n.resolve(self.settings.get("locale"))
        self.t = i18n.Strings(self.locale)
        self.judging = jev.available()
        if not self.judging:
            log.warning("no TypeSafe key: the judge is off (no background picking, no main-thread attribution)")
        self.local = threading.local()
        self.workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="pm")
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()
        self.jobs: dict[str, int] = {}  # PM jobs submitted and not yet finished, per handoff
        pm.configure(self.locale, self.settings.get("pm_model"), self.settings.get("pm_fallback_model"))
        db = self.db()
        if not db.execute("select value from kv where key = 'installed_at'").fetchone():
            db.execute("insert into kv values ('installed_at', ?)", (str(ledger.now()),))

    # ---------- plumbing ----------

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
        with self.locks_guard:
            self.jobs[delegation_id] = self.jobs.get(delegation_id, 0) + 1

        def run():
            try:
                self.run_job(delegation_id, job, args)
            finally:
                with self.locks_guard:
                    self.jobs[delegation_id] -= 1

        return self.workers.submit(run)

    def run_job(self, delegation_id: str, job, args):
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

    def ask_pm(self, row: dict, message: str, validate):
        """pm.ask, with every unusable reply kept verbatim in the events for later."""
        def record(attempt, model, raw, errors):
            ledger.log(self.db(), row["id"], "pm:invalid", {"attempt": attempt, "model": model, "errors": errors, "raw": raw[:8000]})

        return pm.ask(row["pm_key"], message, validate, record=record)

    def show_card(self, delegation_id: str, kind: str, body: dict):
        """Queue the card if one is already waiting; the panel never swaps an unanswered card.

        Once the user has spent every round no card gets through but the limit card,
        whatever the PM's prompt says. Returns None when the card was refused.
        """
        row = ledger.get(self.db(), delegation_id)
        # The one exception: confirming a mention's relay, a round the mention already paid for.
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
        ledger.set_state(self.db(), delegation_id, "awaiting")
        return card_id

    def advance(self, delegation_id: str):
        """After a card is answered: show the next queued one, or go back to running."""
        nxt = ledger.next_queued(self.db(), delegation_id)
        ledger.set_state(self.db(), delegation_id, "awaiting" if nxt else "running")

    # Jobs a failure card can run again: their arguments are plain strings, kept in the card.
    RETRYABLE = ("write_card", "act", "wrap_up", "relay")

    def fail_card(self, delegation_id: str, message: str, job=None, args=(), error: str | None = None):
        """The docket's own failure: a card that stays open with a retry. It never closes the handoff."""
        db = self.db()
        row = ledger.get(db, delegation_id)
        if not row:
            return
        body = {"progress": message[:300], "decisions": [], "decided_for_you": [], "docket_fault": True}
        name = getattr(job, "__name__", None)
        if name in self.RETRYABLE and getattr(job, "__self__", None) is self:
            body["pm_retry"] = {"job": name, "args": list(args)}
            body["decisions"] = [{
                "id": "retry",
                "question": self.t("pm_retry_question"),
                "options": [{"id": "yes", "label": self.t("pm_retry"), "recommended": True}],
            }]
        ledger.log(db, delegation_id, "docket:failed", {"error": (error or message)[:500], "job": name})
        if row["rounds"] >= ledger.ROUND_LIMIT:
            # The budget is spent, but the docket's own fault must still be seen and retried:
            # it takes the limit card's place, and the retry puts the limit card back.
            db.execute(
                "update cards set resolved_at = ?, resolution = ? where delegation_id = ? and resolved_at is null and kind = 'limit'",
                (ledger.now(), json.dumps({"action": "superseded", "note": "docket failure"}), delegation_id),
            )
            ledger.add_card(db, delegation_id, "failure", body)
            ledger.set_state(db, delegation_id, "awaiting")
            return
        self.show_card(delegation_id, "failure", body)

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

    def attach_context(self, handoff_id: str, title: str, instruction: str, brief: str):
        """Find the dispatch in the watched chat and let the judge pick the background."""
        dispatch = None
        for _ in range(6):
            dispatch = registry.find_dispatch(title, ledger.now(), self.settings)
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
        """
        while True:
            try:
                self.sweep_orphans()
            except Exception:
                log.exception("orphan sweep failed")
            try:
                for row in self.db().execute("select * from delegations where state = 'running' and engine = 'opencode'").fetchall():
                    try:
                        session = Service().session(row["id"])
                    except Exception as error:
                        if "404" in str(error) or "NotFound" in str(error):
                            log.info("sweep: session %s is gone", row["id"])
                            # A technical fault never closes a handoff: the user closes or takes over.
                            self.fail_card(row["id"], self.t("session_gone"), error="session not found")
                        continue
                    times = session.get("time") or {}
                    busy = (times.get("busy") or 0) > (times.get("idle") or 0)
                    idle = times.get("idle") or 0
                    if busy or not idle or idle < row["updated_at"] - 60_000:
                        continue
                    log.info("sweep: %s ended without an event (%s)", row["id"], session.get("outcome"))
                    ledger.log(self.db(), row["id"], "sweep", {"outcome": session.get("outcome")})
                    self.on_run_end(row["id"], session.get("outcome") or "succeeded")
            except Exception:
                log.exception("sweep failed")
            time.sleep(60)

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

    def maybe_register(self, session_id: str, attempts: int = 6):
        db = self.db()
        if ledger.get(db, session_id):
            return
        try:
            session = Service().session(session_id)
        except Exception:
            log.warning("cannot read session %s", session_id)
            return
        title = session.get("title") or ""
        prefix = self.settings["prefix"]
        if not title.startswith(prefix.strip()):
            return
        name = title[len(prefix.strip()) :].strip()
        created = (session.get("time") or {}).get("created") or ledger.now()
        dispatch = None
        for attempt in range(attempts):  # the transcript write can lag the session by a moment
            dispatch = registry.find_dispatch(name, created, self.settings)
            if dispatch:
                break
            time.sleep(5)
        if not dispatch:
            log.info("not a watched dispatch, skipping: %s", title)
            ledger.log(db, None, "skipped", {"session": session_id, "title": title})
            return
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
        log.info("registered %s (%s) with %d context messages", name, session_id, len(context))

    # ---------- OpenCode finished a run ----------

    def on_run_end(self, session_id: str, event: str, reply: str | None = None):
        row = self.delegation(session_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        for stale in self.db().execute(
            "select id, body from cards where delegation_id = ? and resolved_at is null", (session_id,)
        ).fetchall():
            if json.loads(stale["body"]).get("pending_permission"):  # the run is over; it can no longer be answered
                ledger.resolve_card(self.db(), stale["id"], {"action": "expired"})
                ledger.log(self.db(), session_id, "permission:expired", {"card": stale["id"]})
        ledger.set_state(self.db(), session_id, "pending")
        self.submit(session_id, self.write_card, session_id, event, reply)

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
        ledger.set_state(self.db(), delegation_id, "running")
        self.engine(row).resume(row, message, fallback)
        return True

    def write_card(self, session_id: str, event: str, reply: str | None = None):
        row = self.delegation(session_id)
        if not row or row["state"] in ("closed", "taken_over"):
            return
        if reply is None:
            reply = self.engine(row).last_reply(row)
        if row["state"] == "limit":  # the budget is spent: a handoff note, never another question
            return self.wrap_up(session_id, event, reply)
        if event in ("failed", "interrupted") and self.retry_with_fallback(session_id, reply):
            return
        first = not self.db().execute("select 1 from cards where delegation_id = ? limit 1", (session_id,)).fetchone()
        if first:
            message = pm.first_wake(row, json.loads(row["context"] or "[]"), event, reply)
        else:
            message = pm.later_wake(event, reply, row["rounds"])
        card = self.ask_pm(row, message, pm.validate_card)
        self.show_card(session_id, card.get("kind", "report"), card)

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
        if body.get("pm_retry"):  # the docket's own failure: running it again is not a round
            if not replay:
                ledger.resolve_card(db, card_id, {**resolution, "action": "retry"})
            ledger.log(db, row["id"], "docket:retry", {"job": body["pm_retry"]["job"], "card": card_id})
            ledger.set_state(db, row["id"], "pending")
            self.submit(row["id"], getattr(self, body["pm_retry"]["job"]), *body["pm_retry"]["args"])
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
                self.check_limit(row["id"]) or self.advance(row["id"])
                raise ValueError(self.t("permission_expired")) from error
            self.check_limit(row["id"]) or self.advance(row["id"])
            return
        if body.get("pending_relay"):  # confirming a relay proposed from a chat mention
            if choices.get("relay") == "yes":
                ledger.set_state(db, row["id"], "pending")
                self.submit(row["id"], self.relay, row["id"], body["pending_relay"] + (f"\n\nThe user added: {reply}" if reply else ""))
            else:
                previous = db.execute("select * from cards where delegation_id = ? and id < ? order by id desc limit 1", (row["id"], card_id)).fetchone()
                ledger.set_state(db, row["id"], "awaiting" if previous and previous["resolved_at"] is None else "running")
                self.check_limit(row["id"])
            return
        ledger.set_state(db, row["id"], "pending")
        self.submit(row["id"], self.act, row["id"], pm.decision_wake(body, choices, reply, rounds))

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

    def act(self, delegation_id: str, message: str, confirm: str | None = None):
        """Wake the PM and carry out its action.

        `confirm` holds the chat text when the trigger was a main-thread mention: a relay
        then becomes a card asking the user to confirm, because the judge can be wrong.
        The mention already cost its round, so the confirmation does not cost another.
        """
        row = self.delegation(delegation_id)
        action = self.ask_pm(row, message, pm.validate_action)
        kind = action["action"]
        if kind == "relay" and confirm is not None:
            t = self.t
            card = {
                "kind": "question",
                "progress": t("confirm_progress", text=confirm[:100]),
                "decisions": [
                    {
                        "id": "relay",
                        "question": t("confirm_question"),
                        "options": [
                            {"id": "yes", "label": t("confirm_yes"), "why": t("confirm_yes_why"), "recommended": True},
                            {"id": "no", "label": t("confirm_no"), "why": "", "recommended": False},
                        ],
                    }
                ],
                "decided_for_you": [t("confirm_draft", text=action["message"][:100])],
                "pending_relay": action["message"],
                "round_spent": True,
            }
            self.show_card(delegation_id, "question", card)
            return
        if kind == "relay":
            self.relay(delegation_id, action["message"])
        elif kind == "card":
            self.show_card(delegation_id, action["card"].get("kind", "question"), action["card"])
        else:
            self.finish(delegation_id, action["conclusion"], action.get("remaining"))
        self.check_limit(delegation_id)

    def relayed_count(self, delegation_id: str) -> int:
        return self.db().execute(
            "select count(*) from events where delegation_id = ? and kind = 'relayed' and coalesce(detail, '') not like '%\"via\": \"fallback\"%'",
            (delegation_id,),
        ).fetchone()[0]

    def relay(self, delegation_id: str, message: str, model: str | None = None):
        """Hand the user's decision to the coding agent. Rounds were spent when the user acted;
        the second gate here only stops more relays than the budget has rounds."""
        row = self.delegation(delegation_id)
        if self.relayed_count(delegation_id) >= ledger.ROUND_LIMIT or row["rounds"] > ledger.ROUND_LIMIT:
            ledger.log(self.db(), delegation_id, "relay:blocked", {"message": message[:200]})
            self.enter_limit(delegation_id)
            return
        engine = self.engine(row)
        if isinstance(engine, engines.Cli):
            self.launch(delegation_id, message, first=False)
        else:
            engine.resume(row, message, model)
        ledger.set_state(self.db(), delegation_id, "running")
        ledger.log(self.db(), delegation_id, "relayed", {"message": message[:500]})
        if not ledger.open_card(self.db(), delegation_id) and ledger.next_queued(self.db(), delegation_id):
            ledger.set_state(self.db(), delegation_id, "awaiting")  # a card was waiting behind the one just answered
        self.check_limit(delegation_id)

    def retry(self, delegation_id: str):
        """Send the last relayed instruction again, on the fallback model."""
        row = self.delegation(delegation_id)
        fallback = self.settings.get("fallback_model")
        last = self.db().execute(
            "select detail from events where delegation_id = ? and kind = 'relayed' order by id desc limit 1", (delegation_id,)
        ).fetchone()
        if not row or not fallback or not last or not last[0]:
            raise ValueError(self.t("err_missing"))
        if ledger.open_card(self.db(), delegation_id):
            raise ValueError(self.t("err_open_card"))
        message = json.loads(last[0])["message"]
        self.engine(row).resume(row, message, fallback)
        ledger.set_state(self.db(), delegation_id, "running")
        ledger.log(self.db(), delegation_id, "relayed", {"message": message, "via": "fallback", "model": fallback})
        return fallback

    def close(self, delegation_id: str, reply: str):
        row = self.delegation(delegation_id)
        if not row or row["state"] == "closed":
            raise ValueError(self.t("err_closed"))
        card = ledger.open_card(self.db(), delegation_id)
        if card:
            ledger.resolve_card(self.db(), card["id"], {"action": "close", "reply": reply})
        at_limit = row["state"] == "limit"
        note = json.loads(card["body"]).get("wrap_up") if card and card["kind"] == "limit" else None
        ledger.set_state(self.db(), delegation_id, "pending")

        def job():
            if note and not reply:  # the PM already wrote the handoff note when the budget ran out
                return self.finish(delegation_id, note["conclusion"], note.get("remaining"))
            if at_limit:
                action = self.ask_pm(row, pm.wrap_up_wake("closed", None, row["rounds"]) + (f"\n\nThe user added: {reply}" if reply else ""), pm.validate_wrap_up)
            else:
                action = self.ask_pm(row, pm.close_wake(reply), pm.validate_action)
            conclusion = action.get("conclusion") if action["action"] == "close" else self.t("closed_fallback")
            self.finish(delegation_id, conclusion, action.get("remaining"))

        self.submit(delegation_id, job)

    def conclusion_text(self, conclusion: str, remaining: list | None) -> str:
        items = [r.strip() for r in remaining or [] if r and r.strip()]
        if not items:
            return conclusion
        separator = "；" if self.locale == "zh" else "; "
        return f"{conclusion} {self.t('remaining', items=separator.join(items))}"

    def finish(self, delegation_id: str, conclusion: str, remaining: list | None = None):
        """The only way a handoff closes: the PM judged it done, the user closed it, or the budget ran out."""
        row = self.delegation(delegation_id)
        conclusion = self.conclusion_text(conclusion, remaining)
        for card in self.db().execute("select id from cards where delegation_id = ? and resolved_at is null", (delegation_id,)).fetchall():
            ledger.resolve_card(self.db(), card["id"], {"action": "closed"})
        ledger.add_card(self.db(), delegation_id, "closed", {"progress": conclusion, "conclusion": conclusion, "remaining": remaining or [], "decisions": [], "decided_for_you": []})
        ledger.set_state(self.db(), delegation_id, "closed")
        self.write_memory(row, conclusion)

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
        self.on_run_end(delegation_id, event, reply)

    def takeover(self, delegation_id: str):
        row = self.delegation(delegation_id)
        if not row:
            raise ValueError(self.t("err_missing"))
        card = ledger.open_card(self.db(), delegation_id)
        if card:
            ledger.resolve_card(self.db(), card["id"], {"action": "takeover"})
        if row["state"] != "closed":
            ledger.set_state(self.db(), delegation_id, "taken_over")
        self.engine(row).takeover(row)

    # ---------- main-thread mentions ----------

    def watch_mentions(self):
        db = self.db()
        while True:
            try:
                if self.settings["attribute"] and self.judging:
                    self.scan_mentions(db)
            except Exception:
                log.exception("mention scan failed")
            time.sleep(MENTION_POLL)

    def scan_mentions(self, db):
        live = [dict(r) for r in db.execute(f"select * from delegations where state in {LIVE}").fetchall()]
        conn = openclaw_connect()
        try:
            sessions = [
                (key, sid)
                for key, sid in conn.execute("select session_key, session_id from session_windows").fetchall()
                if docket_config.watched(self.settings, key)
            ]
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
                rows = conn.execute(
                    "select seq from transcript_events where session_id = ? and seq > ? and json_extract(event_json, '$.message.role') = 'user'",
                    (session_id, since_seq),
                ).fetchall()
                db.execute("insert or replace into kv values (?, ?)", (cursor_key, str(latest)))
                if not rows or not live:
                    continue
                history = transcript_messages(conn, session_id, since=ledger.now() - 86400_000)
                by_seq = {m.seq: m for m in history}
                for (seq,) in rows:
                    message = by_seq.get(seq)
                    if message and message.role == "user":
                        self.attribute(message, [m for m in history if m.seq < seq][-3:], live)
        finally:
            conn.close()

    def attribute(self, message, recent, live):
        def view(m):
            return {"id": f"m{m.seq}", "role": m.role, "time": dt.datetime.fromtimestamp(m.time / 1000).strftime("%m-%d %H:%M"), "text": m.text}

        active = []
        for row in live:
            if row["dispatched_at"] >= message.time:
                continue
            rounds = [e["detail"] for e in self.db().execute("select detail from events where delegation_id = ? and kind = 'relayed' order by id", (row["id"],))]
            active.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "instruction": "",
                    "brief": row["brief"] or "",
                    "rounds": [json.loads(r)["message"] for r in rounds if r],
                }
            )
        if not active:
            return
        answer, _ = relevance.attribute(view(message), [view(m) for m in recent], active)
        ledger.log(self.db(), answer.get("choice"), "attributed", {"seq": message.seq, **{k: answer[k] for k in ("choice", "belongs", "new_work")}})
        if not relevance.should_move(answer, self.settings["move_threshold"]):
            return
        delegation_id = answer["choice"]
        row = self.delegation(delegation_id)
        log.info("main-thread message m%s belongs to %s", message.seq, row["title"])
        if row["rounds"] >= ledger.ROUND_LIMIT:
            ledger.log(self.db(), delegation_id, "mention:blocked", {"seq": message.seq})
            self.enter_limit(delegation_id)
            return
        card = ledger.open_card(self.db(), delegation_id)
        # Writing about the handoff in the chat is taking part: one round, with the card it answers.
        rounds = ledger.spend_round(self.db(), delegation_id, card["id"] if card else None, {"reply": message.text, "via": "chat"})
        if rounds is None:  # the card was answered on the panel in the same moment
            rounds = ledger.spend_round(self.db(), delegation_id)
        ledger.set_state(self.db(), delegation_id, "pending")
        self.submit(delegation_id, self.act, delegation_id, pm.mention_wake(message.text, rounds), message.text)

    # ---------- OpenCode events ----------

    def watch_events(self):
        while True:
            try:
                for event in Service().events():
                    self.handle(event)
            except Exception as error:
                log.debug("event stream dropped: %s", error)
            time.sleep(3)

    def handle(self, event: dict):
        kind = event.get("type", "")
        data = event.get("data") or {}
        # Most events carry data.sessionID; form and permission events may nest it one level down.
        nested = next((v for v in data.values() if isinstance(v, dict) and "sessionID" in v), {})
        session_id = data.get("sessionID") or nested.get("sessionID")
        if not session_id:
            return
        if kind in ("session.created", "session.renamed"):
            threading.Thread(target=self.maybe_register, args=(session_id,), daemon=True).start()
        elif kind.startswith("session.execution.") and kind != "session.execution.started":
            self.on_run_end(session_id, kind.rsplit(".", 1)[-1])
        elif kind == "session.execution.started":
            row = self.delegation(session_id)
            if row and row["state"] in ("pending", "awaiting") and not ledger.open_card(self.db(), session_id):
                ledger.set_state(self.db(), session_id, "running")
        elif kind == "form.created":
            self.on_run_end(session_id, "question")
        elif kind == "permission.asked":
            self.permission_card(session_id, data if str(data.get("id", "")).startswith("per_") else nested)

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
        tier, reason = self.permission_tier(session_id, row, request) if "--auto" in (self.engine(row).args or []) else ("ask", "not --auto")
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
                session = service.session(row["id"])
            except Exception:
                continue
            times = session.get("time") or {}
            busy = (times.get("busy") or 0) > (times.get("idle") or 0)
            if busy:
                if row["state"] == "pending":
                    ledger.set_state(self.db(), row["id"], "running")
            elif session.get("outcome") and (row["state"] == "pending" or (times.get("idle") or 0) > row["updated_at"]):
                self.on_run_end(row["id"], session["outcome"])


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
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            parts = self.path.strip("/").split("/")
            try:
                body = self.body()
                if parts == ["api", "dispatch"]:
                    title = (body.get("title") or "").strip()
                    if not title or not (body.get("instruction") or body.get("brief")):
                        return self.reply(400, {"error": "title and instruction are required"})
                    handoff = docket.dispatch_cli(
                        body.get("engine") or "", title, body.get("directory") or str(Path.home()),
                        body.get("instruction") or "", body.get("brief") or "", body.get("model"),
                    )
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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    docket = Docket()
    docket.reconcile()
    threading.Thread(target=docket.watch_events, name="events", daemon=True).start()
    threading.Thread(target=docket.watch_mentions, name="mentions", daemon=True).start()
    threading.Thread(target=docket.watch_runs, name="runs", daemon=True).start()
    threading.Thread(target=docket.sweep, name="sweep", daemon=True).start()
    serve(docket)


if __name__ == "__main__":
    main()
