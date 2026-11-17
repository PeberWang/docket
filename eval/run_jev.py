"""Run the judge (Jev) over the evaluation set.

    python3 -I eval/run_jev.py            # all cases
    python3 -I eval/run_jev.py c05 c10    # some cases

Writes eval/data/jev_results.json (git-ignored).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from judge.relevance import attribute, context_scores  # noqa: E402

DATA = ROOT / "eval/data"


def seq(message_id: str) -> int:
    return int(message_id.lstrip("m"))


def main():
    cases = json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
    only_attribution = "--attribution" in sys.argv
    wanted = {a for a in sys.argv[1:] if not a.startswith("--")}
    out_path = DATA / "jev_results.json"
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {"context": {}, "attribution": {}}
    tokens = requests = 0

    for case in cases:
        if only_attribution or (wanted and case["case"] not in wanted):
            continue
        started = time.time()
        scores, usage = context_scores(case, case["before"])
        results["context"][case["case"]] = scores
        tokens += usage["input_tokens"]
        requests += usage["requests"]
        print(f"context {case['case']} {len(scores)} msgs {usage['requests']} req {time.time() - started:.2f}s", flush=True)

    # Attribution: every user message after a dispatch, against the delegations active at that point.
    tasks = {c["case"]: {"id": c["case"], **c} for c in cases}
    messages = {}
    for case in cases:
        for row in case["after"]:
            if row["role"] == "user":
                messages.setdefault(row["id"], row)
    history = sorted({row["id"]: row for case in cases for row in case["before"] + case["after"]}.values(), key=lambda r: seq(r["id"]))
    for message_id, row in sorted(messages.items(), key=lambda item: seq(item[0])):
        if wanted and not any(message_id in {r["id"] for r in tasks[c]["after"]} for c in wanted):
            continue
        active = [
            # Only the instructions the delegation had received before this message.
            {**t, "rounds": [r["text"] for r in t.get("rounds", []) if r["ts"] < row["ts"]]}
            for t in tasks.values()
            if t["dispatch_seq"] < seq(message_id) and any(r["id"] == message_id for r in t["after"])
        ]
        recent = [r for r in history if seq(r["id"]) < seq(message_id)][-3:]
        started = time.time()
        answer, usage = attribute(row, recent, active)
        answer["active"] = [t["id"] for t in active]
        results["attribution"][message_id] = answer
        tokens += usage["input_tokens"]
        requests += usage["requests"]
        print(f"attribute {message_id} -> {answer['choice']} ({answer['belongs']:.2f}, new {answer['new_work']:.2f}) among {len(active)} {time.time() - started:.2f}s", flush=True)

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{requests} requests, {tokens} input tokens, ~${tokens * 0.042 / 1e6:.4f}")


if __name__ == "__main__":
    main()
