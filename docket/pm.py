"""The PM: one OpenClaw session per handoff, woken only when something happened.

The daemon calls `openclaw agent --session-key agent:main:docket-<id>` without
`--deliver`, so nothing reaches the chat channel. The PM may use its tools (read the
handoff doc, run the tests) but its final reply must be one JSON object; code digs it out
of whatever prose surrounds it, validates it, asks once more with the errors if it is off,
and once more on the fallback model. Three misses are the docket's own failure, shown as
a failure card with a retry; they never end the handoff.

Prompts are written in English; `configure(locale)` sets the language the PM writes
the user-facing text in, and the length limits that go with it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from docket.i18n import LIMITS, TEXT
from docket.ledger import ROUND_LIMIT

MODEL = None  # None: the OpenClaw agent's own model; config.json "pm_model" overrides
FALLBACK = None  # config.json "pm_fallback_model": the third try after two invalid replies
TIMEOUT = 900
OPENCODE_CHARS = 8000
LOCALE = "en"


def configure(locale: str, model: str | None = None, fallback: str | None = None) -> None:
    global LOCALE, MODEL, FALLBACK
    LOCALE = locale
    MODEL = model or None
    FALLBACK = fallback or None


def _limits() -> dict:
    return LIMITS[LOCALE]


def _language() -> str:
    return TEXT[LOCALE]["language"]


def rules() -> str:
    return f"""You are the user's project manager (PM) on Docket for exactly one handoff. Docket is a native panel on the user's Mac: work handed to OpenCode moves forward there as short briefs the user decides on, instead of in the chat.

Rules:
1. Do not send chat messages of any kind and do not call message or send tools. Your final reply is read only by the Docket program.
2. Decide technical details yourself and list them in decided_for_you. Put only what truly needs the user's call in decisions (at most 2, empty if none). Each decision has 2–4 short options and exactly one with recommended=true.
3. progress is one sentence with the state and the outcome, not the process.
4. To verify a delivery you may read files and run unit tests. Do not change project code, commit, push, release, run anything that sends or deploys (email, chat, push notifications, scheduled jobs, servers), or edit cron.
5. Hard rule: a handoff has a budget of {ROUND_LIMIT} rounds of the user's attention, and Docket enforces it in code.
   a. Every time the user takes part costs one round: answering any card (a permission request too), writing about the handoff in the chat, even asking "can this be done?". What you check, run and decide on your own costs nothing.
   b. Before writing a card, ask yourself whether you can decide it. Decide what you can; when you must ask, ask everything at once. Never split one question across rounds.
   c. When the budget is spent Docket refuses every further card. You must wrap up: close with what got done and list what is left as a handoff note the user can continue from in OpenCode. Do not try to ask again.
6. Working with the coding agent: write each instruction you relay as a complete brief, with the acceptance criteria stated up front (what must pass, what to report back), so it can finish without coming back to the user. Prefer one complete instruction to several partial ones.
7. Write every user-facing string in {_language()}, in plain words. Someone who has not read the code or the project's docs must understand each question; name things by what they are, not by internal terms (a "gate table" is a file's name, not an explanation), and keep file names out of decisions unless the user must pick one.
8. Use no Markdown in card text: no asterisks for emphasis, no backticks, no headings. The panel shows plain text, so Markdown would appear literally.
9. Your final reply is a single JSON object and nothing else: no prose before or after it, no code fence."""


def card_format() -> str:
    l = _limits()
    return f"""Brief JSON:
{{"kind": "report|question|failure",
 "progress": "one sentence, at most {l['progress']} characters",
 "decisions": [{{"id": "d1", "question": "what the user decides, at most {l['question']} characters",
                "options": [{{"id": "a", "label": "at most {l['label']} characters", "recommended": true}},
                            {{"id": "b", "label": "…", "recommended": false}}]}}],
 "decided_for_you": ["one decision you made for the user per line, at most 6, each at most {l['decided']} characters"]}}
kind: report when OpenCode delivered; question when OpenCode is waiting for an answer or the user must choose a direction; failure when the run failed or was interrupted."""


def action_format() -> str:
    return f"""Action JSON, one of:
{{"action": "relay", "message": "the follow-up instruction for OpenCode: the user's decision turned into a clear, actionable request"}}
{{"action": "card", "card": <Brief JSON>}}        // when you can handle it without OpenCode, or need to ask the user once more
{{"action": "close", "conclusion": "one sentence: what got done, where the result is, what is left (at most {_limits()['conclusion']} characters)",
 "remaining": ["optional: one unfinished item per line, at most 6, each at most {_limits()['decided']} characters"]}}"""


def budget_line(rounds: int) -> str:
    if rounds >= ROUND_LIMIT:
        return (f"Rounds used: {rounds}/{ROUND_LIMIT}. This was the user's last round: Docket will refuse any card. "
                "Relay if OpenCode should carry out this last instruction, otherwise close with remaining filled in.")
    return f"Rounds used: {rounds}/{ROUND_LIMIT}. A card costs the user another round: only write one you cannot avoid."


class PMError(RuntimeError):
    pass


# ---------- prompts ----------

def first_wake(handoff: dict, context: list[dict], event: str, opencode_reply: str) -> str:
    lines = [rules(), "", f"## Handoff: {handoff['title']}", f"OpenCode session: {handoff['id']}"]
    if handoff.get("directory"):
        lines.append(f"Project directory: {handoff['directory']}")
    if handoff.get("brief"):
        lines += ["", "### Task brief (excerpt)", handoff["brief"][:4000]]
    if context:
        lines += ["", "### What the user said about it in the main chat, and its background (picked by the judge, in time order)"]
        lines += [f"- [{m['time']}] {'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}" for m in context]
    lines += ["", event_block(event, opencode_reply, handoff.get("rounds", 0)), "", card_format()]
    return "\n".join(lines)


def event_block(event: str, opencode_reply: str, rounds: int) -> str:
    head = {
        "succeeded": "OpenCode finished this round. Verify the delivery and write a brief.",
        "failed": "OpenCode's run failed. Find out why and write a brief (kind=failure).",
        "interrupted": "OpenCode's run was interrupted. Check where things stand and write a brief.",
        "question": "OpenCode is waiting for an answer. Answer it yourself if you can; put what the user must decide in decisions and write a brief (kind=question).",
    }.get(event, "OpenCode has news. Write a brief.")
    reply = opencode_reply.strip()
    if len(reply) > OPENCODE_CHARS:
        reply = reply[:OPENCODE_CHARS] + "\n… (truncated; read the handoff doc in the project if you need more)"
    return f"### This event (rounds used: {rounds}/{ROUND_LIMIT})\n{head}\n\nOpenCode's last reply:\n{reply or '(no text)'}"


def language_line() -> str:
    # Repeated on every wake: a session keeps the language of its earlier turns unless told.
    return f"Write every user-facing string in {_language()}, even if earlier turns used another language."


def later_wake(event: str, opencode_reply: str, rounds: int) -> str:
    return "\n".join([event_block(event, opencode_reply, rounds), "", language_line(), "", card_format()])


def decision_wake(card: dict, choices: dict, reply: str, rounds: int) -> str:
    picked = []
    for decision in card.get("decisions") or []:
        option = next((o for o in decision["options"] if o["id"] == choices.get(decision["id"])), None)
        if option:
            picked.append(f"- {decision['question']} → {option['label']}")
    lines = ["The user answered your last brief on Docket."]
    if picked:
        lines += ["Choices:"] + picked
    if reply.strip():
        lines += [f"They added: {reply.strip()}"]
    lines += [
        "",
        "Decide the next step: relay if OpenCode should keep working; card if you can handle it without OpenCode or need to clarify first; close if the work is done.",
        budget_line(rounds),
        language_line(),
        "",
        action_format(),
    ]
    return "\n".join(lines)


def mention_wake(text: str, rounds: int) -> str:
    return "\n".join(
        [
            f"The user mentioned this handoff in the main chat (the judge is confident): “{text}”",
            "If it is a new requirement or a correction, relay; if they are asking for progress or need an answer from you, card; if they are saying it is done, close.",
            budget_line(rounds),
            language_line(),
            "",
            action_format(),
        ]
    )


def wrap_up_wake(event: str, opencode_reply: str | None, rounds: int) -> str:
    """The budget is spent: no more cards, only a handoff note to close with."""
    lines = [
        f"The user has spent all {ROUND_LIMIT} rounds on this handoff (rounds used: {rounds}). Docket refuses any further card.",
        "Wrap it up now: check where the work stands (you may read files and run tests), then close.",
        "conclusion says what got done and where the result is; remaining lists every unfinished item, written so the user can continue in OpenCode without asking you.",
    ]
    if opencode_reply is not None:
        lines += ["", event_block(event, opencode_reply, rounds)]
    lines += ["", language_line(), "", 'Reply only with: {"action": "close", "conclusion": "…", "remaining": ["…"]}']
    return "\n".join(lines)


def close_wake(reply: str) -> str:
    extra = f"They added: {reply.strip()}\n" if reply.strip() else ""
    return f'The user closed this handoff on Docket. {extra}Give a one-sentence conclusion in {_language()}.\n\nReply only with: {{"action": "close", "conclusion": "…"}}'


# ---------- running and validating ----------

def run(session_key: str, message: str, timeout: int = TIMEOUT, model: str | None = None) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(message)
        path = handle.name
    try:
        done = subprocess.run(
            ["openclaw", "agent", "--session-key", session_key, *(["--model", model] if model else []), "--json", "--timeout", str(timeout), "--message-file", path],
            capture_output=True,
            text=True,
            timeout=timeout + 60,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if done.returncode != 0:
        raise PMError(f"openclaw agent exited {done.returncode}: {done.stderr[-500:]}")
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise PMError(f"openclaw agent printed non-JSON: {done.stdout[-300:]}") from error
    payloads = (result.get("result") or {}).get("payloads") or []
    text = "\n".join(p.get("text") or "" for p in payloads).strip()
    return text or ((result.get("result") or {}).get("meta") or {}).get("finalAssistantVisibleText", "")


_DECODER = json.JSONDecoder()


def json_objects(text: str, depth: int = 0):
    """Every JSON object in a reply, in the order they appear: the whole text, then code
    fences, then any object found by scanning prose. A string that itself holds JSON
    (a reply quoted once more) is opened too."""
    found: list = []

    def add(value):
        if isinstance(value, str) and depth < 2:
            for inner in json_objects(value, depth + 1):
                add(inner)
        elif isinstance(value, dict) and value not in found:
            found.append(value)

    text = (text or "").strip()
    try:
        add(json.loads(text))
    except json.JSONDecodeError:
        pass
    for block in re.findall(r"```[\w-]*\s*\n?(.*?)```", text, re.S):
        try:
            add(json.loads(block.strip()))
        except json.JSONDecodeError:
            pass
    i = text.find("{")
    while i != -1:
        try:
            value, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        add(value)
        i = text.find("{", end)
    return found


def extract_json(text: str):
    objects = json_objects(text)
    if not objects:
        raise ValueError("no JSON object found in the reply")
    return objects[0]


def _short(value, limit, field, errors):
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
    elif len(value) > limit:
        errors.append(f"{field} is longer than {limit} characters ({len(value)})")


def validate_card(card) -> list[str]:
    l = _limits()
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["the brief must be a JSON object"]
    if card.get("kind") not in ("report", "question", "failure"):
        errors.append("kind must be report, question or failure")
    _short(card.get("progress"), l["progress"], "progress", errors)
    decisions = card.get("decisions") or []
    if not isinstance(decisions, list) or len(decisions) > 2:
        errors.append("at most 2 decisions")
        decisions = []
    for i, decision in enumerate(decisions):
        where = f"decisions[{i}]"
        if not isinstance(decision, dict):
            errors.append(f"{where} must be an object")
            continue
        _short(decision.get("id"), 10, f"{where}.id", errors)
        _short(decision.get("question"), l["question"], f"{where}.question", errors)
        options = decision.get("options") or []
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            errors.append(f"{where}.options needs 2–4 options")
            continue
        if sum(1 for o in options if isinstance(o, dict) and o.get("recommended") is True) != 1:
            errors.append(f"{where} needs exactly one recommended=true")
        for j, option in enumerate(options):
            if not isinstance(option, dict):
                errors.append(f"{where}.options[{j}] must be an object")
                continue
            _short(option.get("id"), 10, f"{where}.options[{j}].id", errors)
            _short(option.get("label"), l["label"], f"{where}.options[{j}].label", errors)
            if len(option.get("why") or "") > l["why"]:
                errors.append(f"{where}.options[{j}].why is longer than {l['why']} characters")
    decided = card.get("decided_for_you") or []
    if not isinstance(decided, list) or len(decided) > 6 or any(not isinstance(d, str) or len(d) > l["decided"] for d in decided):
        errors.append(f"decided_for_you: at most 6 lines, each at most {l['decided']} characters")
    return errors


def validate_action(action) -> list[str]:
    if not isinstance(action, dict):
        return ["the action must be a JSON object"]
    kind = action.get("action")
    if kind == "relay":
        return [] if isinstance(action.get("message"), str) and action["message"].strip() else ["relay needs a non-empty message"]
    if kind == "close":
        errors: list[str] = []
        _short(action.get("conclusion"), _limits()["conclusion"], "conclusion", errors)
        remaining = action.get("remaining") or []
        if not isinstance(remaining, list) or len(remaining) > 6 or any(not isinstance(r, str) or len(r) > _limits()["decided"] for r in remaining):
            errors.append(f"remaining: at most 6 lines, each at most {_limits()['decided']} characters")
        return errors
    if kind == "card":
        return validate_card(action.get("card"))
    return ["action must be relay, card or close"]


def validate_wrap_up(action) -> list[str]:
    if not isinstance(action, dict) or action.get("action") != "close":
        return ['the budget is spent: reply with {"action": "close", ...} only']
    return validate_action(action)


class PMFailed(PMError):
    """Three tries, none usable. The docket's own fault, never the handoff's."""

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        last = attempts[-1]
        super().__init__(f"the PM's reply failed {len(attempts)} times: " + "; ".join(last["errors"]))

    @property
    def raw(self) -> str:
        return next((a["raw"] for a in reversed(self.attempts) if a["raw"].strip()), "")


def pick(text: str, validate):
    """The first JSON object in the reply that passes; otherwise the first one's errors."""
    objects = json_objects(text)
    if not objects:
        return None, ["no JSON object found in the reply"]
    first_errors = None
    for value in objects:
        errors = validate(value)
        if not errors:
            return value, []
        first_errors = first_errors or errors
    return None, first_errors


def ask(session_key: str, message: str, validate, record=None):
    """Wake the PM and return its validated JSON.

    Try, retry once with the errors, then once more on the fallback model. Every failed
    try is reported to `record(attempt, model, raw, errors)` with the raw reply.
    """
    tries = [MODEL, MODEL, FALLBACK or MODEL]
    attempts: list[dict] = []
    prompt = message
    for n, model in enumerate(tries, 1):
        try:
            text = run(session_key, prompt, model=model)
        except (PMError, OSError, subprocess.SubprocessError) as error:
            text, errors, value = "", [f"the PM did not answer: {error}"[:500]], None
        else:
            value, errors = pick(text, validate)
        if not errors:
            return normalize(value)
        attempts.append({"attempt": n, "model": model, "raw": text, "errors": errors})
        if record:
            record(n, model, text, errors)
        if text:
            prompt = "Your last reply did not pass:\n- " + "\n- ".join(errors) + "\n\nFix it and reply again with only the JSON object, no other text."
    raise PMFailed(attempts)


_MARKUP = (
    ("**", ""),
    ("__", ""),
    ("`", ""),
    ("### ", ""),
    ("## ", ""),
    ("# ", ""),
)


def plain(text: str) -> str:
    """Card text is shown as plain text: drop the Markdown a model reaches for by habit."""
    for token, replacement in _MARKUP:
        text = text.replace(token, replacement)
    return text.strip()


def _plain_deep(value):
    if isinstance(value, str):
        return plain(value)
    if isinstance(value, list):
        return [_plain_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain_deep(v) for k, v in value.items()}
    return value


def normalize(value: dict) -> dict:
    card = value.get("card") if value.get("action") == "card" else (value if "progress" in value else None)
    if card is not None:
        card.setdefault("decisions", [])
        card.setdefault("decided_for_you", [])
    for key in ("card", "message", "conclusion"):
        if key in value:
            value[key] = _plain_deep(value[key])
    if "progress" in value:
        value = _plain_deep(value)
    return value
