# Changelog

## 0.2.1 (2026-11-27)

- Permissions come in two layers that never mix (with opencode-permission-notify): when you use the coding agent yourself, only its own rules apply; what you set in the permissions window applies only when Docket's PM works with the coding agent for you in a handoff. 0.2.0 still let those settings change your own sessions.
- The permissions window shows the coding agent's own rule for each class, read from its configuration, next to your choice for the PM, opens only once, and carries Docket's icon in the Dock.

## 0.2.0 (2026-11-27)

This release makes Docket hold together under daily use: one lifecycle per handoff, a PM that only judges, the person's attention spent only on decisions, and a record of every step. It was measured against an audit of ten days of the author's own handoffs before and after.

### The PM and the person's attention

- After a run the PM verifies the delivery and answers with one action: relay the next piece, put a card up, close, or hold. It may move the work on by itself up to `continue_limit` times in a row (5).
- A round is the person answering a card, and nothing else: words passed on, chat mentions, the PM's own relays, retries and takeovers are free. At three rounds the PM writes a handoff note.
- Old cards go when they are moot: a newer question replaces an older one, news never stands in front of a decision, a permission request is never replaced. An unanswered card closes the handoff after 12 hours (`quiet_expire_minutes` 720), the limit card too. A card is never closed because a run ended; the 30-minute auto-close is gone.
- No confirmation cards for the person's own words: what the main agent passes on, or the judge finds in the chat, is acted on.
- Instructions that would push, publish, deploy, delete outside the project, run a script from the network or change the system wait for the person's yes unless their own words asked for that act.
- Every wake repeats the PM's contract; text from the coding agent or another session is fenced as untrusted, the person's words as theirs. The PM's reply is taken as exactly one decision, checked field by field.
- PMs run in their own OpenClaw agent, `docket-pm`, with a short workspace and a tool policy that cannot message, reach other sessions or dispatch work.

### One writer

- OpenCode 2.2 and later is driven over its HTTP API: Docket opens the session itself, gives every instruction its own message id and metadata, and confirms delivery by that id. Older OpenCode still runs through `opencode run -s`.
- A write into a handoff's session that Docket did not make is recorded as a foreign turn and adopted, so its end still reaches the PM once.
- When the coding agent asks with its question tool and waits, the person's answer goes to that question.
- `docket note <handoff> "<words>"` is the one way in for anyone else; `desk-dispatch --supersedes <handoff>` redoes work and closes the old handoff as replaced.
- An OpenClaw plugin refuses writes into handoff sessions that do not come from Docket and names the command to use instead, lists open handoffs for the main agent, brings each conclusion back into the dispatching chat once, and adds `/docket`. It fails open: when it cannot tell, the call goes through.

### Permissions

- With opencode-permission-notify, permissions come in two layers that never mix: the person's own sessions follow only the coding agent's own rules, and for a handoff the person chooses per class what happens when the PM works with the coding agent for them (the same as the agent, ask me, allow, or the PM). Handed to the PM, reversible requests are allowed and reviewed when the PM checks the run; irreversible ones are held until the PM allows or rejects them, and go to the person when it is unsure, late or the risk rules flag them. The permissions window shows the agent's own rule for each class next to the PM's, and opens only once. Docket keeps a heartbeat so permission-notify knows it is there, and never approves over the person's classes.
- Without permission-notify, Docket never approves reading credentials (SSH and cloud keys, `.env` files, keychains, OpenClaw's and Docket's own state) or running a command the risk rules flag.
- The risk gate needs every kind of risk in an instruction to have been asked for, does not take "don't push" for asking, and never takes words the judge found in a chat as the person's yes.

### Reliability

- The watchdog follows every instruction: not delivered, ended before Docket saw it start, quiet past the limit, stopped by a person. It observes by default (`"watchdog": "observe"`) and steps in with `"act"`; it stops a run with OpenCode's `/interrupt` (the old `/abort` did not exist).
- A run a person stopped goes to a card and is never resent. Docket's own interrupt is not taken for a run end.
- PM jobs are written to the ledger and run again after a restart; one chain runs per handoff, so run ends merge instead of queueing cards. A closing is final at once and the PM's conclusion follows.
- One service per data folder; registration cannot run twice for one session.
- The panel decodes each handoff on its own, so one it cannot read no longer empties the panel; it keeps the last good data while the service restarts.
- `docket note` and the judge's reading of the same chat message count once (the heartbeat window of a chat is no longer taken for the chat).

### Install and operations

- The service runs from a copy of its code in `~/Library/Application Support/docket/lib` (`scripts/deploy-service.sh`), so launchd's python3 never needs permission to read a clone on the Desktop.
- `scripts/install.sh` wires OpenClaw through `scripts/openclaw_setup.py`: the plugin, the PM agent and the skill, each change to `openclaw.json` one validated `openclaw config patch` (`--dry-run`, `--remove`).
- `docket doctor` checks every link and prints the fix; `docket audit report` writes a read-only report from a copy of the ledger and the logs.
- The daily health summary goes to `health_dir` (default: `health/` in Docket's folder).

### Not built yet

See section 12 of docs/architecture.md.

## 0.1.0 (2026-11-17)

First release: the panel, a PM per handoff woken when a run ends, cards with at most two questions and a recommended option each, the round budget, the judge for background and chat mentions, OpenCode, Claude Code and Codex.
