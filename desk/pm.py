"""The PM: one OpenClaw session per handoff, woken only when something happened.

The daemon calls `openclaw agent --session-key agent:main:desk-<id>` without
`--deliver`, so nothing reaches the chat channel. The PM may use its tools (read the
handoff doc, run the tests) but its final reply must be one JSON object; code validates
it and asks once more with the errors if it is off.

Prompts are written in English; `configure(locale)` sets the language the PM writes
the user-facing text in, and the length limits that go with it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from desk.i18n import LIMITS, TEXT

MODEL = None  # None: the OpenClaw agent's own model; config.json "pm_model" overrides
TIMEOUT = 900
OPENCODE_CHARS = 8000
LOCALE = "en"


def configure(locale: str, model: str | None = None) -> None:
    global LOCALE, MODEL
    LOCALE = locale
    MODEL = model or None


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
5. A handoff gets at most 3 rounds of the user's decisions relayed to OpenCode. When they are used up, Docket suggests the user take it over in OpenCode.
6. Write every user-facing string in {_language()}, in plain words. Someone who has not read the code or the project's docs must understand each question; name things by what they are, not by internal terms (a "gate table" is a file's name, not an explanation), and keep file names out of decisions unless the user must pick one.
7. Use no Markdown in card text: no asterisks for emphasis, no backticks, no headings. The panel shows plain text, so Markdown would appear literally.
8. Write every user-facing string in {_language()}.
7. Your final reply is a single JSON object and nothing else."""


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
{{"action": "close", "conclusion": "one sentence: what got done, where the result is, what is left (at most {_limits()['conclusion']} characters)"}}"""


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
    return f"### This event (rounds used: {rounds}/3)\n{head}\n\nOpenCode's last reply:\n{reply or '(no text)'}"


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
        f"Rounds used: {rounds}/3. Decide the next step: relay if OpenCode should keep working; card if you can handle it without OpenCode or need to clarify first; close if the work is done.",
        language_line(),
        "",
        action_format(),
    ]
    return "\n".join(lines)


def mention_wake(text: str, rounds: int) -> str:
    return "\n".join(
        [
            f"The user mentioned this handoff in the main chat (the judge is confident): “{text}”",
            f"Rounds used: {rounds}/3. If it is a new requirement or a correction, relay; if they are asking for progress or need an answer from you, card; if they are saying it is done, close.",
            language_line(),
            "",
            action_format(),
        ]
    )


def close_wake(reply: str) -> str:
    extra = f"They added: {reply.strip()}\n" if reply.strip() else ""
    return f'The user closed this handoff on Docket. {extra}Give a one-sentence conclusion in {_language()}.\n\nReply only with: {{"action": "close", "conclusion": "…"}}'


# ---------- running and validating ----------

def run(session_key: str, message: str, timeout: int = TIMEOUT) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(message)
        path = handle.name
    try:
        done = subprocess.run(
            ["openclaw", "agent", "--session-key", session_key, *(["--model", MODEL] if MODEL else []), "--json", "--timeout", str(timeout), "--message-file", path],
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


def extract_json(text: str):
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fenced[::-1] + ([text[text.find("{") : text.rfind("}") + 1]] if "{" in text else [])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in the reply")


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
        return errors
    if kind == "card":
        return validate_card(action.get("card"))
    return ["action must be relay, card or close"]


def ask(session_key: str, message: str, validate, attempts: int = 2):
    """Wake the PM and return its validated JSON; one retry with the errors."""
    last_errors: list[str] = []
    prompt = message
    for _ in range(attempts):
        text = run(session_key, prompt)
        try:
            value = extract_json(text)
            last_errors = validate(value)
        except ValueError as error:
            value, last_errors = None, [str(error)]
        if not last_errors:
            return normalize(value)
        prompt = "Your last reply did not pass:\n- " + "\n- ".join(last_errors) + "\n\nFix it and reply again with only the JSON object."
    raise PMError("the PM's reply failed validation twice: " + "; ".join(last_errors))


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
