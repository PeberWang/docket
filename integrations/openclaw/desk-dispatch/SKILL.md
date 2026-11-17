---
name: desk-dispatch
description: Hand a coding task to a coding agent (OpenCode by default, or Claude Code / Codex) and let OpenClaw Desk follow it. Use for multi-step technical work in a code project, whenever the user asks you to delegate, hand off or "have the coding agent do it". After dispatching, reply with one line and stop; the Desk handles verification and follow-ups.
---

# desk-dispatch

OpenClaw Desk is a native macOS panel between you and the coding agent. Work you hand off moves forward there as short briefs the user decides on, not in this chat. Your part ends when the task is started.

## Start a task

Write the brief to a file, then run from the project directory:

```sh
cd <project dir> && ~/.openclaw/workspace/skills/desk-dispatch/scripts/desk-dispatch \
  --title "<short task name>" -f <brief file> "<one-line instruction>"
```

- `--title` is required. Keep it short and specific.
- `--engine opencode|claude|codex` picks the agent. Leave it out to use the user's default (OpenCode unless they set another).
- `--auto` lets OpenCode approve its own permission requests (OpenCode only; Claude Code and Codex use the Desk's unattended settings).
- `--model <id>` passes a model to the agent.
- The script returns at once with one line: `dispatched in background: …`. You get no completion notice, on purpose.

## Then

Reply to the user with one line, for example "Handed off: <task name>. Follow-ups are on the Desk." Do not wait for the result, do not report progress in the chat, and do not continue the task yourself. If the user asks about it later, answer in one sentence and point to the Desk.

## Don't

- Don't call `opencode run`, `claude -p` or `codex exec` yourself for a handed-off task.
- Don't send follow-ups with this script (`-s` is refused). The Desk sends them after the user decides.
- Inside a Desk PM session (`agent:main:desk-*`) never dispatch new work.

## Requirements

- OpenClaw Desk running (its service writes `~/Library/Application Support/openclaw-desk/daemon.json`).
- For OpenCode: the `opencode-dispatch` skill next to this one (from opencode-openclaw-inbox).
- For Claude Code or Codex: the `claude` or `codex` CLI installed and logged in.
