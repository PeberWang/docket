"""docket: what the main chat, other sessions and people use instead of writing to a PM or OpenCode.

    docket note <handoff> "<text>"   pass a word on: logged, then the PM is woken the docket's way
    docket status <handoff>          state, phase and each phase change, rounds, the last events,
                                     the OpenCode session and when it last said something
    docket list                      the handoffs still open, with their ids
    docket doctor                    every link Docket depends on, and the fix for each that fails
    docket audit report|timeline     a read-only report from a copy of the ledger and the logs (docket audit --help)

<handoff> is the id (ses_…) or any unique prefix of it, with or without ses_.
Only the docket writes to OpenCode and wakes PMs: `note` goes through the running service,
`status` and `list` only read the ledger.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import engines, ledger, wording  # noqa: E402
from docket.engines import session_of  # noqa: E402


def fail(message: str, code: int = 1):
    print(f"docket: {message}", file=sys.stderr)
    sys.exit(code)


def stamp(ms) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S") if ms else "-"


def read_only() -> sqlite3.Connection:
    if not ledger.DB.exists():
        fail(f"no ledger at {ledger.DB}")
    conn = sqlite3.connect(f"file:{ledger.DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# The CLI only reads: it never migrates the ledger under a running service. The service migrates
# it every time it opens it (ledger.connect), so a ledger without phases means the service running
# now is older than this CLI; show what the ledger has and say a restart brings the rest.
RESTART = "launchctl kickstart -k gui/$(id -u)/io.github.peberwang.docket"


def schema(conn) -> tuple[set, set]:
    """The delegations columns and the tables this ledger has."""
    columns = {r[1] for r in conn.execute("pragma table_info(delegations)")}
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
    return columns, tables


def field(row, name, default=None):
    return row[name] if name in row.keys() else default


def open_card(conn, delegation_id):
    try:
        return ledger.open_card(conn, delegation_id)
    except sqlite3.OperationalError:  # a ledger from before queued cards
        return conn.execute("select * from cards where delegation_id = ? and resolved_at is null order by id limit 1", (delegation_id,)).fetchone()


def find(conn, handoff: str):
    row = conn.execute("select * from delegations where id = ?", (handoff,)).fetchone()
    if row:
        return row
    bare = handoff.removeprefix("ses_")
    rows = conn.execute("select * from delegations where id like ? or id like ? order by updated_at desc", (f"{handoff}%", f"ses_{bare}%")).fetchall()
    if not rows:
        fail(f"no handoff matches {handoff}")
    if len(rows) > 1:
        fail(f"{handoff} matches {len(rows)} handoffs: " + ", ".join(r["id"] for r in rows[:5]))
    return rows[0]


def session_line(row) -> str:
    """The OpenCode session and when it last said something, if the service can be read."""
    engine = field(row, "engine", "opencode")
    if engine != "opencode":
        return f"{engine} session {field(row, 'engine_session') or '-'}"
    session = session_of({"engine": engine, **dict(row)})
    try:
        service = engines.Service()
        info = service.session(session)
        newest = max((max([v for v in (m.get("time") or {}).values() if isinstance(v, (int, float))] or [0]) for m in service.messages(session, limit=3) or []), default=0)
        times = info.get("time") or {}
        busy = (times.get("busy") or 0) > (times.get("idle") or 0)
        return f"{session} ({'busy' if busy else info.get('outcome') or 'idle'}), last message {stamp(newest)}"
    except Exception as error:
        return f"{session} (OpenCode not readable: {str(error)[:80]})"


def status(handoff: str) -> str:
    conn = read_only()
    columns, tables = schema(conn)
    row = find(conn, handoff)
    out = [f"{row['title']}  [{row['id']}]", f"state     {row['state']}"]
    if "phase" in columns:
        out.append(f"phase     {row['phase'] or ledger.PHASE_OF.get(row['state'], '-')} since {stamp(row['phase_at'])}")
    out += [f"rounds    {row['rounds']}/{ledger.ROUND_LIMIT}", f"opencode  {session_line(row)}"]
    turn = conn.execute("select * from turns where delegation_id = ? order by id desc limit 1", (row["id"],)).fetchone() if "turns" in tables else None
    if turn:
        out.append(
            f"turn      #{turn['id']} {turn['via']} attempt {turn['attempt']}: sent {stamp(turn['started_at'])}, "
            f"seen running {stamp(turn['confirmed_at'])}, last activity {stamp(turn['last_activity'])}, "
            + (f"ended {turn['outcome']} {stamp(turn['ended_at'])}" if turn["ended_at"] else "not ended")
        )
    card = open_card(conn, row["id"])
    if card:
        body = json.loads(card["body"])
        mark = " [stalled]" if body.get("stalled") else " [docket fault]" if body.get("docket_fault") else ""
        out.append(f"card      #{card['id']} {card['kind']}{mark}: {(wording.body(body.get('progress'), body.get('next')) or '')[:120]}")
    phases = conn.execute("select at, kind, detail from events where delegation_id = ? and kind like 'phase:%' order by id", (row["id"],)).fetchall()
    if phases:
        out.append("phases")
        for e in phases:
            detail = json.loads(e["detail"] or "{}")
            extra = ", ".join(f"{k}={v}" for k, v in detail.items() if k != "from" and v is not None)
            out.append(f"  {stamp(e['at'])}  {detail.get('from') or '-'} -> {e['kind'][6:]}" + (f"  ({extra})" if extra else ""))
    out.append("last events")
    for e in reversed(conn.execute("select at, kind, detail from events where delegation_id = ? order by id desc limit 3", (row["id"],)).fetchall()):
        out.append(f"  {stamp(e['at'])}  {e['kind']}  {(e['detail'] or '')[:160]}")
    if "phase" not in columns or "turns" not in tables:
        out.append(f"note      the running Docket service is older than this CLI (ledger has no phases or turns yet); restart it to see phase changes: {RESTART}")
    return "\n".join(out)


def listing() -> str:
    conn = read_only()
    rows = conn.execute("select * from delegations where state != 'closed' or updated_at >= ? order by updated_at desc", (ledger.now() - 86400_000,)).fetchall()
    return "\n".join(f"{r['id']}  {r['state']:<10} {(field(r, 'phase') or '-'):<17} {r['title']}" for r in rows) or "(none)"


def note(handoff: str, text: str, source: str) -> str:
    try:
        service = json.loads((ledger.HOME / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("the Docket service is not running (no daemon.json)")
    request = urllib.request.Request(
        f"{service['url']}/api/delegations/{handoff}/note",
        data=json.dumps({"text": text, "source": source}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Docket-Token": service["token"]},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        fail(f"Docket refused: {json.loads(error.read() or b'{}').get('error') or error}")
    except urllib.error.URLError as error:
        fail(f"the Docket service is not reachable: {error.reason}")
    # Passing words on costs no round; the count is the person's card answers so far.
    return f"passed on to the PM of {result['id']} ({result['rounds']}/{ledger.ROUND_LIMIT} rounds used); see docket status {result['id']}"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    source = "docket note"
    if "--from" in args:
        i = args.index("--from")
        if i + 1 >= len(args):
            fail("--from needs a value", 2)
        source = args[i + 1]
        del args[i : i + 2]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    command, rest = args[0], args[1:]
    if command == "audit":
        from docket import audit

        return audit.main(rest)
    if command == "note" and len(rest) >= 2:
        print(note(rest[0], " ".join(rest[1:]), source))
    elif command == "status" and len(rest) == 1:
        print(status(rest[0]))
    elif command == "list" and not rest:
        print(listing())
    elif command == "doctor" and not rest:
        from docket import doctor

        text, code = doctor.report()
        print(text)
        sys.exit(code)
    else:
        fail('usage: docket note <handoff> "<text>" [--from <who>] | docket status <handoff> | docket list | docket doctor | docket audit report', 2)


if __name__ == "__main__":
    main()
