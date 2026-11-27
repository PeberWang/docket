# OpenClaw Docket plugin

Keeps OpenClaw's agents inside the Docket protocol (see `docs/architecture.md`, section 8). It decides nothing; it routes and refuses.

| Hook | What it does |
|---|---|
| `before_tool_call` (`exec`, `process`, session tools) | refuses writes into a handoff's OpenCode session that do not come from Docket (`opencode-dispatch -s`, `opencode run -s`, POSTs to the session's routes), `sessions_send` into a PM session, and, from a PM, anything that dispatches work, reaches a coding agent or another session, or pushes. The refusal names the command to use instead: `docket note <id> "<words>"` |
| `message_received` | tells Docket a chat message arrived, so it reads the transcript at once instead of at the next poll. No text is sent |
| `before_prompt_build` | adds a short list of open handoffs to the main session and to any chat that handed work off |
| `enqueueNextTurnInjection` | queues each conclusion once into the next turn of the chat that dispatched it |
| `/docket` | `list`, `status <id>`, `note <id> <words>` (owner only) |

It never breaks OpenClaw. OpenClaw treats a tool hook that throws or times out as a refusal, so the tool check is synchronous and answers from a view kept in memory (refreshed every 5 s from `GET /api/openclaw`). Without a view, or when the check fails, the call goes through and the plugin logs once that the service is unreachable.

## Install

The installer does this (`scripts/install.sh`). By hand:

```sh
openclaw plugins install --link integrations/openclaw/plugin --force
openclaw plugins enable docket
openclaw plugins inspect docket --runtime --json   # typedHooks lists the three hooks
```

`before_prompt_build` needs conversation access. In `openclaw.json`:

```json5
{
  plugins: {
    entries: {
      docket: {
        enabled: true,
        hooks: { allowConversationAccess: true },
        config: {
          // all optional
          // home: "~/Library/Application Support/docket",
          // mainSessions: ["agent:main:main"],
          // enforce: false,   // record what would be refused, refuse nothing
        },
      },
    },
  },
}
```

Without conversation access the list of open handoffs is not added; everything else works.

## Test

```sh
node --test test/
```
