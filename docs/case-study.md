# Case study: stabilising Docket from its own records

Docket is a workflow tool: it sits between a person, their chat agent and the coding agents they hand work to, and its job is to spend the person's attention only where a decision needs them. This note describes how version 0.2 was made stable, working from what ten days of daily use had recorded rather than from impressions. All figures are aggregates from the author's own instance; no task names or conversation text are used.

## Starting point

Version 0.1 had been in daily use for ten days: 102 handoffs, 434 cards and 2,028 ledger events. It felt unreliable in ways that were hard to name: cards that appeared twice, work that seemed to continue without the panel knowing, handoffs that closed themselves. The code itself looked sound (187 tests passing, no broken ledger invariants at the time). So the first step was an audit, not a fix.

## The audit

A read-only tool copied the ledger and cross-checked it against the service log, the coding agent's own log (every `opencode run -s` it recorded, matched to Docket's sends within 15 seconds) and the chat agent's transcript (commands only, never message text). It rebuilt the timeline of every unusual handoff and checked eight invariants that must hold at every moment. The tool is now part of the repository as `docket audit`.

The five numbers that mattered:

| Finding | Figure |
|---|---|
| Follow-up instructions into handoff sessions that did not come from Docket | 26 of 74 (35%); of those traced, 10 from the PM's own session, 6 from the main chat |
| Places where one run ending produced several near-identical cards | 25 |
| Handoffs closed by a 30-minute "nobody answered" rule | 15, one card 42 seconds after it appeared |
| Closings whose conclusion never reached the chat's memory | 21 |
| Handoffs registered twice | 90 of 102 |

The root cause was not in any one function. Docket assumed one writer into the coding agent's session after dispatch, and in practice there were three: Docket, the PM and the main chat agent. Each was reasonable on its own; together they produced duplicates, races and cards that no longer matched the work.

The data also said something about the person. When a card was answered on the panel, the median time from appearing to answer was 3 minutes, the 90th percentile 71 minutes. More than half of the person's involvement (24 of 42 rounds) happened in the chat, not on the panel. And the three handoffs that ran out of their three-round budget did so in 11, 24 and 65 minutes, every time through chat mentions that were each counted as a round. The budget was meant to protect the person's attention, and it was being spent on things that were not decisions.

## Decisions

The fixes followed from a few product decisions, each taken by the person after seeing the data:

- **The PM is a passive middle layer.** It is woken by one event, answers with one action and ends. Waiting, following runs and retrying belong to code. This removed the PM's own follow-ups, the largest single source of writes Docket did not make.
- **One writer, held in three layers.** In code, an OpenClaw plugin refuses direct writes into handoff sessions and names the command to use instead; in the skill, the main agent learns to pass words on with `docket note`; in the prompt, the PM runs as its own agent with no tools that reach the coding agent. Each layer alone had failed in practice.
- **A round is the person answering a card, and nothing else.** Passing words on, the PM moving the work on and retries became free. The budget now measures decisions, not activity.
- **Old cards go when they are moot, not when a timer says so.** A newer question replaces an older one; an unanswered card expires after twelve hours with a note; a card is never closed because a run ended.
- **No confirmation for the person's own words.** If the main agent passes words on, or the judge finds them in the chat, the PM acts on them. Only acts that reach beyond the project, such as pushing, publishing or deleting outside it, wait for an explicit yes.
- **Permissions in two layers.** The coding agent's own rules govern the person's own sessions and are never changed by Docket; for handoffs, the person decides per class of request what happens when the PM works with the coding agent for them. Reversible requests handed to the PM are allowed and reviewed afterwards; irreversible ones are held until the PM decides and come back to the person when it is unsure.
- **Observe before acting.** The new watchdog first only recorded what it would have done, so its rules could be checked against real runs before it was allowed to resend anything.

## Building and checking it

The work was done in a separate worktree while the running service stayed on the old code, in five phases: validation and audit traces, the PM's lifecycle, driving OpenCode over its HTTP API with its own message ids, the OpenClaw plugin and PM agent, then installation and documentation. An independent review of the whole change found four blocking issues (three release-hygiene rules and a credential gap in Docket's own permission answers) and a dozen correctness issues, among them a restart that could wake a PM twice and a race between a queued instruction and the end of the work before it; they were fixed before release, and what was not fixed is listed. The test suite grew from 187 to 296 tests, including the plugin's own.

Going live surfaced problems no test had: an OpenCode run waiting on its own question tool, which queued the person's answer behind it; a heartbeat window being taken for the chat it belonged to, so one message woke the PM twice; a plugin manifest field that made OpenClaw reject its whole configuration for a minute; macOS withdrawing launchd's permission to read the clone, which took the service down for three minutes and led to running it from its own copy. Each was traced in the ledger, fixed, and covered by a test.

## Result

On the first end-to-end runs through the chat after release, a handoff went from dispatch to verified closing in about ten seconds of Docket's own time, follow-ups said in the chat reached the work through `docket note` and cost no round, the conclusion came back into the chat once, and a plugin refusal stopped a direct write and pointed to the right command. An irreversible permission request handed to the PM was decided in three seconds without asking the person, and the reason was recorded on the handoff.

What remains is listed in section 12 of the architecture document. The main lesson was about method: the instability was a protocol problem, visible only when the records of every party were put side by side, and the fixes were product decisions about whose attention is spent on what, before they were code.
