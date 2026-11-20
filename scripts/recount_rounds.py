"""Recount `delegations.rounds` from what people actually did, under the rule that every
time a person takes part in a handoff is one round (see BRIEF_round_budget.md).

    /usr/bin/python3 -I scripts/recount_rounds.py              # print the differences, change nothing
    /usr/bin/python3 -I scripts/recount_rounds.py --apply      # write the recounted rounds
    /usr/bin/python3 -I scripts/recount_rounds.py --db PATH    # another ledger

Before this change only relays were counted, so the stored numbers are low. A round is:
  - a card the user answered on the panel (a choice or a reply; a permission card too),
    answered once even if it was replayed later;
  - a main-thread message the judge moved onto the handoff (an `attributed` event at or
    above move_threshold, or a card resolved "via": "chat").
Not a round: closing, taking over, expired or superseded cards, retries of the docket's own
failures, and confirming a relay a mention proposed (that mention was already counted).
Rows counted after the change carry `round` events; for them the events are the count.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docket import config, ledger  # noqa: E402


def human_rounds(conn, delegation_id: str, threshold: float) -> tuple[int, list[str]]:
    """(rounds, one line per round explaining it)."""
    rows = conn.execute("select kind, detail from events where delegation_id = ? and kind = 'round'", (delegation_id,)).fetchall()
    if rows:  # counted by the new code: trust it
        return len(rows), [f"round event #{json.loads(d)['rounds']}" for _, d in rows]
    why: list[str] = []
    chat_cards = 0
    for card in conn.execute("select id, kind, body, resolution from cards where delegation_id = ? and resolution is not null order by id", (delegation_id,)):
        resolution = json.loads(card["resolution"])
        body = json.loads(card["body"])
        if resolution.get("action") or body.get("round_spent") or body.get("pm_retry"):
            continue  # closed, taken over, expired, superseded, retried: nobody decided anything
        if body.get("pending_relay"):
            continue  # confirming a mention's relay: the mention is the round
        if resolution.get("via") == "chat":
            chat_cards += 1
            why.append(f"card {card['id']} answered from the chat")
            continue
        if resolution.get("choices") or (resolution.get("reply") or "").strip() or body.get("pending_permission"):
            why.append(f"card {card['id']} ({'permission' if body.get('pending_permission') else card['kind']}) answered")
    moved = 0
    for (detail,) in conn.execute("select detail from events where delegation_id = ? and kind = 'attributed'", (delegation_id,)):
        answer = json.loads(detail or "{}")
        if answer.get("choice") == delegation_id and (answer.get("belongs") or 0) >= threshold:
            moved += 1
    # A mention that answered an open card shows up both as that card and as an attributed event.
    for _ in range(max(0, moved - chat_cards)):
        why.append("chat message moved onto it")
    return len(why), why


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=ledger.DB)
    parser.add_argument("--apply", action="store_true", help="write the recounted rounds (default: print only)")
    args = parser.parse_args()
    if args.apply:
        conn = sqlite3.connect(args.db, timeout=10, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    threshold = config.load().get("move_threshold", 0.85)
    changes = []
    for row in conn.execute("select id, title, state, rounds from delegations order by dispatched_at").fetchall():
        counted, why = human_rounds(conn, row["id"], threshold)
        if counted != row["rounds"]:
            changes.append((row, counted, why))
    for row, counted, why in changes:
        print(f"{row['id']}  {row['title']}  [{row['state']}]  stored {row['rounds']} -> counted {counted}")
        for line in why:
            print(f"    - {line}")
    print(f"\n{len(changes)} handoff(s) differ (move_threshold {threshold}).")
    if args.apply and changes:
        conn.execute("begin immediate")
        for row, counted, _ in changes:
            conn.execute("update delegations set rounds = ? where id = ?", (counted, row["id"]))
            conn.execute(
                "insert into events (delegation_id, at, kind, detail) values (?, ?, 'rounds:recounted', ?)",
                (row["id"], ledger.now(), json.dumps({"from": row["rounds"], "to": counted})),
            )
        conn.execute("commit")
        print("written.")
    elif changes:
        print("nothing written; run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
