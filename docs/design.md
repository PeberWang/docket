# Design

OpenClaw Docket sits between three rhythms. The chat is synchronous: you and your agent take turns, and a question you have not answered holds up the thread. The coding agent is parallel: each handoff moves forward in the background, in its own session. Docket is the rhythm that can be late: a brief of one sentence and at most two questions, each with a recommended option, waits on the panel until you have a minute. Everything that needs you, but not now, lands there. The sections below explain why this layer is needed and where its boundaries lie.

## The gap

A personal agent like OpenClaw lives in one chat. That is the point: your attention is limited, so daily life and work run through one thread, and the agent does most of the talking. It expands on what you say and fills in what you left out.

A coding agent like OpenCode, Claude Code or Codex works the other way. Every session is a complete piece of work. You decide up front, let go, and get a finished result.

Hand work from the first to the second and a third kind of thread appears. It is small and bounded, and it needs only a few decisions from you. It does not fit either place:

- **In the chat it gets in the way.** A question left unanswered blocks the conversation. Reports pile up between everything else. The agent's habit of expanding is exactly wrong here: you handed the work off so you would not have to think about the details.
- **In the coding agent it asks too much.** Opening the session means reading the transcript, and in practice you end up steering technical details yourself.

OpenClaw Docket gives this thread its own place: a quiet panel on the Mac where each handoff waits as a short brief until you decide.

## Three flows

| | Chat with your agent | Docket | Coding agent session |
|---|---|---|---|
| Unit | an ongoing conversation | one handoff, with a start and an end | one complete delivery |
| Who talks more | the agent expands | both keep it short | you specify, it builds |
| Your role | thinking partner | the one who decides | the one who specifies and reviews |
| Rhythm | synchronous; unanswered means stuck | asynchronous; it can wait | start it and let go |
| Rounds | open-ended | at most 3 | one, plus follow-ups |

Docket is closer to an in-tray than to a chat. A brief states where things stand in one sentence and asks for at most two decisions, each with a recommended option. You answer with a click or one line.

## Three boundaries

- **Chat → Docket.** The moment the agent hands work off. The chat gets one line ("Handed off: …") and does not bring the work up again.
- **Docket → coding agent.** After three rounds, or whenever you choose, Docket hands the session to you: OpenCode opens it, and for Claude Code or Codex a Terminal window opens with the resume command. Tasks that need your hands, such as tuning how something feels, should end up here early.
- **Docket → chat.** When a handoff is closed, its one-sentence conclusion comes back into the chat that dispatched it, once, at the start of the agent's next turn, and is written to the agent's daily memory. The process moves out of the chat; the result comes back.

## The brief

```json
{
  "kind": "report | question | failure",
  "progress": "one sentence: where it stands and what came out",
  "decisions": [
    {"id": "d1", "question": "…", "options": [{"id": "a", "label": "…", "recommended": true}, {"id": "b", "label": "…"}]}
  ],
  "decided_for_you": ["technical calls the PM made, one per line"]
}
```

- Only what truly needs you goes into `decisions`; everything else the PM decides and lists under *Decided for you*, folded away but one click from view.
- The format is checked in code (lengths, counts, exactly one recommended option). A reply that fails is sent back once with the errors. Prompting alone does not keep models short.
- The coding agent's own output never reaches the panel directly. The PM reads it, checks the delivery itself (files, unit tests; nothing that sends or deploys), and writes the brief.

## The PM

Each handoff gets its own OpenClaw session in a dedicated agent (`agent:docket-pm:<id>`), with its own short workspace and a tool policy that lets it read files and run commands but not send messages, reach other sessions or dispatch work. It never loads the main agent's instructions, which describe an agent that dispatches and follows up. It is not running all the time:

- The service watches the coding agent: the OpenCode event stream, or the exit of a Claude Code or Codex process.
- When a run ends, it wakes the PM once with `openclaw agent --agent docket-pm --session-key … --json` (no `--deliver`, so nothing reaches the chat). The wake repeats the contract (one JSON action, no tools that reach the coding agent, no waiting) and fences text from the coding agent or another session as untrusted.
- Your decision goes to the PM, which answers with an action: `relay` (the service sends the follow-up), `card` (ask you again), or `close`. Only answering a card is a round; steering with words in the chat, notes and the PM's own relays are free.
- After a run the PM verifies it and answers the same way: it may relay the next piece itself when your decisions already settle it (no round, at most `continue_limit` times in a row), and any instruction that would push, publish, deploy or reach outside the project waits on a card for your yes.

- A permission request you handed to the PM (with opencode-permission-notify) wakes it too: a reversible one is reviewed when the PM checks the run, an irreversible one waits for its allow, reject or "ask the person". This applies only to the sessions Docket runs for you; when you use the coding agent yourself, its own rules apply and nothing in Docket changes them.

Code keeps watch; the model only judges.

## The judge

A personal chat interleaves topics, so the context of a handoff is scattered across the conversation. Picking it out is a conversation disentanglement problem. The judge is [Jev](https://typesafe.ai), a model that answers typed questions with calibrated probabilities instead of generating text:

1. **Background.** At dispatch, one yes/no question per earlier message: is this part of the task's context? Messages above the threshold are kept verbatim. They carry your own words; a task brief is the agent's paraphrase.
2. **Attribution.** Later, when you mention a handoff in the chat, one choice among the open handoffs plus a yes/no "is it about any of them" and a "is this new work" check. A message moves only when all three agree with high probability. The message then goes to the handoff's PM, which decides what it means: steer the work with it, keep it as background (`hold`), or ask you. No card asks you to confirm that the words were meant. It answers the card on the panel only when the PM says your words settle it, and that is the one thing that costs a round.

Evaluated on 17 real handoffs from one person's chat, against reference labels written by a strong model and revised once:

| Task | Result |
|---|---|
| Background, threshold 0.5 | recall 0.97, recall of core messages 1.00, precision 0.77 |
| Attribution, threshold 0.85 | 19 messages moved, 17 correct; uncertain ones stay in the chat |
| Cost | under $0.05 for both evaluation runs; about 0.2 s per call |

Without a TypeSafe key the judge is off: handoffs still work, without picked background and without attribution.

## Privacy

- Only watched sessions are read, only the user and assistant text (no tool calls, results or reasoning), and only a recent window.
- Credentials are stripped before anything leaves the machine (`judge/redact.py`).
- TypeSafe's policy says inputs are not used to train or fine-tune models and are not shared beyond its service providers; it does not state a retention period.
- The PM runs through your OpenClaw, so its prompts go to whatever model your OpenClaw uses.
