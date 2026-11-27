"""Instructions that reach beyond the project: never sent to the coding agent without the person.

The coding agent runs with broad permissions, and what a PM relays can be shaped by text it read
(the agent's output, a repository file). So a relay that would push, rewrite history, publish,
deploy, delete outside the project, run a script from the network or touch the system is put
on a card first, unless the person's own words ask for exactly that. The patterns look for the
act, not the topic: "write the deploy script" is fine, "deploy to production" is not.
"""

from __future__ import annotations

import re

# (kind, what the instruction does, what the person's words must contain to have asked for it)
RISKS = (
    ("push", r"\bgit\s+push\b|\bpush\b[^.\n]{0,40}\b(?:origin|remote|upstream|main|master|github)\b"
             r"|(?:推送|推)到?\s*(?:远端|远程|origin|github|主分支)",
     r"push|推送|推上|推到|提交到远端|上传到 ?github"),
    ("history", r"--force\b|\bforce[- ]push\b|\breset\s+--hard\b|\bpush\s+-f\b",
     r"force|强推|强制推送|覆盖历史|reset"),
    ("publish", r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgh\s+release\s+create\b|\bdocker\s+push\b"
                r"|\bpublish\b[^.\n]{0,30}\b(?:npm|pypi|app store|testflight|registry|marketplace)\b"
                r"|发布到\s*(?:npm|pypi|app ?store|testflight|应用商店)|上架",
     r"publish|release|发布|上架|发版"),
    ("deploy", r"\b(?:vercel|netlify|fly|firebase|heroku|wrangler)\s+deploy\b|\bkubectl\s+apply\b|\bterraform\s+apply\b"
               r"|\bdeploy\b[^.\n]{0,30}\b(?:prod|production|server|live)\b|部署到\s*(?:生产|线上|服务器)|上线",
     r"deploy|部署|上线"),
    ("delete", r"\brm\s+-[a-z]*r[a-z]*\s+(?:~|/|\$HOME|\.\./)|\bdelete\b[^.\n]{0,30}\b(?:outside the project|home folder|~/)"
               r"|删除?(?:项目外|主目录|家目录)",
     r"delete|remove|删除|删掉|清理"),
    ("network", r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sh|bash|zsh|python3?)\b",
     r"curl|wget|下载.{0,6}运行"),
    ("system", r"\bsudo\b|\blaunchctl\b|\bcrontab\b|\bssh\s+[\w.-]+@|\bscp\s",
     r"sudo|launchctl|crontab|ssh|scp|系统设置|服务器"),
)
_COMPILED = tuple((kind, re.compile(act, re.I), re.compile(asked, re.I)) for kind, act, asked in RISKS)


def of(message: str | None) -> str | None:
    """The first kind of risk the instruction carries, or None."""
    for kind, act, _ in _COMPILED:
        if act.search(message or ""):
            return kind
    return None


def asked_for(kind: str, said: str | None) -> bool:
    """Whether the person's own words ask for this kind of act."""
    return bool(said) and any(k == kind and asked.search(said) for k, _, asked in _COMPILED)
