"""Compare the judge's answers with the reference labels.

    python3 -I eval/score.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from judge.relevance import should_move  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "eval/data"
THRESHOLDS = (0.3, 0.5, 0.7, 0.85)


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main():
    results = json.loads((DATA / "jev_results.json").read_text(encoding="utf-8"))
    labels = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((DATA / "labels").glob("c*.json"))}

    print("== context: pick earlier messages that belong to the task")
    print("threshold  precision  recall(any)  recall(core)  F1(any)")
    for threshold in THRESHOLDS:
        tp = fp = fn = core_hit = core_total = 0
        for case, scores in results["context"].items():
            gold = labels.get(case, {}).get("before")
            if not gold:
                continue
            for message_id, score in scores.items():
                label = gold.get(message_id, 0)
                picked = score >= threshold
                tp += picked and label >= 1
                fp += picked and label == 0
                fn += (not picked) and label >= 1
                if label == 2:
                    core_total += 1
                    core_hit += picked
        p, r, f = prf(tp, fp, fn)
        print(f"  {threshold:.2f}      {p:.2f}       {r:.2f}         {core_hit / core_total if core_total else 1:.2f}        {f:.2f}")

    print("\n== attribution: user messages after a dispatch")
    gold_of = {}
    for case, label in labels.items():
        for message_id, value in (label.get("after") or {}).items():
            gold_of.setdefault(message_id, set())
            if value:
                gold_of[message_id].add(case)
    print("threshold  moved  correct  wrong-task  wrong-moved(main)  missed")
    for threshold in THRESHOLDS:
        moved = correct = wrong_task = wrong_main = missed = 0
        for message_id, answer in results["attribution"].items():
            gold = gold_of.get(message_id, set()) & set(answer["active"])
            confident = should_move(answer, threshold)
            if confident:
                moved += 1
                if answer["choice"] in gold:
                    correct += 1
                elif gold:
                    wrong_task += 1
                else:
                    wrong_main += 1
            elif gold:
                missed += 1
        print(f"  {threshold:.2f}      {moved:4d}   {correct:4d}     {wrong_task:4d}          {wrong_main:4d}            {missed:4d}")


if __name__ == "__main__":
    main()
