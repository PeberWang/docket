"""What card text may say, checked in code: plain words, no paths or code, short.

The PM is told the rules in its prompt; `jargon` and `sentences` catch the replies that
ignore them, so the retry can quote the sentence and say what is wrong with it. `short_title`
keeps the card head to one glance whatever name the dispatcher picked.
"""

from __future__ import annotations

import re
import unicodedata

# Each pattern is a thing the user should never have to read on a card, with why, for the retry.
_WORD = r"[A-Za-z0-9_\-]"
JARGON = (
    (re.compile(r"https?://\S+|\bwww\.\S+"), "a URL"),
    (re.compile(rf"(?<![A-Za-z0-9])[~.]{{0,2}}/{_WORD}[\w.\-]*"), "a file path"),
    (re.compile(rf"{_WORD}+(?:/{_WORD}[\w.\-]*){{2,}}"), "a file path"),
    (re.compile(rf"{_WORD}+/(?=\s|$|[，。、；：！？,.;:!?）)])"), "a directory name"),
    (re.compile(r"[A-Za-z0-9_\-]\.(?:swift|py|md|json|sh|ts|tsx|yaml|yml|toml|xcconfig|plist|txt|html|css|sql|db|log|ini|env|lock|rb|go|rs|kt|java|cpp)(?![A-Za-z0-9])", re.I), "a file name"),
    (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\([^()㐀-鿿]*\)"), "a function call"),
    (re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"), "a variable or function name"),
    (re.compile(r"\$|\bsudo\b"), "a shell command"),
    (re.compile(r"(?<!\S)--[a-z][a-z\-]*"), "a command-line flag"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\bline \d+\b|第\s*\d+\s*行", re.I), "an error code or line number"),
)


def _whole(text: str, start: int, end: int) -> str:
    """The match widened to the whole Latin token around it: tools/build.sh, not d.sh."""
    token = lambda ch: ch.isascii() and not ch.isspace() and ch not in ",;\"'"
    while start > 0 and token(text[start - 1]):
        start -= 1
    while end < len(text) and token(text[end]):
        end += 1
    piece = text[start:end].rstrip(",.;:!?")
    return piece if "(" in piece else piece.rstrip(")")


def jargon(text: str) -> list[tuple[str, str]]:
    """Every (what was found, what it is) in the text that does not belong on a card."""
    found, seen = [], set()
    text = text or ""
    for pattern, what in JARGON:
        for match in pattern.finditer(text):
            piece = _whole(text, match.start(), match.end())
            if piece and not any(piece in s or s in piece for s in seen):
                seen.add(piece)
                found.append((piece, what))
    return found


_END = re.compile(r"[。！？!?]|\.(?=\s|$)")


def sentences(text: str) -> int:
    """How many sentences the text holds: split at sentence ends, a decimal point is not one."""
    return sum(1 for part in _END.split((text or "").strip()) if part.strip(" \t\n，,、）)\"”'"))


def width(text: str) -> int:
    """Display width: a Chinese character or full-width mark counts 2, everything else 1."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def body(progress: str | None, following: str | None) -> str | None:
    """The card's two sentences as one text, with the stop between them the first one may lack."""
    if not following:
        return progress
    if not progress:
        return following
    last = progress.rstrip()[-1]
    if last in "。！？；…":
        return progress.rstrip() + following
    if last in ".!?;":
        return f"{progress.rstrip()} {following}"
    return progress.rstrip() + ("。" if width(last) == 2 else ". ") + following


TITLE_WIDTH = 24  # 12 Chinese characters, or 24 Latin ones
_TOKENS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#'_\-]*|\s+|.")


def short_title(title: str, limit: int = TITLE_WIDTH) -> str:
    """The title cut to `limit` at a word boundary, with an ellipsis. Latin words are never split;
    a run of Chinese is cut back to the last space or mark when one is near, else between characters."""
    title = " ".join((title or "").split())
    if width(title) <= limit:
        return title
    kept, used, boundary = [], 0, 0
    for token in _TOKENS.findall(title):
        if used + width(token) > limit - 1:  # room for the ellipsis
            break
        kept.append(token)
        used += width(token)
        if token.isspace() or not token[0].isalnum() or token[0].isascii():
            boundary = len(kept)  # a space, a mark, or a whole Latin word: a clean place to stop
    if boundary and sum(width(t) for t in kept[:boundary]) >= limit // 2:
        kept = kept[:boundary]
    return "".join(kept).rstrip(" ，、,;；:：-—·") + "…"
