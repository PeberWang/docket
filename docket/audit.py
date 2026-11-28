# -*- coding: utf-8 -*-
"""Read-only audit of a running Docket: what the ledger, the logs and the coding agent say happened.

    docket audit report [--out DIR] [--transcript] [--memory]
    docket audit timeline <handoff id prefix> ... [--since "YYYY-MM-DD HH:MM"]

`report` copies the live ledger with SQLite's backup API (the service keeps running, nothing is
written to it), then prints one Markdown report: how handoffs ended, where cards went, who took part,
registration, PM wakes and failures, who sent instructions into handoff sessions (Docket or someone
else, from OpenCode's own log), the watchdog, the ledger invariants that must hold at every moment,
the service log, and what the OpenClaw plugin and the permission classes did. --transcript also names the OpenClaw session behind each instruction Docket
did not send (reads the transcript database read-only, commands only, never chat text). --memory
checks that every closing reached OpenClaw's daily memory.

Paths follow the service: $DOCKET_HOME (default ~/Library/Application Support/docket),
~/Library/Logs/docket/daemon.log, ~/.local/share/opencode/log/opencode.log, ~/.openclaw/workspace/memory.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

HOME = Path(os.environ.get("DOCKET_HOME", Path.home() / "Library/Application Support/docket"))
LEDGER = HOME / "docket.sqlite"
DAEMON_LOG = Path.home() / "Library/Logs/docket/daemon.log"
OPENCODE_LOG = Path.home() / ".local/share/opencode/log/opencode.log"
MEMORY = Path.home() / ".openclaw/workspace/memory"
ROUND_LIMIT = 3
PHASE_OF = {"pending": "verifying", "awaiting": "awaiting_user", "limit": "awaiting_user", "closed": "closed", "running": "running"}


def ts(ms, seconds=False) -> str:
    if not ms:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S" if seconds else "%m-%d %H:%M")


def j(text) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def copy_ledger(dest: Path) -> Path:
    """A consistent copy of the live ledger; the live file is only read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    source.backup(target)
    target.close()
    source.close()
    return dest


def table(rows, head) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in r) + " |" for r in rows]
    return out


class Audit:
    def __init__(self, db: Path):
        self.c = sqlite3.connect(db)
        self.c.row_factory = sqlite3.Row
        self.titles = {r["id"]: r["title"] for r in self.c.execute("select id, title from delegations")}
        self.columns = {r[1] for r in self.c.execute("pragma table_info(delegations)")}
        self.tables = {r[0] for r in self.c.execute("select name from sqlite_master where type = 'table'")}
        self.out: list[str] = []

    def h(self, text):
        self.out += ["", f"## {text}", ""]

    def p(self, *lines):
        self.out += list(lines)

    def events(self, kind_sql: str, args=()):
        return self.c.execute(f"select * from events where {kind_sql} order by id", args).fetchall()

    # ---------- sections ----------

    def overview(self):
        c = self.c
        n = c.execute("select count(*) from delegations").fetchone()[0]
        span = c.execute("select min(dispatched_at), max(dispatched_at) from delegations").fetchone()
        self.h("1. Overview")
        self.p(f"- handoffs: {n}, dispatched {ts(span[0])} → {ts(span[1])}")
        self.p(f"- cards: {c.execute('select count(*) from cards').fetchone()[0]}, events: {c.execute('select count(*) from events').fetchone()[0]}")
        rows = c.execute("select state, engine, count(*) n from delegations group by 1, 2 order by n desc").fetchall()
        self.p("", *table([(r["state"], r["engine"], r["n"]) for r in rows], ("state", "engine", "handoffs")))
        days = c.execute("select date(dispatched_at/1000, 'unixepoch', 'localtime') d, count(*) n from delegations group by 1 order by 1").fetchall()
        self.p("", "Handoffs per day: " + ", ".join(f"{r['d'][5:]} {r['n']}" for r in days))
        rounds = c.execute("select rounds, count(*) n from delegations group by 1 order by 1").fetchall()
        self.p("", "Rounds used per handoff: " + ", ".join(f"{r['rounds']} → {r['n']}" for r in rounds))

    def ending(self, d) -> str:
        kinds = [e["kind"] for e in self.c.execute("select kind from events where delegation_id = ?", (d["id"],))]
        if "sweep:auto-expired" in kinds:
            return "auto-expired: session ended, card unanswered (sweep_awaiting)"
        if "stale:quiet-expired" in kinds:
            return "auto-expired: unanswered for quiet_expire_minutes"
        if "orphaned" in kinds:
            return "closed as overdue on a restart (close_overdue)"
        if "superseded" in kinds:
            return "replaced by a newer handoff (--supersedes)"
        if "limit:expired" in kinds:
            return "closed at the limit, wrap-up unanswered for quiet_expire_minutes"
        close = self.c.execute("select detail from events where delegation_id = ? and kind = 'exec:close' order by id desc limit 1", (d["id"],)).fetchone()
        if close:
            by = j(close["detail"]).get("by")
            return {"user": "closed by the user", "quiet-wake": "closed by its PM after a quiet wake"}.get(by, "closed by its PM")
        actions = [j(r["resolution"]).get("action") for r in self.c.execute("select resolution from cards where delegation_id = ? and resolution is not null", (d["id"],))]
        if "close" in actions:
            return "closed by the user (older build, no exec event)"
        if "takeover" in actions:
            return "taken over, then closed"
        if d["state"] == "closed":
            return "closed, path not recorded (older build)"
        return d["state"]

    def endings(self):
        self.h("2. How handoffs ended")
        counts = collections.Counter(self.ending(d) for d in self.c.execute("select * from delegations where state = 'closed'"))
        self.p(*table(counts.most_common(), ("ending", "handoffs")))
        recent = self.c.execute("select * from delegations where state = 'closed' order by updated_at desc limit 15").fetchall()
        self.p("", "Latest closings:", "", *table([(ts(d["updated_at"]), d["title"][:30], self.ending(d)) for d in recent], ("closed", "handoff", "ending")))

    def cards(self):
        self.h("3. Where cards went")
        counts = collections.Counter()
        for r in self.c.execute("select kind, resolution from cards"):
            res = j(r["resolution"]) if r["resolution"] else None
            if res is None:
                how = "still open"
            elif res.get("via") in ("chat", "note"):
                how = f"answered via {res['via']}"
            elif ("choices" in res or res.get("reply")) and not res.get("action"):
                how = "answered on the panel"
            else:
                how = (res.get("action") or "?") + (f" by {res['by']}" if res.get("by") else "") + (f" ({res['note']})" if res.get("note") else "")
            counts[(r["kind"], how)] += 1
        self.p(*table([(k, h, n) for (k, h), n in counts.most_common()], ("kind", "resolution", "cards")))
        waits = []
        for r in self.c.execute("select created_at, shown_at, resolved_at, resolution from cards where resolved_at is not null and kind not in ('closed', 'limit')"):
            res = j(r["resolution"])
            if ("choices" in res or res.get("reply")) and not res.get("action"):
                waits.append((r["resolved_at"] - (r["shown_at"] or r["created_at"])) / 60_000)
        if waits:
            waits.sort()
            self.p("", f"Time from a card going up to a person answering it (minutes): n={len(waits)}, median {statistics.median(waits):.0f}, "
                   f"p90 {waits[int(len(waits) * 0.9) - 1]:.0f}, max {waits[-1]:.0f}")

    def participation(self):
        self.h("4. Who took part (rounds)")
        src = collections.Counter()
        for e in self.events("kind = 'round'"):
            card = j(e["detail"]).get("card")
            res = {}
            if card:
                row = self.c.execute("select resolution from cards where id = ?", (card,)).fetchone()
                res = j(row["resolution"]) if row else {}
            src[res.get("via") or ("panel" if card else "chat or note, no card open")] += 1
        self.p(*table(src.most_common(), ("round spent through", "rounds")))
        fast = []
        for d in self.c.execute("select * from delegations where rounds >= ?", (ROUND_LIMIT,)):
            rounds = [e["at"] for e in self.c.execute("select at from events where delegation_id = ? and kind = 'round' order by id", (d["id"],))]
            if rounds:
                fast.append((d["title"][:30], ts(d["dispatched_at"]), f"{(rounds[-1] - d['dispatched_at']) / 60_000:.0f}"))
        if fast:
            self.p("", "Handoffs that spent the whole budget, and how fast:", "", *table(fast, ("handoff", "dispatched", "minutes to the last round")))
        blocked = collections.Counter(e["kind"] for e in self.events("kind in ('mention:blocked', 'card:blocked', 'relay:blocked', 'stale:blocked')"))
        if blocked:
            self.p("", "Refused at the limit: " + ", ".join(f"{k} {n}" for k, n in blocked.items()))

    def registration(self):
        self.h("5. Registration")
        dup = self.c.execute("select delegation_id, count(*) n from events where kind = 'registered' group by 1 having n > 1").fetchall()
        total = self.c.execute("select count(*) from delegations").fetchone()[0]
        self.p(f"- registered more than once (two threads racing on session.created + session.renamed): {len(dup)} of {total}")
        if "turns" in self.tables:
            twins = self.c.execute("select count(*) from turns where outcome = 'superseded' and via = 'dispatch'").fetchone()[0]
            self.p(f"- dispatch turns superseded at once by a twin registration: {twins}")
        reasons, titles = collections.Counter(), collections.Counter()
        for e in self.events("kind = 'skipped'"):
            d = j(e["detail"])
            reasons[d.get("reason") or "(no reason recorded)"] += 1
            titles[d.get("title") or ""] += 1
        self.p(f"- sessions looked at and skipped: {sum(reasons.values())} events, {len(titles)} distinct titles")
        self.p("", *table(reasons.most_common(), ("skip reason", "events")))
        marked = [(t, n) for t, n in titles.most_common() if t and not t.startswith(("Fork", "Review", "审"))]
        if marked:
            self.p("", "Skipped titles (check: were any of these real handoffs?):", "", *table(marked[:40], ("title", "times")))

    def pm(self):
        self.h("6. PM wakes")
        wakes = [(j(e["detail"]), e["delegation_id"]) for e in self.events("kind = 'pm:wake'")]
        if wakes:
            secs = sorted((w["end"] - w["start"]) / 1000 for w, _ in wakes if w.get("end") and w.get("start"))
            self.p(f"- wakes recorded: {len(secs)}; seconds median {statistics.median(secs):.0f}, p90 {secs[int(len(secs) * 0.9) - 1]:.0f}, max {secs[-1]:.0f}")
            long = [(ts(w["start"], True), self.titles.get(d, d)[:30], f"{(w['end'] - w['start']) / 60_000:.1f}") for w, d in wakes if w.get("end") and w["end"] - w["start"] > 300_000]
            if long:
                self.p("", "Wakes over 5 minutes (a PM doing work itself instead of answering):", "", *table(long, ("start", "handoff", "minutes")))
        else:
            self.p("- no pm:wake events (older build)")
        errors = collections.Counter()
        for e in self.events("kind = 'pm:invalid'"):
            for err in j(e["detail"]).get("errors") or []:
                key = ("jargon: " + err.split(" contains ")[1].split(":")[0][:60]) if " contains " in err else err.split(":")[0][:80]
                errors[key] += 1
        if errors:
            self.p("", "Rejected PM replies, by reason:", "", *table(errors.most_common(25), ("reason", "replies")))
        failed = self.events("kind = 'docket:failed'")
        self.p("", f"- failure cards the docket put up itself: {len(failed)}")
        for e in failed[-10:]:
            self.p(f"  - {ts(e['at'])} {self.titles.get(e['delegation_id'], e['delegation_id'])[:30]}: {(j(e['detail']).get('error') or '')[:160]}")
        # Run ends that woke a PM while one was already queued for the same handoff.
        dupes = []
        for d in self.c.execute("select id from delegations"):
            cards = self.c.execute("select id, created_at, body from cards where delegation_id = ? and kind in ('report', 'question', 'failure') order by id", (d["id"],)).fetchall()
            for a, b in zip(cards, cards[1:]):
                if b["created_at"] - a["created_at"] < 60_000 and j(a["body"]).get("progress") and not j(b["body"]).get("pending_relay") and not j(b["body"]).get("pending_permission"):
                    dupes.append((self.titles.get(d["id"], "")[:30], a["id"], b["id"], ts(b["created_at"], True)))
        if dupes:
            self.p("", "Cards written less than a minute apart on one handoff (duplicate PM jobs):", "", *table(dupes, ("handoff", "card", "next card", "at")))

    def writers(self, transcript: bool):
        self.h("7. Who sent instructions into handoff sessions")
        if not OPENCODE_LOG.exists():
            self.p(f"- no OpenCode log at {OPENCODE_LOG}")
            return
        sessions = {}
        for r in self.c.execute("select id, engine_session from delegations where engine = 'opencode'"):
            sessions[r["id"]] = r["id"]
            if r["engine_session"]:
                sessions[r["engine_session"]] = r["id"]
        first = self.c.execute("select min(dispatched_at) from delegations").fetchone()[0] or 0
        ours = collections.defaultdict(list)
        for e in self.events("kind in ('relayed', 'turn:sent', 'turn:send-failed')"):
            ours[e["delegation_id"]].append(e["at"])
        line_re = re.compile(r'^timestamp=(\S+) .*message="cli starting".*args="(\[.*?\])" role=cli')
        follow = []
        with OPENCODE_LOG.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"cli starting"' not in line or '\\"run\\"' not in line:
                    continue
                m = line_re.search(line)
                if not m:
                    continue
                when = dt.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
                ms = int(when.timestamp() * 1000)
                if ms < first:
                    continue
                try:
                    args = json.loads(m.group(2).encode().decode("unicode_escape").encode("latin-1").decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if args[:1] == ["run"] and "-s" in args:
                    sid = args[args.index("-s") + 1]
                    hid = sessions.get(sid)
                    if hid:
                        mine = any(abs(ms - at) < 15_000 for at in ours.get(hid, []))
                        follow.append((ms, hid, "Docket" if mine else "someone else", args[-1]))
        senders = collections.Counter(f[2] for f in follow)
        self.p(f"- follow-ups into handoff sessions: {len(follow)}; " + ", ".join(f"{k} {n}" for k, n in senders.items()))
        foreign = [f for f in follow if f[2] == "someone else"]
        names = {}
        if transcript and foreign:
            names = self.name_senders([f[0] for f in foreign], set(sessions))
        rows = [(ts(ms, True), self.titles.get(hid, "")[:28], names.get(ms, "-"), " ".join(text.split())[:60]) for ms, hid, _, text in foreign]
        if rows:
            self.p("", "Not sent by Docket:", "", *table(rows, ("at", "handoff", "OpenClaw session", "instruction (head)")))
            if names:
                by = collections.Counter(("a PM session" if ":docket-" in n or ":desk-" in n else n) for n in names.values())
                self.p("", "Senders: " + ", ".join(f"{k} {n}" for k, n in by.most_common()))

    def name_senders(self, times: list[int], session_ids: set) -> dict:
        """The OpenClaw session whose tool call sent each instruction: the nearest opencode call before it."""
        from judge import transcript as tr
        try:
            conn = tr.connect()
        except Exception as error:  # the report goes on without names
            self.p(f"- transcript not readable: {error}")
            return {}
        keys = tr.session_keys(conn)
        out = {}
        for at in times:
            best = None
            for session_id, created, raw in conn.execute(
                "select session_id, created_at, event_json from transcript_events where created_at between ? and ? "
                "and (event_json like '%opencode%' or event_json like '%dispatch%') order by created_at",
                (at - 180_000, at + 5_000),
            ):
                try:
                    role, _, calls = tr.normalize(json.loads(raw))
                except (ValueError, AttributeError, TypeError):
                    continue
                for call in calls or []:
                    command = call.get("command") or ""
                    if " -s " in command and any(s in command for s in session_ids):
                        best = keys.get(session_id, session_id)
            if best:
                out[at] = best
        conn.close()
        return out

    def watchdog(self):
        self.h("8. Watchdog")
        if "turns" not in self.tables:
            self.p("- the ledger has no turns table yet (service older than the watchdog)")
            return
        rows = self.c.execute("select via, attempt, outcome, count(*) n from turns group by 1, 2, 3 order by n desc").fetchall()
        self.p(*table([(r["via"], r["attempt"], r["outcome"] or "(open)", r["n"]) for r in rows], ("via", "attempt", "outcome", "turns")))
        kinds = collections.Counter(e["kind"] for e in self.events("kind like 'watchdog:%' or kind like 'orphan:%' or kind like 'turn:%'"))
        self.p("", "Events: " + (", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) or "none"))
        late = self.c.execute("select avg(confirmed_at - started_at), max(confirmed_at - started_at) from turns where confirmed_at is not null").fetchone()
        if late[0]:
            self.p(f"- time from handing an instruction over to seeing it run: mean {late[0] / 1000:.0f} s, max {late[1] / 1000:.0f} s")

    def invariants(self):
        """What must hold at every moment. Each violation is something the panel shows wrongly now."""
        self.h("9. Ledger invariants (now)")
        c, bad = self.c, []
        for d in c.execute("select * from delegations"):
            open_cards = c.execute("select * from cards where delegation_id = ? and resolved_at is null", (d["id"],)).fetchall()
            shown = [x for x in open_cards if not x["queued"]]
            queued = [x for x in open_cards if x["queued"]]
            perm = [x for x in shown if j(x["body"]).get("pending_permission")]
            name = f"{d['title'][:28]} ({d['id'][:16]})"
            if d["state"] == "awaiting" and len(shown) != 1:
                bad.append((name, d["state"], f"awaiting with {len(shown)} cards on show"))
            if d["state"] in ("running", "pending") and len(shown) - len(perm) > 0:
                bad.append((name, d["state"], f"{len(shown) - len(perm)} card(s) on show the panel hides"))
            if d["state"] == "closed" and open_cards:
                bad.append((name, d["state"], f"{len(open_cards)} unanswered card(s) on a closed handoff"))
            if d["state"] == "limit" and any(x["kind"] not in ("limit", "failure") for x in shown):
                bad.append((name, d["state"], "a card other than limit/failure on show at the limit"))
            if queued and not shown:
                bad.append((name, d["state"], f"{len(queued)} queued card(s) with none on show: the queue is stuck"))
            spent = c.execute("select count(*) from events where delegation_id = ? and kind = 'round'", (d["id"],)).fetchone()[0]
            if d["rounds"] > ROUND_LIMIT or (spent and spent != d["rounds"]):
                bad.append((name, d["state"], f"rounds {d['rounds']} but {spent} round events"))
            if "phase" in self.columns and d["phase"] and d["state"] in PHASE_OF and d["phase"] != PHASE_OF[d["state"]] \
                    and d["phase"] not in ("stalled", "dispatching", "awaiting_dispatch") and not (d["state"] == "closed"):
                bad.append((name, d["state"], f"phase {d['phase']} does not follow state {d['state']}"))
            if "turns" in self.tables:
                turns = c.execute("select count(*) from turns where delegation_id = ? and ended_at is null", (d["id"],)).fetchone()[0]
                if turns > 1:
                    bad.append((name, d["state"], f"{turns} turns open at once"))
                if turns and d["state"] in ("closed", "taken_over"):
                    bad.append((name, d["state"], "an open turn on an ended handoff"))
        self.p(*(table(bad, ("handoff", "state", "violation")) if bad else ["- none"]))
        # Written to the ledger by something other than the service: no event behind the state, a clock
        # running backwards, an ending with nothing in it. The ledger is only trustworthy if this stays empty.
        forged = []
        for d in c.execute("select * from delegations"):
            name = f"{d['title'][:28]} ({d['id'][:16]})"
            last = c.execute("select kind from events where delegation_id = ? and kind like 'state:%' order by id desc limit 1", (d["id"],)).fetchone()
            if d["state"] != "running" and (not last or last["kind"] != f"state:{d['state']}"):
                forged.append((name, f"state {d['state']} but the last state event is {last['kind'] if last else 'none'}"))
            if d["updated_at"] < d["dispatched_at"]:
                forged.append((name, f"updated {ts(d['updated_at'])} before dispatched {ts(d['dispatched_at'])}"))
            for card in c.execute("select id, body, resolution from cards where delegation_id = ? and kind = 'closed'", (d["id"],)):
                if not (j(card["body"]).get("conclusion") or j(card["body"]).get("progress")):
                    forged.append((name, f"closed card {card['id']} with no conclusion"))
        others = collections.Counter(j(r["resolution"]).get("by") for r in c.execute("select resolution from cards where resolution like '%\"by\"%'"))
        outsiders = {k: n for k, n in others.items() if k not in (None, "sweep", "quiet-expiry", "quiet-wake", "chat-match")}
        self.p("", "Ledger integrity (writes that did not come from the service):", "")
        self.p(*(table(forged, ("handoff", "finding")) if forged else ["- none"]))
        if outsiders:
            self.p("", "Card resolutions signed by someone other than the service: " + ", ".join(f"{k} {n}" for k, n in outsiders.items()))
        stale_kv = collections.Counter(k.split(":")[0] for (k,) in c.execute("select key from kv"))
        self.p("", "kv rows by family: " + ", ".join(f"{k} {n}" for k, n in stale_kv.most_common()))

    def service_log(self):
        self.h("10. Service log")
        if not DAEMON_LOG.exists():
            self.p(f"- no log at {DAEMON_LOG}")
            return
        levels, kinds, starts = collections.Counter(), collections.Counter(), []
        for line in DAEMON_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) (\S+) (.*)$", line)
            if not m:
                continue
            levels[m.group(2)] += 1
            text = re.sub(r"ses_\w+|\d+", "#", m.group(4))[:90]
            if m.group(2) in ("WARNING", "ERROR"):
                kinds[(m.group(2), text)] += 1
            if "serving on" in m.group(4):
                starts.append(m.group(1))
        self.p(f"- lines by level: {dict(levels)}; service starts: {len(starts)} (latest {starts[-1] if starts else '-'})")
        self.p("", *table([(lvl, text, n) for (lvl, text), n in kinds.most_common(15)], ("level", "message (ids and numbers folded)", "times")))

    def integrations(self):
        """What the OpenClaw plugin and opencode-permission-notify did, from the events Docket keeps of them."""
        self.h("11. OpenClaw plugin and permission classes")
        kinds = ("plugin:refused", "conclusion:delivered", "permission:pm", "permission:person", "permission:denied",
                 "permission:waiting", "permission:pm-decided", "permission:escalated", "permission:pm-late", "turn:answered-question")
        marks = ",".join("?" * len(kinds))
        counts = dict(self.c.execute(f"select kind, count(*) from events where kind in ({marks}) group by kind", kinds).fetchall())
        self.p(*table([(k, counts.get(k, 0)) for k in kinds], ("event", "times")))
        refused = self.c.execute("select at, delegation_id, detail from events where kind = 'plugin:refused' order by id desc limit 10").fetchall()
        if refused:
            self.p("", "Latest refusals (a write into a handoff that did not come from Docket):", "",
                   *table([(ts(r["at"]), r["delegation_id"] or "-", j(r["detail"]).get("agent"), j(r["detail"]).get("rule"),
                            (j(r["detail"]).get("command") or "")[:60]) for r in refused], ("at", "handoff", "agent", "rule", "command")))
        decided = self.c.execute("select at, delegation_id, kind, detail from events where kind in ('permission:pm-decided', 'permission:escalated') order by id desc limit 10").fetchall()
        if decided:
            self.p("", "Class 1 requests put to the PM:", "",
                   *table([(ts(r["at"]), r["delegation_id"], j(r["detail"]).get("decision") or "to the person", "; ".join(j(r["detail"]).get("what") or [])[:50],
                            (j(r["detail"]).get("reason") or "")[:70]) for r in decided], ("at", "handoff", "decision", "request", "reason")))

    def memory(self):
        self.h("12. Closings written back to OpenClaw's memory")
        if not MEMORY.exists():
            self.p(f"- no memory folder at {MEMORY}")
            return
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(MEMORY.glob("20*.md")))
        missing = []
        rows = self.c.execute("select id, title, updated_at from delegations where state = 'closed' order by updated_at").fetchall()
        for d in rows:
            if d["id"] not in text:
                missing.append((ts(d["updated_at"]), d["title"][:30], self.ending(dict(d) | {"state": "closed"})))
        self.p(f"- closed handoffs: {len(rows)}, with no memory entry: {len(missing)}")
        if missing:
            self.p("", *table(missing[-25:], ("closed", "handoff", "ending")))

    def report(self, transcript: bool, memory: bool) -> str:
        self.out = [f"# Docket audit {dt.datetime.now():%Y-%m-%d %H:%M}", "", f"Ledger copy of {LEDGER}"]
        for section in (self.overview, self.endings, self.cards, self.participation, self.registration, self.pm):
            section()
        self.writers(transcript)
        self.watchdog()
        self.invariants()
        self.service_log()
        self.integrations()
        if memory:
            self.memory()
        return "\n".join(self.out) + "\n"

    def timeline(self, prefixes: list[str], since: int | None) -> str:
        out = []
        for prefix in prefixes:
            for d in self.c.execute("select * from delegations where id like ?", (prefix + "%",)):
                out.append(f"\n## {d['id']}  {d['title']}  state={d['state']} phase={d['phase'] if 'phase' in self.columns else '-'} rounds={d['rounds']}")
                out.append(f"dispatched {ts(d['dispatched_at'], True)}  updated {ts(d['updated_at'], True)}  from {d['source_key']}  in {d['directory']}")
                for e in self.c.execute("select * from events where delegation_id = ? and at >= ? order by id", (d["id"], since or 0)):
                    detail = e["detail"] or ""
                    if e["kind"] == "pm:invalid":
                        x = j(detail)
                        detail = json.dumps({"attempt": x.get("attempt"), "errors": x.get("errors"), "raw": (x.get("raw") or "")[:200]}, ensure_ascii=False)
                    out.append(f"  {ts(e['at'], True)}  {e['kind']:<22} {detail[:240]}")
                for card in self.c.execute("select * from cards where delegation_id = ? and created_at >= ? order by id", (d["id"], since or 0)):
                    body = j(card["body"])
                    flags = [k for k in ("pending_relay", "pending_permission", "pm_retry", "docket_fault", "stalled", "round_spent", "orphan") if body.get(k)]
                    out.append(f"  card {card['id']} {card['kind']} queued={card['queued']} created {ts(card['created_at'], True)} resolved {ts(card['resolved_at'], True)} {flags}")
                    out.append(f"       {(body.get('progress') or '')[:200]}")
                    for x in body.get("decisions") or []:
                        out.append(f"       ? {x.get('question')} [{' / '.join(o.get('label', '') for o in x.get('options') or [])}]")
                    if card["resolution"]:
                        out.append(f"       = {card['resolution'][:220]}")
        return "\n".join(out) + "\n"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out_dir = None
    if "--out" in argv:
        i = argv.index("--out")
        out_dir = Path(argv[i + 1]).expanduser()
        del argv[i:i + 2]
    workdir = out_dir or Path(os.environ.get("TMPDIR", "/tmp")) / f"docket-audit-{stamp}"
    copy = copy_ledger(workdir / "docket.sqlite")
    audit = Audit(copy)
    if argv[0] == "report":
        text = audit.report("--transcript" in argv, "--memory" in argv)
        if out_dir:
            (out_dir / "report.md").write_text(text, encoding="utf-8")
            print(f"wrote {out_dir / 'report.md'} (ledger copy alongside)")
        else:
            print(text)
    elif argv[0] == "timeline":
        since = None
        if "--since" in argv:
            i = argv.index("--since")
            since = int(dt.datetime.strptime(argv[i + 1], "%Y-%m-%d %H:%M").timestamp() * 1000)
            del argv[i:i + 2]
        print(audit.timeline(argv[1:], since))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
