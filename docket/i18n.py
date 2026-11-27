"""Language for everything the docket shows or asks the PM to write.

`locale` in config.json: "auto" (default, follows the macOS preferred language), "en" or "zh".
"""

from __future__ import annotations

import subprocess

TEXT = {
    "en": {
        "language": "English",
        "plain_example": "write \"the signing settings are missing one value; I will fill it in\", not \"fix the Team ID in Config/Signing.local.xcconfig\"",
        "term_example": "a provisioning profile (the permission file installed on the phone)",
        "limit": "All {n} rounds used. Please take it over.",
        "err_limit": "All rounds are used: take it over or close it.",
        "docket_fault": "Docket itself failed, not the task; the work is not lost. {error}",
        "pm_failed": "Docket itself failed, not the task: the PM's reply could not be read after 3 tries. Its reply began: {raw}",
        "pm_retry_question": "Try again?",
        "pm_retry": "Retry",
        "remaining": "Left to do: {items}",
        "confirm_progress": "You said in chat: “{text}”. The PM would hand this to OpenCode.",
        "confirm_question": "Send this to OpenCode?",
        "confirm_yes": "Send",
        "confirm_yes_why": "That's what I meant",
        "confirm_no": "Not now",
        "confirm_draft": "Draft for OpenCode: {text}",
        "failed": "Docket hit an error: {error}",
        "missing_dir": "The dispatch directory does not exist: {raw} → {expanded}",
        "closed_fallback": "Done.",
        "closed_overdue": "When the docket restarted this card had been idle for {hours} hours, so it was closed as overdue. Dispatch it again if it is still needed.",
        "auto_expired": "The OpenCode session ended ({outcome}) and this card got no reply for {minutes} minutes, so the docket closed it as expired. The result is in the chat; dispatch it again if it is still needed.",
        "quiet_expired": "Nobody answered this card for {hours} hours, so it has expired and the docket closed the handoff. Dispatch it again if it is still needed.",
        "memory_title": "Docket · closed · {title}",
        "memory_kind": "Handoff closed (docket, OpenCode session {id}, {rounds} rounds)",
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
        "err_not_taken_over": "Only a handoff you have taken over can be handed back; this one is not taken over.",
        "session_gone": "The coding agent's session no longer exists, so this handoff cannot continue. Nothing was lost on Docket.",
        "err_empty": "Pick an option or write a reply.",
        "err_open_card": "Answer the card that is waiting first.",
        "permission_expired": "That run has already finished, so the permission is no longer needed.",
        "why_not_started": "OpenCode never started on the instruction",
        "why_quiet": "OpenCode went quiet for {minutes} minutes: no new message, no file changed",
        "why_unfinished": "OpenCode's run ended {outcome} before it finished",
        "stalled": "Stuck: {why}, and the docket's one automatic resend did not get it moving. Nothing was lost. Retry sends the same instruction again.",
        "stalled_question": "Send it again?",
        "orphan": "Stuck: the PM got a message outside Docket (from {source}) and answered with “{action}”, which nobody carried out.",
        "orphan_question": "Carry out the PM's answer now?",
        "orphan_run": "Carry it out",
        "orphan_skip": "Leave it",
        "resend_same": "(Docket: this instruction was sent before and {why}. Here it is again; carry on from where the project stands now.)\n\n{message}",
        "resend_new": "(Docket: this continues the work of OpenCode session {session}, where {why}; that session takes no more instructions. Files the last turn left behind:\n{files}\nBuild on them; do not start over.)\n\n{message}",
        "no_files": "(none changed)",
        "err_taken_over": "You have taken this handoff over: hand it back first, or write in OpenCode.",
        "err_note_empty": "Write what to pass on.",
    },
    "zh": {
        "language": "简体中文",
        "plain_example": "写「签名配置缺一项，我来补」，不写「修 Config/Signing.local.xcconfig 里的 Team ID」",
        "term_example": "描述文件（装到手机上的授权文件）",
        "limit": "已满 {n} 轮，请接管。",
        "err_limit": "轮次已用完，请接管或结案",
        "docket_fault": "案头自身出错，不是任务失败，工作没有丢。{error}",
        "pm_failed": "案头自身出错，不是任务失败：PM 的回复试了 3 次都读不出来。它的回复开头是：{raw}",
        "pm_retry_question": "重试一次？",
        "pm_retry": "重试",
        "remaining": "未完成：{items}",
        "confirm_progress": "你在聊天里说：「{text}」，PM 拟按此续派。",
        "confirm_question": "按这句话续派给 OpenCode？",
        "confirm_yes": "续派",
        "confirm_yes_why": "就是这件事",
        "confirm_no": "先不",
        "confirm_draft": "拟发给 OpenCode：{text}",
        "failed": "案头处理出错：{error}",
        "missing_dir": "派发目录不存在：{raw} → {expanded}",
        "closed_fallback": "已结案。",
        "closed_overdue": "守护进程重启时发现这张卡已停 {hours} 小时，按超期结案，需要的话可以重新派。",
        "auto_expired": "OpenCode 那一轮已经结束（{outcome}），这张卡 {minutes} 分钟没人回复，按过期自动结案。结论在对话里，需要的话可以重新派。",
        "quiet_expired": "这张卡 {hours} 小时无人回答，已过期，案头已结案。需要的话可以重新派。",
        "memory_title": "案头结案 · {title}",
        "memory_kind": "交办结案（来源：docket，OpenCode 会话 {id}，{rounds} 轮批示）",
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
        "err_not_taken_over": "只有已接管的交办才能交还，这件现在不是接管状态",
        "session_gone": "编程代理那边的会话已经不存在了，这件交办无法继续；案头这边没有丢东西。",
        "err_empty": "选一个选项或回一句话",
        "err_open_card": "先把正在等你的那张卡处理掉",
        "permission_expired": "那一轮已经结束了，这次授权不再需要。",
        "why_not_started": "OpenCode 一直没有开始执行这条指令",
        "why_quiet": "OpenCode {minutes} 分钟没有新消息、没有改动任何文件",
        "why_unfinished": "OpenCode 这一轮没做完就结束了（{outcome}）",
        "stalled": "卡住了：{why}，案头自动重派一次也没能让它动起来。工作没有丢，点重试会把同一条指令再派一次。",
        "stalled_question": "再派一次？",
        "orphan": "卡住了：PM 在案头之外收到一条消息（来自 {source}），回复了「{action}」，但没有人执行。",
        "orphan_question": "现在按 PM 这条回复执行？",
        "orphan_run": "执行",
        "orphan_skip": "不执行",
        "resend_same": "（案头：这条指令之前发过，{why}。现在重发一次，请从项目当前的状态接着做。）\n\n{message}",
        "resend_new": "（案头：这是接着 OpenCode 会话 {session} 的工作，那边{why}，那个会话已经不再接受指令。上一轮留下的文件：\n{files}\n请在这些改动的基础上继续，不要从头重做。）\n\n{message}",
        "no_files": "（没有文件改动）",
        "err_taken_over": "这件交办已被你接管：先交还，或直接在 OpenCode 里说。",
        "err_note_empty": "写一句要转达的话",
    },
}

# Text limits are in characters; English needs roughly twice the room of Chinese.
LIMITS = {
    # progress and next are the card's two sentences; body caps the two together
    "en": {"progress": 100, "next": 100, "body": 200, "question": 80, "label": 24, "why": 80, "decided": 120, "conclusion": 125},
    "zh": {"progress": 40, "next": 40, "body": 80, "question": 40, "label": 8, "why": 40, "decided": 60, "conclusion": 50},
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
