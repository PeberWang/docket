# Architecture

This document describes how Docket is built: the parts, the one lifecycle every handoff follows, who may change what, and how Docket fits into OpenClaw and the coding agents so that installing it is enough. The reasons behind the product are in [design.md](design.md); the panel's contract is in [app/SPEC.md](../app/SPEC.md).

It is written against OpenClaw 2026.11 and OpenCode 2.2. Where Docket relies on behavior of either, the section says which interface it uses and what it falls back to.

## 1. What Docket is

A personal agent hands work to a coding agent. Between the two, Docket keeps one case per handoff: it follows the coding agent's runs, wakes a project manager (PM) when there is something to judge, puts only what needs the person on a panel, carries their answer back, and records every step.

Seven rules hold everywhere. Each later section points back to them.

1. **Code keeps watch; the model only judges.** The PM is passive. It is woken by one event, answers with one action, and ends. Following runs, waiting, retrying and timing out are Docket's code, never the PM's loop.
2. **One writer.** Only Docket sends instructions into a handoff's coding-agent session and only Docket wakes its PM. Everyone else (the person, the main agent, other sessions) passes information to Docket, which decides what reaches the PM.
3. **The person decides.** Nothing the person did not say is decided on their behalf. Silence is never an answer. An instruction that follows from the person's input quotes their words.
4. **Late is fine.** A question waits until it is answered, made moot by newer work, settled in the chat, or left unanswered for a day, and each of those leaves a trace.
5. **Everything is recorded.** Every transition is an event with who did it and why. The ledger is the single source of truth and nothing but the service writes it.
6. **Faults never close work.** Docket's own failures become cards with a retry. Only the closings in section 6 end a handoff.
7. **Installing is enough.** The installer wires OpenClaw (plugin, PM agent, skill) and the coding agent. Nobody has to tune prompts by hand afterwards.

## 2. Parts

```
 person ── chat ──▶ OpenClaw Gateway ─────────────────────────────┐
   │                 │  main agent (agent:main:main)              │
   │                 │  docket plugin: tool policy, message tap,  │
   │                 │    context injection, /docket command      │
   │                 │  PM agent (agent:docket-pm:<handoff>)      │
   │                 └──────────────┬─────────────────────────────┘
   │                                │ local HTTP (token)
   │   Docket.app ◀── local HTTP ──▶ Docket service (launchd)
   │   docket CLI                   │  ledger · state machine · PM orchestrator
   └──────── panel ─────────────────│  engines · watchdog · judge · audit
                                    │
                    HTTP / events   ▼          process + output file
                 OpenCode service ◀─┴─▶ Claude Code / Codex CLI
```

| Part | Owns | Never does |
|---|---|---|
| Docket service (`docket/`) | the ledger, every state change, waking PMs, sending instructions to coding agents, cards, closings, the audit trail | ask the person anything except through a card |
| OpenClaw plugin (`integrations/openclaw/plugin/`) | keeping OpenClaw's agents inside the protocol: blocks direct writes into handoff sessions, forwards the owner's chat messages, tells the main agent which handoffs are open, delivers conclusions back | decide anything; it only routes and refuses |
| PM agent `docket-pm` | judging one event for one handoff and answering with one action | send messages, dispatch work, or talk to the coding agent; it has no tools for that |
| Skill `desk-dispatch` | teaching the main agent the two verbs it needs: hand off, and pass a word on | follow up on its own |
| Docket.app and `docket` CLI | showing state and taking the person's answers | write the ledger directly |
| Judge (`judge/`) | three yes/no-with-probability questions (section 7) | act on its own answers |

## 3. One handoff, from start to end

This is the canonical flow. The code is organised around it, and the tests walk it.

1. **Hand off.** The main agent runs `desk-dispatch` with a title, a project directory, a brief and one instruction. The script posts them to Docket (`POST /api/dispatch`) with the session it came from. Docket registers the handoff, creates the coding-agent session itself, and sends the instruction as turn 1. The chat gets one line: "Handed off: …".
2. **Run.** The coding agent works on its own. Docket follows the turn (section 5) until the run ends.
3. **Verify.** The run's end wakes the PM once. The PM reads the result (it may read files and run the project's tests) and answers one action:
   - `relay`: the next instruction, when the brief already settles what comes next;
   - `card`: what the person must decide, as a brief with at most two questions;
   - `close`: the work is done;
   - `hold`: nothing to send now (for example, information to keep for the next instruction).
4. **Ask.** A card waits on the panel. The person clicks an option or writes one line. That wakes the PM, which answers with an action again.
5. **The chat says something about it.** There are two paths, and the first is preferred:
   - The main agent recognises a follow-up and runs `docket note <handoff> "<the person's words>"`.
   - The plugin forwards the owner's chat message to Docket, the judge attributes it to an open handoff, and Docket hands it to the PM as new information.

   Either way the PM decides what to do with it. A relay made while the coding agent is still working is delivered with `queue` (after the current work) or, for a correction the person wants applied now, `steer` (before the agent's next model call).
6. **End.** The handoff closes through one of the closings in section 6. The conclusion goes back to the chat it came from.

Taking over (the person continues in OpenCode or Terminal) and handing back are side paths of step 4.

## 4. State

A handoff has one **state**, which is what the panel shows, and its current **turn**, which is what the watchdog follows.

| State | Meaning | Left by |
|---|---|---|
| `running` | the coding agent works on a turn | run end → `pending` |
| `pending` | a PM job for this handoff is queued or running | the PM's action |
| `awaiting` | a card waits for the person | an answer, a note, a settled chat, an expiry |
| `limit` | the attention budget is spent; only close or take over | close, take over |
| `taken_over` | the person drives the session themselves | hand back |
| `closed` | ended through a closing in section 6 | a note reopens it |

Invariants. The sweep checks them every minute and logs any breach as an error event:

1. `awaiting` has exactly one card on show; queued cards exist only behind one on show.
2. `running` has an open turn; `pending` has a PM job in the ledger's job table.
3. `closed` and `taken_over` have no open turn and no unanswered card.
4. A live handoff is always in exactly one of: a turn running, a PM job pending, a card waiting. When nothing applies (the person declined a proposed instruction, a PM answered `hold` with nothing running), Docket settles it (`settle_state`): it shows the next queued card, and if there is none it wakes the PM once for a "what next" card, which costs no round.
5. `taken_over` and `closed` are left only by hand back, a note, or the person closing. A PM job that finishes after a takeover is recorded and dropped.

PM jobs are durable. A job is written to the ledger before it runs and deleted when it ends. One chain runs per handoff, so a second run end while a job waits merges into it instead of queueing a second card. After a restart, unfinished jobs run again.

## 5. Coding agents

Every engine answers the same five questions: how to start a session, how to send an instruction, how to tell a run ended, what the agent said last, and how the person takes over.

**OpenCode (HTTP, OpenCode 2.2 and later).** Docket drives the OpenCode service directly:

- **Session.** `POST /api/session` with an id Docket chooses (retries return the same session), the project as `location`, `metadata: {"docket": {"handoff": id}}`, and the title `🦞 <title>`, archived, so the inbox convention still holds. Permissions are a per-handoff ruleset (section 9) instead of `--auto`.
- **Instruction.** `POST /api/session/{id}/prompt` with a message id Docket chooses (the turn), `metadata: {"docket": {"turn": n, "via": …}}`, and `delivery: "queue"` or `"steer"`. Sending the same id again is a no-op, so a resend after a lost connection cannot duplicate work.
- **Following a turn.** `session.inbox.delivered` for Docket's own message id says the instruction reached the agent; `session.execution.started`, `session.tool.*` and `session.step.*` are activity; `session.execution.succeeded/failed/interrupted` end the run. When the event stream drops, the sweep reads the same facts from `GET /api/session/{id}` and `GET /api/session/{id}/message/{messageID}`.
- **Writes Docket did not make.** A user message in the session without Docket's metadata is a foreign turn. It is recorded (`turn:foreign`), adopted as the current turn so its end wakes the PM once, and counted in the health summary.
- **Stopping.** `POST /api/session/{id}/interrupt`.
- **What changed.** `GET /api/session/{id}/diff` gives the files a turn changed, for the PM and for a resend.

Docket reads OpenCode's `/openapi.json` at start. An older OpenCode without these routes falls back to `opencode run -s`, with the limits listed in the README.

**Claude Code and Codex (CLI).** Docket starts the CLI as a detached process whose output goes to a file. The process exit is the run end, and the output file holds the reply. These engines have no watchdog beyond a time limit on the process.

**The watchdog** follows each turn of an OpenCode handoff:

- not delivered within the grace period: sent again once, with the same message id;
- ran and ended before Docket saw it start (registered late, events lost): treated as a run end for the PM;
- busy but silent past the quiet limit: noted, then stopped and sent again once. A running tool with progress events, a step under way, or a changed file all count as activity;
- ended interrupted by someone other than Docket (the person pressed stop): a PM card, never a resend.

It has two modes: `observe` records what it would do and does nothing; `act` steps in. Observe is the default until its records show it does not misfire.

## 6. Closing

A handoff closes in exactly one of these ways. Each goes through `finish()`, which is idempotent: it writes the conclusion card and the events, and delivers the conclusion back to OpenClaw.

| Closing | Trigger | Conclusion |
|---|---|---|
| PM close | the PM judged the work done | the PM's sentence and what is left |
| Person close | the person pressed Close | the PM's sentence; if the PM fails, the last card's progress |
| Budget | the person closes at the limit | the wrap-up the PM wrote when the budget ran out |
| Quiet expiry | a card unanswered for `quiet_expire_minutes` (24 h) with the agent idle | the last progress, marked unanswered |
| Superseded | a newer handoff declared it replaces this one (`desk-dispatch --supersedes`) | "replaced by …" |

Docket's own faults are never on this list: a missing session, a PM that failed three times, or a directory that does not exist become failure cards with a retry. Takeover is not a closing; the handoff waits for a hand back.

The conclusion reaches OpenClaw twice. It is injected into the next turn of the session that dispatched the handoff (exactly once, through the plugin), and it is written as one line to the agent's daily memory, as before.

## 7. The person's attention

**Rounds.** A handoff may take the person's attention three times (`round_limit`). One round is exactly one thing: the person answering a card, on the panel or in the chat when the judge reads the answer off their words. Nothing else costs a round: words passed on with `docket note`, a chat message the PM takes up, a `hold`, the PM's own relays, Docket's retries, takeovers and hand-backs are all free. The budget is about the person's attention on decisions, not about how often the work moves. At the limit, only the limit card is shown, and the person closes or takes over.

**Old cards.** A card leaves the panel when it is answered, when the handoff ends, or earlier when it is moot: only the newest question stands, a question takes the panel from news, newer news replaces older news, and a permission request is never superseded. What is left expires after `quiet_expire_minutes` (12 hours) and closes the handoff with a note; a card for Docket's own fault never expires, and a card is never closed just because a run ended.

**Continuing without the person.** After a run ends, the PM may relay the next piece itself when the brief already settles it. This is how a handoff moves forward without the person, and it costs no round. It is bounded:

- at most `continue_limit` such relays in a row (default 5). Then the PM must put a card up showing where things stand, and the person's answer resets the count;
- a relay that matches the risk patterns below always goes on a card first.

**Who can cause an instruction.** An instruction reaches the coding agent without asking the person again only when it follows from one of:

- the person's click;
- the person's own words, quoted: a note carrying the person's words, or a chat message the judge matched with the quote;
- the PM's technical judgment within the brief, after a run end.

Anything else goes on a card first: a message that reached a PM from another session, or text not from the owner. So does a relay matching the risk patterns (push, publish, deploy, delete outside the project, credentials, sending data out), unless the person's quoted words ask for exactly that.

**The judge** answers three questions with calibrated probabilities:

1. at dispatch, which earlier chat messages are background for the task;
2. later, which open handoff a chat message belongs to, if any, and whether it is new work instead;
3. whether a waiting card was answered or made moot by what the person wrote since.

Without a judge key, Docket works without background, attribution or chat settling.

## 8. OpenClaw integration

The protocol holds in three layers. Each one alone has failed in practice.

**Code (the plugin).** Installed into the Gateway, it:

- refuses tool calls that write into a handoff's session from anywhere but Docket: `opencode-dispatch -s`, `opencode run -s`, API calls to the session's prompt, message or interrupt routes, and `sessions_send` into a PM session. The refusal tells the model the one command to use instead: `docket note <id> "<words>"`;
- refuses, from the PM agent, any command that dispatches work or reaches a coding agent;
- forwards the owner's messages in watched sessions to Docket as they arrive. This replaces reading OpenClaw's database every five seconds, which stays only as a fallback;
- adds a short list of open handoffs to the main agent's prompt, so it knows what exists and how to pass a word on;
- injects each conclusion into the dispatching session's next turn;
- registers `/docket` (list, status, note) in the chat.

The plugin must never break the person's OpenClaw. OpenClaw treats a tool hook that throws or times out (15 s) as a refusal, so the plugin:

- answers from a list of live handoff sessions it keeps in memory and refreshes in the background;
- never calls Docket while a tool call waits;
- lets everything through when it cannot tell, logging that it did.

Which hooks it uses:

| Need | Hook | Permission it needs |
|---|---|---|
| refuse a write | `before_tool_call` (`event.params.command`, `ctx.agentId`, `ctx.sessionKey`) returns `{block, blockReason}` | none |
| forward the owner's message | `message_received` (text, sender, session key) | none |
| tell human from `sessions_send` input | `before_prompt_build` (`inputProvenance`) | `hooks.allowConversationAccess` |
| list open handoffs for the main agent | `before_prompt_build` | `hooks.allowConversationAccess` |
| deliver a conclusion | `api.session.workflow.enqueueNextTurnInjection` | prompt modification not disabled |

Three limits shape the design:

- A process an agent's `exec` starts cannot run `openclaw agent`. Only the Docket service, running on its own, wakes a PM.
- The plugin manifest can ship the skill, but not an agent. The installer creates the PM agent with `openclaw agents add` and its config entry.
- `openclaw agent --timeout` bounds one model attempt in the Gateway. A CLI that is killed sends `chat.abort`; a CLI that merely lost its connection may leave the turn running. So a PM reply that arrives after Docket gave up is never carried out on its own; it goes on a card.

**Skill.** `desk-dispatch` is the main agent's manual: hand off through Docket, pass follow-ups on with `docket note`, and redo work with `--supersedes` instead of a second unrelated handoff.

**Prompt.** The PM agent has its own workspace, so it never loads the main agent's instructions. Its AGENTS.md is the PM contract: one JSON action per wake, no tools that reach the coding agent, no waiting. Every wake repeats the contract's short form, and Docket checks the reply in code.

The PM agent's tool policy allows reading files and running commands; the plugin refuses the commands above. It denies messaging, session tools, cron, browser and gateway tools.

## 9. Permissions in the coding agent

Every OpenCode handoff carries a ruleset applied on top of OpenCode's own:

- inside the project: allow;
- outside it: allow reads of ordinary files, ask for writes;
- always: ask for reads of credentials (`~/.ssh`, `~/.aws`, keychains, `.env*`, OpenClaw and Docket's own state) and for destructive commands.

Questions OpenCode still asks reach the person through the permission popup. They are never silently approved.

## 10. Records and audit

Every event carries:

- `actor`: `docket`, `user:panel`, `user:chat`, `main`, `pm`, `opencode`, `watchdog` or `sweep:<name>`;
- `cause`: the id of the turn, card, job or event that led to it.

Beyond state changes, Docket records:

- turns sent, delivered, running, foreign and ended;
- every PM wake: why, which job, which attempt and model, how long, and the action it returned;
- cards shown and resolved, with who resolved them;
- tool calls the plugin refused;
- registration skips, with the exact reason;
- every closing.

The daily health summary counts:

- stuck turns and those that healed by themselves;
- what needed the person;
- PM failures and slow wakes;
- foreign turns and refused overreach;
- invariant breaches.

`docket audit` produces the full report from a copy of the ledger, the service log and OpenCode's log; its method is described in the report itself.

## 11. Installation

`scripts/install.sh` does everything once, idempotently, and prints what it changed:

1. builds Docket.app and copies the service's code into `~/Library/Application Support/docket/lib`, so the clone can move or change without touching the running service;
2. installs the two launch agents;
3. installs and enables the OpenClaw plugin with the hook permissions it needs;
4. creates the `docket-pm` agent with its workspace and tool policy;
5. installs the skill, backing up any copy the user changed;
6. detects which coding agents are installed and sets the default.

`docket doctor` checks every link: the Gateway, the plugin, the PM agent, OpenCode's API, the judge key and the launch agents. For each one it prints the fix.
