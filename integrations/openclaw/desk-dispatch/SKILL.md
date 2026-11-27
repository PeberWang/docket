---
name: desk-dispatch
description: Hand a coding task to a coding agent (OpenCode by default, or Claude Code / Codex) and let OpenClaw Docket follow it. Use for multi-step technical work in a code project, whenever the user asks you to delegate, hand off or "have the coding agent do it". After dispatching, reply with one line and stop; Docket handles verification and follow-ups.
---

# desk-dispatch

OpenClaw Docket is a native macOS panel between you and the coding agent. Work you hand off moves forward there as short briefs the user decides on, not in this chat. Your part ends when the task is started.

## Start a task

Write the brief to a file, then run from the project directory:

```sh
cd <project dir> && ~/.openclaw/workspace/skills/desk-dispatch/scripts/desk-dispatch \
  --title "<short task name>" -f <brief file> "<one-line instruction>"
```

- `--title` is required. Keep it short and specific: at most 12 Chinese characters or 24 Latin ones, a plain name for the work, no version numbers, paths or tech stack (「健身记录 App」, not 「健身 FitLog v0.1 原生 iPhone 训练应用」). Docket cuts a longer one with an ellipsis on the card.
- `--engine opencode|claude|codex` picks the agent. Leave it out to use the user's default (OpenCode unless they set another).
- `--auto` lets OpenCode approve its own permission requests (OpenCode only; Claude Code and Codex use Docket's unattended settings). OpenCode 2.2 and later runs unattended anyway: Docket answers the requests itself, approving what is inside the project and putting writes outside it on a card.
- `--model <id>` passes a model to the agent.
- `--supersedes <handoff id>` redoes earlier work: the new handoff replaces that one, which Docket stops and closes as "replaced by …". Use it whenever the user wants the same thing done again or differently, instead of a second, unrelated handoff.
- The script returns at once with one line: `dispatched in background: …`. You get no completion notice, on purpose.

## Then

Reply to the user with one line, for example "Handed off: <task name>. Follow-ups are on Docket." Do not wait for the result, do not report progress in the chat, and do not continue the task yourself. If the user asks about it later, answer in one sentence and point to Docket.

## Passing something on to a running handoff

When the user says something more about a handed-off task (go on, a correction, a new requirement), pass it on with one command and reply with one line:

```sh
~/.openclaw/workspace/skills/desk-dispatch/scripts/docket note <handoff id> "<what the user said, in their words>" --from agent:main:main
```

`docket list` shows the handoff ids (a unique prefix is enough); `docket status <id>` shows where one stands. With the Docket plugin installed, the open handoffs are also listed at the top of your turn in a `<docket>` block. Docket logs the note, wakes the handoff's PM itself and carries out what the PM answers. Passing words on costs the user nothing: only answering a card on the panel uses one of a handoff's three rounds.

When a handoff closes, its conclusion reaches you once in a `<docket-conclusion>` block and is written to the day's memory. Mention it only if it matters to what the user is doing now.

## Don't

- Don't call `opencode run`, `claude -p` or `codex exec` yourself for a handed-off task, and don't run `opencode-dispatch -s`: only Docket sends instructions to the coding agent.
- Don't send follow-ups with this script (`-s` is refused). Docket sends them after the user decides.
- Don't `sessions_send` into a Docket PM session (`agent:docket-pm:*`, or `agent:main:docket-*` and `agent:main:desk-*` for older handoffs): nobody carries out what the PM answers you there. Use `docket note`.
- Inside a Docket PM session never dispatch new work.
- If a command is refused with "Docket refused this", do what the refusal says instead; do not look for another way around it.

## In the chat

The user can type `/docket list`, `/docket status <id>` or `/docket note <id> <words>` themselves; these go to Docket without you.

## Requirements

- OpenClaw Docket running (its service writes `~/Library/Application Support/docket/daemon.json`).
- For OpenCode: nothing else. With an OpenCode older than 2.2 (or the Docket service not running), the `opencode-dispatch` skill next to this one (from opencode-openclaw-inbox) starts the session instead, and Docket picks it up.
- For Claude Code or Codex: the `claude` or `codex` CLI installed and logged in.
