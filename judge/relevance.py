"""The judge: which earlier messages belong to a delegation, and where a new message belongs.

Both are System One questions, answered by whichever judge backend is configured
(judge/backend.py); the thresholds and the stitching stay in code.

- `context_scores`: at dispatch time, one Noul per earlier message ("is this part of
  the task's context?"). The messages above the threshold, verbatim, become the card's
  background material.
- `attribute`: after dispatch, one Choice over the active delegations plus a companion
  Noul ("does it belong to any of them?"). Code moves a message only when both agree
  with high probability; otherwise it stays in the main thread.
"""

from __future__ import annotations

from judge import backend
from judge.redact import redact

# Jev's default limits are 64k tokens per request and 32k per question. Chinese runs
# close to one token per character, so budget in characters with headroom.
STATE_BUDGET = 20_000
MESSAGE_CHARS = 600
BRIEF_CHARS = 1_500
OVERLAP = 3

CONTEXT_CRITERIA = {
    "true": (
        "The message states or discusses this task's goal, requirements, constraints, preferences, "
        "acceptance criteria, the problem it fixes, or a decision the user made about it; or it explains "
        "background that someone taking over this exact task would need to read."
    ),
    "false": (
        "The message is about another topic, or about a different piece of work in the same project, "
        "or is small talk, or would not help someone carry out this exact task."
    ),
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def task_view(task: dict) -> dict:
    return {
        "title": task["title"],
        "instruction": redact(task.get("instruction", "")),
        "brief_excerpt": _clip(redact(task.get("brief", "")), BRIEF_CHARS),
    }


def _rows(messages: list[dict]) -> list[dict]:
    return [
        {"id": m["id"], "speaker": "user" if m["role"] == "user" else "assistant", "time": m["time"], "text": _clip(redact(m["text"]), MESSAGE_CHARS)}
        for m in messages
    ]


def _chunks(rows: list[dict]) -> list[list[dict]]:
    """Split rows into windows that fit the state budget, overlapping a little."""
    chunks, current, size = [], [], 0
    for row in rows:
        length = len(row["text"]) + 40
        if current and size + length > STATE_BUDGET:
            chunks.append(current)
            current = current[-OVERLAP:]
            size = sum(len(r["text"]) + 40 for r in current)
        current.append(row)
        size += length
    if current:
        chunks.append(current)
    return chunks


def context_scores(task: dict, messages: list[dict]) -> tuple[dict[str, float], dict]:
    """Return ({message id: P(relevant)}, usage)."""
    scores: dict[str, float] = {}
    usage = {"input_tokens": 0, "requests": 0}
    view = task_view(task)
    for chunk in _chunks(_rows(messages)):
        state = {"task": view, "conversation": chunk}
        questions = {
            row["id"]: {
                "type": "noul",
                "instructions": f"Is the conversation message with id `{row['id']}` part of the context of `task`?",
                "criteria": CONTEXT_CRITERIA,
            }
            for row in chunk
        }
        result = backend.ask(state, questions)
        usage["input_tokens"] += result.get("usage", {}).get("input_tokens", 0)
        usage["requests"] += 1
        for key, answer in result["answers"].items():
            # Overlapping rows are judged twice; keep the higher score.
            scores[key] = max(scores.get(key, 0.0), answer["noul"])
    return scores, usage


ROUND_CHARS = 300
MAX_ROUNDS = 6


def delegation_view(task: dict) -> dict:
    """A delegation as the judge sees it: the task plus the latest instructions it was sent."""
    view = task_view(task)
    rounds = task.get("rounds") or []
    if rounds:
        view["instructions_sent"] = [_clip(redact(r), ROUND_CHARS) for r in rounds[-MAX_ROUNDS:]]
    return view


def attribute(message: dict, recent: list[dict], active: list[dict]) -> tuple[dict, dict]:
    """Which active delegation, if any, `message` is about.

    `active` items carry "id", "title", "instruction", "brief" and "rounds" (instruction
    texts the delegation's OpenCode session has received so far).

    Returns ({"choice", "probabilities", "confidence", "belongs", "new_work"}, usage).
    Callers move a message only when choice and belongs are both high and new_work is low.
    """
    if not active:
        return {"choice": None, "probabilities": {}, "belongs": 0.0, "new_work": 0.0}, {"input_tokens": 0, "requests": 0}
    state = {
        "delegations": {task["id"]: delegation_view(task) for task in active},
        "recent_messages": _rows(recent[-3:]),
        "message": _rows([message])[0],
    }
    options = {
        task["id"]: f"The message is about the specific work in delegation `{task['id']}` ({task['title']}), as described by its task and instructions_sent."
        for task in active
    }
    options["none"] = "The message is not about the specific work of any listed delegation."
    questions = {
        "which": {
            "type": "choice",
            "instructions": (
                "Which delegation's specific work is `message` about: asking for its progress, giving feedback on it, "
                "adding a requirement to it, reporting or checking its result, or deciding what happens to it? "
                "Being in the same project or a similar area is not enough; it must be the same piece of work."
            ),
            "criteria": options,
        },
        "belongs": {
            "type": "noul",
            "instructions": "Is `message` about the specific work of one of the `delegations`, not merely the same project or a similar area?",
            "criteria": {
                "true": "It refers to work that one delegation's task or instructions_sent describe: its progress, feedback, an added requirement, its result, or its next step.",
                "false": "It is about something else: another topic, another piece of work in the same project, or a similarly named but different system.",
            },
        },
        "new_work": {
            "type": "noul",
            "instructions": "Does `message` ask for a new, separate piece of work to be started or handed off, rather than following up on work already handed off?",
            "criteria": {
                "true": "It asks to start, delegate or hand off something that is not yet covered by any delegation's task or instructions_sent.",
                "false": "It follows up on, corrects, accepts or asks about work already handed off, or it is unrelated chat.",
            },
        },
    }
    result = backend.ask(state, questions)
    answers = result["answers"]
    which = answers["which"]
    choice = which.get("choice")
    return (
        {
            "choice": None if choice == "none" else choice,
            "probabilities": which.get("probabilities", {}),
            "confidence": which.get("confidence"),
            "belongs": answers["belongs"]["noul"],
            "new_work": answers["new_work"]["noul"],
        },
        {"input_tokens": result.get("usage", {}).get("input_tokens", 0), "requests": 1},
    )


CARD_MESSAGES = 20  # the latest user messages after a card that are judged against it


def card_view(card: dict) -> dict:
    """A card as the judge sees it: what it says and what it asks, without Docket's own fields."""
    return {
        "progress": _clip(redact(card.get("progress") or ""), MESSAGE_CHARS),
        "decisions": [
            {
                "id": d["id"],
                "question": redact(d.get("question") or ""),
                "options": [{"id": o["id"], "label": redact(o.get("label") or "")} for o in d.get("options") or []],
            }
            for d in card.get("decisions") or []
        ],
    }


def card_status(task: dict, card: dict, messages: list[dict], threshold: float = 0.9, pick_min: float = 0.5) -> tuple[dict, dict]:
    """Has the user settled a waiting card in the main chat, in the messages written after it?

    `messages` are the user's main-chat messages after the card went up ({id, role, time,
    text}, oldest first). One Choice decides the card's status (answered / superseded / open),
    one picks the message that settles it, and one per decision reads which option the user
    took ("other" when they said something none of the options says).

    Returns ({"status", "probability", "message", "choices", "probabilities"}, usage). Code calls
    a card answered only when the status is sure, one message is named, and every decision is
    read; superseded needs the status and the message. Anything short of that is "open".
    """
    usage = {"input_tokens": 0, "requests": 0}
    messages = [m for m in messages if m["role"] == "user"][-CARD_MESSAGES:]
    if not messages:
        return {"status": "open", "probability": 0.0, "message": None, "choices": {}, "probabilities": {}}, usage
    view = card_view(card)
    asks = bool(view["decisions"])
    state = {"task": delegation_view(task), "card": view, "messages_after_card": _rows(messages)}
    answered = (
        "A message gives the user's decision on the card's question(s): it picks one of the options, or says what to do "
        "in words that settle each question. Asking about the topic, thinking aloud, or deciding a different question is not enough."
        if asks
        else "A message responds to what the card reports: the user accepts the result, says it is done, or says what to do next with this work."
    )
    questions = {
        "status": {
            "type": "choice",
            "instructions": (
                "`card` waits on Docket for the user's answer about `task`. The user did not answer it there; `messages_after_card` "
                "is what they wrote in the main chat after it went up. Did they settle the card there?"
            ),
            "criteria": {
                "answered": answered,
                "superseded": (
                    "The messages show the card no longer needs an answer: this work was dropped, done another way, or replaced "
                    "by a different plan, so none of what the card asks will be taken up."
                ),
                "open": "Neither: the messages do not settle the card, or they are about something else.",
            },
        },
        "message": {
            "type": "choice",
            "instructions": "Which message in `messages_after_card` settles `card` (answers it, or makes it moot)?",
            "criteria": {
                **{row["id"]: f"Message `{row['id']}` settles the card." for row in state["messages_after_card"]},
                "none": "No message settles the card.",
            },
        },
    }
    for decision in view["decisions"]:
        questions[f"pick_{decision['id']}"] = {
            "type": "choice",
            "instructions": f"In `messages_after_card`, which option did the user take for the card's decision `{decision['id']}` ({decision['question']})?",
            "criteria": {
                **{o["id"]: f"The user chose option `{o['id']}`: {o['label']}." for o in decision["options"]},
                "other": "The user decided this question, but with something none of the options says.",
                "none": "The messages do not decide this question.",
            },
        }
    result = backend.ask(state, questions)
    usage = {"input_tokens": result.get("usage", {}).get("input_tokens", 0), "requests": 1}
    answers = result["answers"]
    status = answers["status"]
    probabilities = {"status": status.get("probabilities", {}), "message": answers["message"].get("probabilities", {})}
    out = {"status": "open", "probability": status.get("probabilities", {}).get(status.get("choice"), 0.0), "message": None, "choices": {}, "probabilities": probabilities}
    choice = status.get("choice")
    named = answers["message"].get("choice")
    if choice not in ("answered", "superseded") or out["probability"] < threshold:
        return out, usage
    if named in (None, "none") or answers["message"].get("probabilities", {}).get(named, 0) < pick_min:
        return out, usage
    choices = {}
    if choice == "answered":
        for decision in view["decisions"]:
            pick = answers[f"pick_{decision['id']}"]
            probabilities[f"pick_{decision['id']}"] = pick.get("probabilities", {})
            if pick.get("choice") in (None, "none") or pick.get("probabilities", {}).get(pick["choice"], 0) < pick_min:
                return out, usage  # a decision left unread: the card is not settled
            choices[decision["id"]] = pick["choice"]
    return {**out, "status": choice, "message": named, "choices": choices}, usage


def should_move(answer: dict, threshold: float = 0.85, new_work_max: float = 0.5) -> bool:
    """Move a main-thread message into a delegation only when Jev is sure on every axis."""
    choice = answer.get("choice")
    return bool(
        choice
        and answer["belongs"] >= threshold
        and answer["probabilities"].get(choice, 0) >= threshold
        and answer.get("new_work", 0) < new_work_max
    )
