#!/usr/bin/env python3
"""Fake Docket service for building and checking Docket.app without the real one.

Serves the API in SPEC.md from memory and writes the real discovery file
~/Library/Application Support/docket/daemon.json by default. Refuses to start
if that file points at a daemon that is still reachable. Python 3.9, standard library only.

    python3 app/dev-daemon.py                 # one awaiting + one running delegation
    python3 app/dev-daemon.py --all           # also limit / taken_over / closed samples
    python3 app/dev-daemon.py --permission    # also an awaiting permission-request card
    python3 app/dev-daemon.py --locale zh     # "locale" field and sample copy in Chinese
    python3 app/dev-daemon.py --discovery app/build/dev-daemon.json
        # side by side with a real daemon; point a debug build at it with
        # Docket.app/Contents/MacOS/Docket -debugDiscoveryFile <absolute path>

After a decide, the delegation goes pending (3 s) -> running (10 s) -> a new card,
so the polling path can be watched end to end.
"""

import argparse
import copy
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

DEFAULT_DISCOVERY = os.path.expanduser("~/Library/Application Support/docket/daemon.json")
DISCOVERY = DEFAULT_DISCOVERY
STATE_ORDER = {"awaiting": 0, "pending": 1, "running": 2, "taken_over": 3, "closed": 4}

# Sample copy and error messages, per locale.
TEXT = {
    "en": {
        "monitor_title": "Add rate limiting to the public API",
        "monitor_progress": "Limits are in for all public routes and the 214 tests pass, including 9 new ones.",
        "monitor_q1": "Limit per API key or per IP?",
        "monitor_q1_a": ("Per API key", "Fair to users behind shared IPs"),
        "monitor_q1_b": ("Per IP", ""),
        "monitor_q2": "Ship it today?",
        "monitor_q2_a": ("Ship to staging first", "Watch a day of real traffic"),
        "monitor_q2_b": ("Ship to production", ""),
        "monitor_handled": [
            "Used the existing Redis instead of adding a store",
            "Returned 429 with a Retry-After header",
            "Left the admin routes unlimited",
        ],
        "coding_title": "Clean up duplicate translation keys",
        "coding_progress": "Merged 41 exact duplicates. Six keys differ in wording and need your call.",
        "coding_q1": "Merge “Sign in” and “Log in” into one key?",
        "coding_q1_a": ("Merge as “Sign in”", "Matches the rest of the app"),
        "coding_q1_b": ("Keep both", ""),
        "coding_handled": ["Kept the old keys as aliases for one release"],
        "coding_reply": "Merge, and note it in the changelog",
        "deps_title": "Upgrade the web app to React 20",
        "deps_progress": "After three rounds, two snapshot tests still fail on the new renderer.",
        "ocr_title": "Speed up the nightly export",
        "perm_title": "Clean up the scratch folder on the server",
        "perm_progress": "OpenCode wants to delete files outside the project; deleted files cannot be recovered.",
        "perm_q": "Allow it?",
        "perm_once": "Allow once",
        "perm_reject": "Reject",
        "zotero_title": "Write the migration guide for v3",
        "zotero_conclusion": "Guide is in docs/migrating-to-v3.md, reviewed against the changelog.",
        "next_progress": "The changes are in and the smoke tests pass.",
        "next_limit_progress": "That was the last round. The rest needs your eyes.",
        "next_q1": "Remove the old script while we're at it?",
        "next_q1_a": ("Remove it", "Nothing calls it anymore"),
        "next_q1_b": ("Keep it for now", ""),
        "next_handled": ["Kept the commits small, one per change"],
        "closed": "Closed.",
        "closed_reply": "Closed. You said: %s",
        "err_token": "Invalid token",
        "err_choices_type": "choices must be an object",
        "err_empty": "Pick an option or write a reply",
        "err_answered": "This card was already answered",
        "err_option": "Unknown option %s=%s",
        "err_card": "No such card",
        "err_delegation": "No such handoff",
        "err_route": "Not found",
        "err_body": "Request body must be a JSON object",
    },
    "zh": {
        "monitor_title": "给公开 API 加限流",
        "monitor_progress": "所有公开接口都加上了限流，214 项测试全过，其中 9 项是新写的。",
        "monitor_q1": "按 API key 限还是按 IP 限？",
        "monitor_q1_a": ("按 API key", "共用 IP 的用户不吃亏"),
        "monitor_q1_b": ("按 IP", ""),
        "monitor_q2": "今天就上线吗？",
        "monitor_q2_a": ("先上预发环境", "看一天真实流量"),
        "monitor_q2_b": ("直接上生产", ""),
        "monitor_handled": ["沿用现有的 Redis，不另加存储", "超限返回 429 并带 Retry-After", "管理后台接口不限流"],
        "coding_title": "清理重复的翻译键",
        "coding_progress": "合并了 41 个完全重复的键，剩下 6 个措辞不同，要你拍板。",
        "coding_q1": "「登录」和「登入」合并成一个键吗？",
        "coding_q1_a": ("合并为「登录」", "和应用其他地方一致"),
        "coding_q1_b": ("都保留", ""),
        "coding_handled": ["旧键保留为别名一个版本"],
        "coding_reply": "合并，并写进更新日志",
        "deps_title": "前端升级到 React 20",
        "deps_progress": "三轮下来还有两个快照测试过不了，卡在新渲染器上。",
        "ocr_title": "加快每晚的数据导出",
        "perm_title": "清理服务器上的临时目录",
        "perm_progress": "OpenCode 想删掉项目目录以外的文件，删掉就找不回来。",
        "perm_q": "允许吗？",
        "perm_once": "允许一次",
        "perm_reject": "拒绝",
        "zotero_title": "写 v3 迁移指南",
        "zotero_conclusion": "指南在 docs/migrating-to-v3.md，已对照更新日志核过。",
        "next_progress": "按你的意思改完了，冒烟测试全部通过。",
        "next_limit_progress": "已满轮数，剩下的需要你直接看。",
        "next_q1": "要不要顺手把旧脚本也删掉？",
        "next_q1_a": ("删", "已经没有调用方"),
        "next_q1_b": ("先留着", ""),
        "next_handled": ["提交拆小，一处改动一个提交"],
        "closed": "已结案。",
        "closed_reply": "已结案。你说：%s",
        "err_token": "token 不对",
        "err_choices_type": "choices 必须是对象",
        "err_empty": "至少选一项或回一句",
        "err_answered": "这张卡已经答过了",
        "err_option": "未知的选项 %s=%s",
        "err_card": "没有这张卡",
        "err_delegation": "没有这件交办",
        "err_route": "没有这个接口",
        "err_body": "请求体不是 JSON 对象",
    },
}


def now_ms():
    return int(time.time() * 1000)


def minutes_ago(n):
    return now_ms() - n * 60 * 1000


def option(option_id, pair, recommended=False):
    label, why = pair
    return {"id": option_id, "label": label, "why": why, "recommended": recommended}


def sample_delegations(t, include_all, include_permission=False):
    items = [
        {
            "id": "ses_dev_monitor01",
            "title": t["monitor_title"],
            "state": "awaiting",
            "rounds": 1,
            "round_limit": 3,
            "dispatched_at": minutes_ago(95),
            "updated_at": minutes_ago(12),
            "card": {
                "id": 12,
                "kind": "report",
                "open": True,
                "progress": t["monitor_progress"],
                "decisions": [
                    {
                        "id": "d1",
                        "question": t["monitor_q1"],
                        "options": [option("a", t["monitor_q1_a"], True), option("b", t["monitor_q1_b"])],
                    },
                    {
                        "id": "d2",
                        "question": t["monitor_q2"],
                        "options": [option("a", t["monitor_q2_a"], True), option("b", t["monitor_q2_b"])],
                    },
                ],
                "decided_for_you": list(t["monitor_handled"]),
                "conclusion": None,
            },
        },
        {
            "id": "ses_dev_coding02",
            "title": t["coding_title"],
            "state": "running",
            "rounds": 2,
            "round_limit": 3,
            "dispatched_at": minutes_ago(240),
            "updated_at": minutes_ago(3),
            "card": {
                "id": 9,
                "kind": "report",
                "open": False,
                "progress": t["coding_progress"],
                "decisions": [
                    {
                        "id": "d1",
                        "question": t["coding_q1"],
                        "options": [option("a", t["coding_q1_a"], True), option("b", t["coding_q1_b"])],
                    }
                ],
                "decided_for_you": list(t["coding_handled"]),
                "conclusion": None,
                "choices": {"d1": "a"},
                "reply": t["coding_reply"],
            },
        },
    ]
    if include_permission:
        items.append(
            {
                "id": "ses_dev_perm06",
                "title": t["perm_title"],
                "state": "awaiting",
                "rounds": 0,
                "round_limit": 3,
                "dispatched_at": minutes_ago(6),
                "updated_at": minutes_ago(1),
                "card": {
                    "id": 90,
                    "kind": "question",
                    "open": True,
                    "progress": t["perm_progress"],
                    "decisions": [
                        {
                            "id": "permission",
                            "question": t["perm_q"],
                            "options": [
                                option("once", (t["perm_once"], ""), True),
                                option("reject", (t["perm_reject"], "")),
                            ],
                        }
                    ],
                    "decided_for_you": [],
                    "conclusion": None,
                },
            }
        )
    if include_all:
        items += [
            {
                "id": "ses_dev_deps03",
                "title": t["deps_title"],
                "state": "awaiting",
                "rounds": 3,
                "round_limit": 3,
                "dispatched_at": minutes_ago(600),
                "updated_at": minutes_ago(6),
                "card": {
                    "id": 21,
                    "kind": "limit",
                    "open": True,
                    "progress": t["deps_progress"],
                    "decisions": [],
                    "decided_for_you": [],
                    "conclusion": None,
                },
            },
            {
                "id": "ses_dev_ocr04",
                "title": t["ocr_title"],
                "state": "taken_over",
                "rounds": 2,
                "round_limit": 3,
                "dispatched_at": minutes_ago(1500),
                "updated_at": minutes_ago(300),
                "card": None,
            },
            {
                "id": "ses_dev_zotero05",
                "title": t["zotero_title"],
                "state": "closed",
                "rounds": 1,
                "round_limit": 3,
                "dispatched_at": minutes_ago(2000),
                "updated_at": minutes_ago(1400),
                "card": {
                    "id": 5,
                    "kind": "closed",
                    "open": False,
                    "progress": "",
                    "decisions": [],
                    "decided_for_you": [],
                    "conclusion": t["zotero_conclusion"],
                },
            },
        ]
    return items


class World:
    def __init__(self, locale, include_all, include_permission=False):
        self.lock = threading.Lock()
        self.locale = locale
        self.t = TEXT[locale]
        self.delegations = sample_delegations(self.t, include_all, include_permission)
        self.next_card_id = 100
        self.timers = {}  # delegation id -> (running_at, card_at) in seconds

    def _find(self, delegation_id):
        for d in self.delegations:
            if d["id"] == delegation_id:
                return d
        return None

    def _advance(self):
        t = self.t
        now = time.time()
        for d in self.delegations:
            timer = self.timers.get(d["id"])
            if not timer:
                continue
            running_at, card_at = timer
            if d["state"] == "pending" and now >= running_at:
                d["state"] = "running"
                d["updated_at"] = now_ms()
            if d["state"] == "running" and now >= card_at:
                del self.timers[d["id"]]
                d["rounds"] += 1
                self.next_card_id += 1
                limit = d["rounds"] >= d["round_limit"]
                d["state"] = "awaiting"
                d["updated_at"] = now_ms()
                d["card"] = {
                    "id": self.next_card_id,
                    "kind": "limit" if limit else "question",
                    "open": True,
                    "progress": t["next_limit_progress"] if limit else t["next_progress"],
                    "decisions": []
                    if limit
                    else [
                        {
                            "id": "d1",
                            "question": t["next_q1"],
                            "options": [option("a", t["next_q1_a"], True), option("b", t["next_q1_b"])],
                        }
                    ],
                    "decided_for_you": list(t["next_handled"]),
                    "conclusion": None,
                }

    def state(self):
        with self.lock:
            self._advance()
            items = sorted(
                self.delegations,
                key=lambda d: (STATE_ORDER.get(d["state"], 9), -d.get("updated_at", 0)),
            )
            return {
                "locale": self.locale,
                "awaiting": sum(1 for d in items if d["state"] == "awaiting"),
                "delegations": copy.deepcopy(items),
            }

    def decide(self, card_id, body):
        t = self.t
        choices = body.get("choices") or {}
        reply = (body.get("reply") or "").strip()
        if not isinstance(choices, dict):
            return 400, {"error": t["err_choices_type"]}
        if not choices and not reply:
            return 400, {"error": t["err_empty"]}
        with self.lock:
            self._advance()
            for d in self.delegations:
                card = d.get("card")
                if not card or card["id"] != card_id:
                    continue
                if not card["open"]:
                    return 409, {"error": t["err_answered"]}
                options = {dec["id"]: {o["id"] for o in dec["options"]} for dec in card["decisions"]}
                for key, value in choices.items():
                    if key not in options or value not in options[key]:
                        return 400, {"error": t["err_option"] % (key, value)}
                card["open"] = False
                card["choices"] = choices
                card["reply"] = reply
                d["state"] = "pending"
                d["updated_at"] = now_ms()
                now = time.time()
                self.timers[d["id"]] = (now + 3, now + 13)
                print("decide card %d: choices=%s reply=%r" % (card_id, choices, reply), flush=True)
                return 202, {"ok": True}
        return 404, {"error": t["err_card"]}

    def close(self, delegation_id, body):
        t = self.t
        reply = (body.get("reply") or "").strip()
        with self.lock:
            d = self._find(delegation_id)
            if not d:
                return 404, {"error": t["err_delegation"]}
            self.timers.pop(delegation_id, None)
            self.next_card_id += 1
            previous = d.get("card") or {}
            d["state"] = "closed"
            d["updated_at"] = now_ms()
            d["card"] = {
                "id": self.next_card_id,
                "kind": "closed",
                "open": False,
                "progress": previous.get("progress", ""),
                "decisions": [],
                "decided_for_you": [],
                "conclusion": (t["closed_reply"] % reply) if reply else t["closed"],
            }
            print("close %s reply=%r" % (delegation_id, reply), flush=True)
            return 202, {"ok": True}

    def takeover(self, delegation_id):
        with self.lock:
            d = self._find(delegation_id)
            if not d:
                return 404, {"error": self.t["err_delegation"]}
            self.timers.pop(delegation_id, None)
            d["state"] = "taken_over"
            d["updated_at"] = now_ms()
            print("takeover %s (the real daemon would open it in OpenCode Desktop)" % delegation_id, flush=True)
            return 202, {"ok": True}

    def handback(self, delegation_id):
        with self.lock:
            d = self._find(delegation_id)
            if not d:
                return 404, {"error": self.t["err_delegation"]}
            if d["state"] != "taken_over":
                return 409, {"error": "not taken over"}
            d["state"] = "running"  # the real daemon goes to pending and wakes the PM
            d["handbacks"] = d.get("handbacks", 0) + 1
            d["updated_at"] = now_ms()
            print("handback %s" % delegation_id, flush=True)
            return 202, {"ok": True, "state": "pending"}


def make_handler(world, token):
    t = world.t

    class Handler(BaseHTTPRequestHandler):
        server_version = "docket-dev/0.2"

        def log_message(self, fmt, *args):
            if self.command != "GET":
                sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if self.headers.get("X-Docket-Token") != token:
                self._send(401, {"error": t["err_token"]})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def do_GET(self):
            if not self._authorized():
                return
            if urlparse(self.path).path == "/api/state":
                self._send(200, world.state())
            else:
                self._send(404, {"error": t["err_route"]})

        def do_POST(self):
            if not self._authorized():
                return
            body = self._body()
            if body is None:
                self._send(400, {"error": t["err_body"]})
                return
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
            if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[3] == "decide":
                try:
                    card_id = int(parts[2])
                except ValueError:
                    self._send(404, {"error": t["err_card"]})
                    return
                self._send(*world.decide(card_id, body))
            elif len(parts) == 4 and parts[:2] == ["api", "delegations"] and parts[3] == "close":
                self._send(*world.close(parts[2], body))
            elif len(parts) == 4 and parts[:2] == ["api", "delegations"] and parts[3] == "takeover":
                self._send(*world.takeover(parts[2]))
            elif len(parts) == 4 and parts[:2] == ["api", "delegations"] and parts[3] == "handback":
                self._send(*world.handback(parts[2]))
            else:
                self._send(404, {"error": t["err_route"]})

    return Handler


def existing_daemon_reachable():
    try:
        with open(DISCOVERY, encoding="utf-8") as f:
            url = json.load(f).get("url", "")
    except (OSError, ValueError, AttributeError):
        return None
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return None
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return url
    except OSError:
        return None


def write_discovery(url, token):
    os.makedirs(os.path.dirname(DISCOVERY), exist_ok=True)
    tmp = DISCOVERY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"url": url, "token": token}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, DISCOVERY)


def remove_discovery(token):
    try:
        with open(DISCOVERY, encoding="utf-8") as f:
            if json.load(f).get("token") != token:
                return  # someone else owns it now
        os.remove(DISCOVERY)
    except (OSError, ValueError):
        pass


def main():
    parser = argparse.ArgumentParser(description="Fake Docket service for Docket.app development")
    parser.add_argument("--port", type=int, default=0, help="port to bind (default: any free port)")
    parser.add_argument("--all", action="store_true", help="also serve limit / taken_over / closed samples")
    parser.add_argument("--permission", action="store_true", help="also serve a permission-request sample (as the real daemon would)")
    parser.add_argument("--locale", choices=sorted(TEXT), default="en", help="locale field and sample copy (default: en)")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY, help="discovery file to write (default: the real one)")
    args = parser.parse_args()

    global DISCOVERY
    DISCOVERY = os.path.abspath(os.path.expanduser(args.discovery))

    running = existing_daemon_reachable()
    if running:
        sys.exit("Refusing to start: %s points at a daemon that is still running (%s). Stop it first."
                 % (DISCOVERY, running))

    token = secrets.token_hex(16)
    world = World(args.locale, args.all, args.permission)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(world, token))
    url = "http://127.0.0.1:%d" % server.server_address[1]
    write_discovery(url, token)
    print("dev-daemon listening on %s (pid %d, locale %s)" % (url, os.getpid(), args.locale), flush=True)
    print("discovery: %s" % DISCOVERY, flush=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        remove_discovery(token)
        print("dev-daemon stopped", flush=True)


if __name__ == "__main__":
    main()
