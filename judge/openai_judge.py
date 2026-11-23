"""The judge on an OpenAI-compatible /chat/completions endpoint (stdlib only).

It takes the System One request (state + noul/choice questions) and returns the System One
answer shape, so relevance.py reads it unchanged:

    noul    {"noul": p}                                  p = P(the "true" criterion holds)
    choice  {"choice": id, "probabilities": {...}, "confidence": max p}

A language model's stated probabilities are not calibrated the way System One's are; the
thresholds in config.json were set against System One (see HANDOFF.md for the measured gap).
A reply that is not the JSON asked for is asked again, twice; then the request fails like a
System One request that failed, and the caller logs it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from judge.backend import JudgeError, scrub

PARSE_RETRIES = 2
TIMEOUT = 120

SYSTEM = (
    "You are a careful classifier for a task-tracking program. You answer judgment questions about the JSON "
    "`state` you are given, using only what it says. Reply with one JSON object and nothing else: no prose, "
    "no code fence."
)

FORMAT = """Answer every question in `questions`, keyed by its id, in this shape:

{"answers": {"<question id>": <answer>, ...}}

- For a question with "type": "noul", the answer is {"probability": p}: p is a number from 0 to 1, how likely the
  "true" criterion holds rather than the "false" one. Use values near 0 or 1 only when the state makes it plain.
- For a question with "type": "choice", the answer is {"probabilities": {"<option>": p, ...}} with one entry for
  every key of its "criteria", each p from 0 to 1, summing to 1.

Every question id must be present. Reply with the JSON object only."""


class ReplyError(ValueError):
    pass


def url(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"


def extract(text: str) -> dict:
    """The JSON object in a reply, tolerating a code fence or a line of prose around it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ReplyError("no JSON object in the reply")
    try:
        value = json.loads(text[start : end + 1])
    except ValueError as error:
        raise ReplyError(f"the reply is not valid JSON: {error}") from None
    if not isinstance(value, dict):
        raise ReplyError("the reply is not a JSON object")
    return value


def _number(value) -> float:
    if isinstance(value, dict):
        value = value.get("probability", value.get("noul", value.get("p")))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ReplyError(f"not a probability: {value!r}"[:200])
    return float(value)


def _probability(value) -> float:
    value = _number(value)
    return value / 100 if 1 < value <= 100 else min(1.0, value)  # "85" for 85% happens


def answers(reply: dict, questions: dict) -> dict:
    """Map the model's answers onto System One's shape; ReplyError when one is missing or malformed."""
    given = reply.get("answers", reply)
    if not isinstance(given, dict):
        raise ReplyError("`answers` is not an object")
    out = {}
    for qid, question in questions.items():
        if qid not in given:
            raise ReplyError(f"no answer for {qid}")
        answer = given[qid]
        if question["type"] == "noul":
            out[qid] = {"noul": _probability(answer)}
            continue
        options = list(question["criteria"])
        if isinstance(answer, dict) and isinstance(answer.get("probabilities"), dict):
            # Weights, not clamped: they are normalized below, so 75/25 and 0.75/0.25 read the same.
            raw = {option: _number(answer["probabilities"].get(option, 0)) for option in options}
        elif isinstance(answer, dict) and answer.get("choice") in options:
            raw = {option: float(option == answer["choice"]) for option in options}
        elif isinstance(answer, str) and answer in options:
            raw = {option: float(option == answer) for option in options}
        else:
            raise ReplyError(f"no probabilities for {qid}")
        total = sum(raw.values())
        if total <= 0:
            raise ReplyError(f"all probabilities are zero for {qid}")
        probabilities = {option: p / total for option, p in raw.items()}
        choice = max(probabilities, key=probabilities.get)
        out[qid] = {"choice": choice, "probabilities": probabilities, "confidence": probabilities[choice]}
    return out


def _post(request: urllib.request.Request, key: str, retries: int) -> dict:
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = scrub(error.read().decode("utf-8", "replace")[:500], key)
            if (error.code == 429 or error.code >= 500) and attempt < retries:
                time.sleep(float(error.headers.get("retry-after") or 2 ** attempt))
                continue
            raise JudgeError(f"HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise JudgeError(scrub(str(getattr(error, "reason", error)), key)) from None
        except ValueError:
            raise JudgeError("the endpoint did not return JSON") from None
    raise JudgeError("unreachable")


def ask(state, questions: dict, model: str, endpoint: str, key: str, retries: int = 4) -> dict:
    """Same contract as jev.ask: return {"answers", "usage", "model"}."""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps({"state": state, "questions": questions}, ensure_ascii=False) + "\n\n" + FORMAT},
    ]
    usage = {"input_tokens": 0, "output_tokens": 0}
    problem = ""
    for _ in range(PARSE_RETRIES + 1):
        body = json.dumps({"model": model, "messages": messages, "temperature": 0}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url(endpoint),
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        result = _post(request, key, retries)
        spent = result.get("usage") or {}
        usage["input_tokens"] += spent.get("prompt_tokens", 0) or 0
        usage["output_tokens"] += spent.get("completion_tokens", 0) or 0
        try:
            text = result["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise JudgeError("the endpoint's reply has no choices[0].message.content") from None
        try:
            return {"answers": answers(extract(text), questions), "usage": usage, "model": result.get("model", model)}
        except ReplyError as error:
            problem = str(error)
            messages = messages[:2] + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": f"That reply could not be used ({problem}). Reply again with the JSON object only, every question id present."},
            ]
    raise JudgeError(f"the model's reply could not be used after {PARSE_RETRIES + 1} tries: {problem}"[:300])
