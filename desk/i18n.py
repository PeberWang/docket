"""Language for everything the desk shows or asks the PM to write.

`locale` in config.json: "auto" (default, follows the macOS preferred language), "en" or "zh".
"""

from __future__ import annotations

import subprocess

TEXT = {
    "en": {
        "language": "English",
        "limit": "{n} rounds used. Take it over in OpenCode.",
        "confirm_progress": "You said in chat: “{text}”. The PM would hand this to OpenCode.",
        "confirm_question": "Send this to OpenCode?",
        "confirm_yes": "Send",
        "confirm_yes_why": "That's what I meant",
        "confirm_no": "Not now",
        "confirm_draft": "Draft for OpenCode: {text}",
        "failed": "The desk hit an error: {error}",
        "closed_fallback": "Done.",
        "memory_title": "Desk · closed · {title}",
        "memory_kind": "Handoff closed (openclaw-desk, OpenCode session {id}, {rounds} rounds)",
        "memory_body": "Outcome",
        "permission_sentence": "OpenCode wants to {action}; {risk}.",
        "permission_delete_temp": "delete some temporary files outside the project",
        "permission_delete_files": "delete files outside the project",
        "permission_read_files": "read files outside the project",
        "permission_run_command": "run a command outside the project",
        "permission_risk_delete": "deleted files cannot be recovered",
        "permission_risk_change": "files there may change",
        "permission_risk_read": "it only reads, nothing changes",
        "permission_question": "Allow it?",
        "permission_once": "Allow once",
        "permission_reject": "Reject",
        "err_answered": "This card has already been answered.",
        "err_over": "This handoff has ended.",
        "err_closed": "This handoff is already closed.",
        "err_missing": "No such handoff.",
        "session_gone": "The coding agent's session no longer exists, so this handoff cannot continue. Nothing was lost on the desk.",
        "err_empty": "Pick an option or write a reply.",
        "err_open_card": "Answer the card that is waiting first.",
        "permission_expired": "That run has already finished, so the permission is no longer needed.",
    },
    "zh": {
        "language": "简体中文",
        "limit": "已满 {n} 轮，建议直接去 OpenCode 接管。",
        "confirm_progress": "你在聊天里说：「{text}」，PM 拟按此续派。",
        "confirm_question": "按这句话续派给 OpenCode？",
        "confirm_yes": "续派",
        "confirm_yes_why": "就是这件事",
        "confirm_no": "先不",
        "confirm_draft": "拟发给 OpenCode：{text}",
        "failed": "案头处理出错：{error}",
        "closed_fallback": "已结案。",
        "memory_title": "案头结案 · {title}",
        "memory_kind": "交办结案（来源：openclaw-desk，OpenCode 会话 {id}，{rounds} 轮批示）",
        "memory_body": "内容",
        "permission_sentence": "它想{action}，{risk}。",
        "permission_delete_temp": "删掉项目外的一些临时文件",
        "permission_delete_files": "删掉项目外的一些文件",
        "permission_read_files": "读项目目录以外的文件",
        "permission_run_command": "在项目目录以外执行一条命令",
        "permission_risk_delete": "删掉就找不回来",
        "permission_risk_change": "那里的文件可能被改动",
        "permission_risk_read": "只是读取，不会改动",
        "permission_question": "允许吗？",
        "permission_once": "允许一次",
        "permission_reject": "拒绝",
        "err_answered": "这张卡片已经处理过了",
        "err_over": "这件交办已经结束",
        "err_closed": "这件交办已经结案",
        "err_missing": "没有这件交办",
        "session_gone": "编程代理那边的会话已经不存在了，这件交办无法继续；案头这边没有丢东西。",
        "err_empty": "选一个选项或回一句话",
        "err_open_card": "先把正在等你的那张卡处理掉",
        "permission_expired": "那一轮已经结束了，这次授权不再需要。",
    },
}

# Text limits are in characters; English needs roughly twice the room of Chinese.
LIMITS = {
    "en": {"progress": 160, "question": 80, "label": 40, "why": 80, "decided": 120, "conclusion": 200},
    "zh": {"progress": 80, "question": 40, "label": 20, "why": 40, "decided": 60, "conclusion": 100},
}


def system_locale() -> str:
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "en"
    first = next((part.strip(' "\n') for part in out.strip("()\n ").split(",") if part.strip(' "\n')), "en")
    return "zh" if first.lower().startswith("zh") else "en"


def resolve(setting: str | None) -> str:
    return setting if setting in TEXT else system_locale()


class Strings:
    def __init__(self, locale: str):
        self.locale = locale
        self.table = TEXT[locale]
        self.limits = LIMITS[locale]

    def __call__(self, key: str, **values) -> str:
        return self.table[key].format(**values)
