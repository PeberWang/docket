"""Read an OpenClaw session transcript as a flat list of user/assistant messages.

OpenClaw keeps transcripts in the agent SQLite (`transcript_events`). Two event shapes exist:

  2026.9.x   {"message": {"role": "user" | "assistant", "content": [...]}}
             tool calls are assistant content parts {"type": "toolCall", "name", "arguments"}
  2026.11.x  {"message": {"role": "custom", "customType": "openclaw…", ...}}
             the real message sits inside the custom event (message / data / payload / details),
             or on it directly with the role implied by customType; tool calls may be content
             parts, a `toolCalls` list, or the event itself, with arguments as an object or a
             JSON string and the command as a string or an argv list.

`normalize` folds both into (role, text, calls); everything else here goes through it.
Only text and tool calls are kept: tool results and thinking are dropped, because the
judge decides relevance on what the user and the agent actually said to each other.

Where the agent database lives: 2026.11 maps each agent to its database in
~/.openclaw/state/openclaw.sqlite (`agent_databases`); that is asked first, and the
2026.9 path is the fallback. Neither database is ever written.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

OPENCLAW = Path.home() / ".openclaw"
AGENT_DB = OPENCLAW / "agents/main/agent/openclaw-agent.sqlite"
STATE_DB = OPENCLAW / "state/openclaw.sqlite"
AGENT = "main"
RETRY_DELAY = 1.0

# "[message_id: om_x]\nou_x: text" -> "text"
_FEISHU_PREFIX = re.compile(r"^\[message_id: [^\]]+\]\s*\n(?:ou_[A-Za-z0-9]+|[^\n:]{1,40}):\s*", re.S)
ROLES = ("user", "assistant")
CALL_TYPES = {"toolcall", "tool_call", "tooluse", "tool_use", "functioncall", "function_call"}
INNER_KEYS = ("message", "data", "payload", "details", "value")
USER_WORDS = {"user", "inbound", "human"}
ASSISTANT_WORDS = {"assistant", "reply", "outbound", "tool", "toolcall", "tooluse", "exec"}
# Windows of one conversation that 2026.11 keys separately; they are the same session.
KEY_VARIANTS = (":heartbeat",)


class Unavailable(RuntimeError):
    """The transcript database cannot be opened; the message is meant for a person."""

    def __init__(self, path: Path, source: str, error: Exception):
        super().__init__(f"cannot open the OpenClaw transcript database {path} (from {source}): {error}")
        self.path, self.source, self.error = path, source, error


@dataclass
class Message:
    seq: int
    role: str  # "user" | "assistant"
    time: int  # epoch ms
    text: str
    tool_calls: list  # assistant only: [{"name", "arguments", "command"}]


# ---------- where the database is ----------


def _columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"pragma table_info({table})")]


def _pick(columns: list[str], names: tuple) -> str | None:
    return next((c for c in names if c in columns), None)


def _expand(raw: str, base: Path) -> Path:
    path = Path(os.path.expanduser(str(raw).replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))))
    return Path(os.path.normpath(path if path.is_absolute() else base / path))


def mapped_db(state: Path = STATE_DB, agent: str = AGENT) -> Path | None:
    """The agent's database as `agent_databases` names it, if the state database says so."""
    if not state.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        columns = _columns(conn, "agent_databases")
        path_col = _pick(columns, ("path", "db_path", "database_path", "sqlite_path", "file", "location"))
        agent_col = _pick(columns, ("agent_id", "agent", "agent_name", "name", "id"))
        if not path_col:
            return None
        if agent_col:
            rows = conn.execute(f"select {path_col} from agent_databases where {agent_col} = ?", (agent,)).fetchall()
        else:
            rows = conn.execute(f"select {path_col} from agent_databases").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    candidates = []
    for (raw,) in rows:
        if not raw:
            continue
        for base in (state.parent, state.parent.parent):  # relative paths: next to the state db, or under ~/.openclaw
            candidates.append(_expand(raw, base))
    # Several rows per agent (agent, memory, …): the one holding transcripts wins, then any that exists.
    existing = [p for p in candidates if p.is_file()]
    for path in existing:
        if _has_transcripts(path):
            return path
    return existing[0] if existing else (candidates[0] if candidates else None)


def _has_transcripts(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return bool(conn.execute("select 1 from sqlite_master where name = 'transcript_events'").fetchone())
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def resolve(state: Path | None = None, fallback: Path | None = None) -> tuple[Path, str]:
    """(path, where it came from): `DOCKET_OPENCLAW_DB`, then agent_databases, then the 2026.9 path."""
    override = os.environ.get("DOCKET_OPENCLAW_DB")
    if override:
        return Path(override), "DOCKET_OPENCLAW_DB"
    state = state or STATE_DB
    fallback = fallback or AGENT_DB
    mapped = mapped_db(state)
    if mapped and mapped.is_file():
        return mapped, "agent_databases"
    if mapped:
        return fallback, f"fallback (agent_databases names {mapped}, which does not exist)"
    return fallback, "fallback"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the transcript database read-only, retrying once; raise Unavailable with a readable reason."""
    if path is None:
        path, source = resolve()
    else:
        path, source = Path(path), "given path"
    error: Exception | None = None
    for attempt in range(2):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            # Opening is lazy: touch the table so a missing or locked file fails here, not mid-scan.
            conn.execute("select 1 from transcript_events limit 1").fetchall()
            return conn
        except sqlite3.Error as caught:
            error = caught
            try:
                conn.close()
            except Exception:
                pass
            if attempt == 0:
                time.sleep(RETRY_DELAY)
    raise Unavailable(path, source, error)


# ---------- session keys ----------


def canonical_key(key: str) -> str:
    """`agent:main:main:heartbeat` is a window of `agent:main:main`, not another conversation."""
    for suffix in KEY_VARIANTS:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def session_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """{session_id: canonical session key}; a session seen under several windows gets its plain key."""
    out: dict[str, str] = {}
    for key, session_id in conn.execute("select session_key, session_id from session_windows order by rowid"):
        if not key or not session_id:
            continue
        plain = canonical_key(key)
        if session_id not in out or key == plain:
            out[session_id] = plain
    return out


def session_id_for(conn: sqlite3.Connection, key: str) -> str | None:
    """The conversation a session key names now: of the sessions filed under the key itself (or, failing
    that, its windows such as :heartbeat), the one with the newest transcript event. The last window
    registered is not it: a heartbeat window registers after the chat it belongs to, so `docket note`
    used to claim a message there while the judge read the real one (2026-11-27)."""
    key = canonical_key(key)
    for keys in ([key], [key + suffix for suffix in KEY_VARIANTS]):
        marks = ",".join("?" * len(keys))
        ids = [r[0] for r in conn.execute(f"select session_id from session_windows where session_key in ({marks})", keys)]
        if not ids:  # 2026.11 stores keys lowercased ("agent:main:docket-de70…"), the docket keeps the session id's case
            ids = [r[0] for r in conn.execute(f"select session_id from session_windows where lower(session_key) in ({marks})",
                                              [k.lower() for k in keys])]
        ids = list(dict.fromkeys(i for i in ids if i))
        if not ids:
            continue
        if len(ids) == 1:
            return ids[0]
        marks = ",".join("?" * len(ids))
        row = conn.execute(f"select session_id from transcript_events where session_id in ({marks}) "
                           "group by session_id order by max(created_at) desc limit 1", ids).fetchone()
        return row[0] if row else ids[-1]
    return None


# ---------- one event ----------


def _words(custom_type: str) -> set:
    return set(re.split(r"[^a-z0-9]+", custom_type.lower()))


def _role_from(custom_type: str) -> str | None:
    words = _words(custom_type)
    if words & USER_WORDS:
        return "user"
    if words & ASSISTANT_WORDS:
        return "assistant"
    return None


def _looks_like_message(node: dict) -> bool:
    return any(k in node for k in ("content", "text", "toolCalls", "tool_calls")) or _is_call(node)


def _is_call(node: dict) -> bool:
    kind = str(node.get("type") or "").lower()
    return kind in CALL_TYPES or (bool(node.get("toolName") or node.get("tool")) and any(k in node for k in ("arguments", "input", "args", "params", "command")))


def _unwrap(event: dict) -> tuple[str | None, dict]:
    """(role, the node that holds content) for either event shape."""
    msg = event.get("message") if isinstance(event.get("message"), dict) else event
    role = msg.get("role")
    if role in ROLES:
        return role, msg
    if role != "custom" and event.get("type") != "custom":
        return None, msg
    custom_type = str(msg.get("customType") or event.get("customType") or "")
    for key in INNER_KEYS:
        inner = msg.get(key)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                continue
        if not isinstance(inner, dict):
            continue
        if isinstance(inner.get("message"), dict):
            inner = inner["message"]
        if inner.get("role") in ROLES:
            return inner["role"], inner
        if _looks_like_message(inner):
            return _role_from(custom_type), inner
    return _role_from(custom_type), msg


def _text_of(node: dict) -> str:
    content = node.get("content")
    if isinstance(content, str):
        return content
    parts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text")]
    if not parts and isinstance(node.get("text"), str):
        parts = [node["text"]]
    return "\n".join(p for p in parts if p)


def _arguments(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {"command": raw}
    return raw if isinstance(raw, dict) else {}


def command_of(arguments: dict) -> str:
    """The shell command line in a tool call's arguments, whatever shape it came in."""
    for key in ("command", "cmd", "commandLine", "command_line", "script"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            argv = [str(v) for v in value]
            for flag in ("-c", "-lc", "-ic", "-lic"):  # ["bash", "-lc", "cd … && desk-dispatch …"]
                if flag in argv[:-1]:
                    return argv[argv.index(flag) + 1]
            return shlex.join(argv)
    return ""


def _call(node: dict) -> dict:
    function = node.get("function") if isinstance(node.get("function"), dict) else {}
    name = node.get("name") or node.get("toolName") or node.get("tool") or function.get("name")
    raw = next((node[k] for k in ("arguments", "input", "args", "params") if k in node), function.get("arguments"))
    arguments = _arguments(raw) if raw is not None else {k: node[k] for k in ("command", "cmd") if k in node}
    return {"name": name, "arguments": arguments, "command": command_of(arguments)}


def _calls_of(node: dict) -> list:
    calls = []
    content = node.get("content")
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and str(part.get("type") or "").lower() in CALL_TYPES:
            calls.append(_call(part))
    for key in ("toolCalls", "tool_calls"):
        for item in node.get(key) or []:
            if isinstance(item, dict):
                calls.append(_call(item))
    if not calls and _is_call(node):  # the custom event is the tool call itself
        calls.append(_call(node))
    return calls


def normalize(event: dict) -> tuple[str | None, str, list]:
    """(role, text, tool calls) of one transcript event; role None means it is not a message."""
    role, node = _unwrap(event)
    if role not in ROLES:
        return None, "", []
    text = _text_of(node)
    if role == "user":
        text = _FEISHU_PREFIX.sub("", text, count=1)
    calls = _calls_of(node) if role == "assistant" else []
    return role, text.strip(), calls


# ---------- a session ----------

MESSAGE_FILTER = "json_extract(event_json, '$.message.role') in ('user', 'assistant', 'custom')"


def messages(conn: sqlite3.Connection, session_id: str, since: int = 0, until: int | None = None, after_seq: int | None = None) -> list[Message]:
    query = f"select seq, created_at, event_json from transcript_events where session_id = ? and created_at >= ? and {MESSAGE_FILTER}"
    args: list = [session_id, since]
    if until is not None:
        query += " and created_at <= ?"
        args.append(until)
    if after_seq is not None:
        query += " and seq > ?"
        args.append(after_seq)
    query += " order by seq"
    out = []
    for seq, created, raw in conn.execute(query, args):
        try:
            role, text, calls = normalize(json.loads(raw))
        except (ValueError, AttributeError, TypeError):
            continue
        if role is None or (not text and not calls):
            continue
        out.append(Message(seq=seq, role=role, time=created, text=text, tool_calls=calls))
    return out
