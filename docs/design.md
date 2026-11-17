# Design

## The gap

A personal agent like OpenClaw lives in one chat. That is the point: your attention is limited, so daily life and work run through one thread, and the agent does most of the talking. It expands on what you say and fills in what you left out.

A coding agent like OpenCode, Claude Code or Codex works the other way. Every session is a complete piece of work. You decide up front, let go, and get a finished result.

Hand work from the first to the second and a third kind of thread appears. It is small and bounded, and it needs only a few decisions from you. It does not fit either place:

- **In the chat it gets in the way.** A question left unanswered blocks the conversation. Reports pile up between everything else. The agent's habit of expanding is exactly wrong here: you handed the work off so you would not have to think about the details.
- **In the coding agent it asks too much.** Opening the session means reading the transcript, and in practice you end up steering technical details yourself.

OpenClaw Desk gives this thread its own place: a quiet panel on the Mac where each handoff waits as a short brief until you decide.

## Three flows

| | Chat with your agent | The Desk | Coding agent session |
|---|---|---|---|
| Unit | an ongoing conversation | one handoff, with a start and an end | one complete delivery |
| Who talks more | the agent expands | both keep it short | you specify, it builds |
| Your role | thinking partner | the one who decides | the one who specifies and reviews |
| Rhythm | synchronous; unanswered means stuck | asynchronous; it can wait | start it and let go |
| Rounds | open-ended | at most 3 | one, plus follow-ups |

The Desk is closer to an in-tray than to a chat. A brief states where things stand in one sentence and asks for at most two decisions, each with a recommended option. You answer with a click or one line.

## Three boundaries

- **Chat → Desk.** The moment the agent hands work off. The chat gets one line ("Handed off: …") and does not bring the work up again.
- **Desk → coding agent.** After three rounds, or whenever you choose, the Desk hands the session to you: OpenCode opens it, and for Claude Code or Codex a Terminal window opens with the resume command. Tasks that need your hands, such as tuning how something feels, should end up here early.
- **Desk → chat.** When a handoff is closed, a one-sentence conclusion is written to the agent's daily memory. The process moves out of the chat; the result comes back.

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

Each handoff gets its own OpenClaw session (`agent:main:desk-<id>`). It is the same agent, with the same memory and tools, but a different session and different rules. It is not running all the time:

- The service watches the coding agent: the OpenCode event stream, or the exit of a Claude Code or Codex process.
- When a run ends, it wakes the PM once with `openclaw agent --session-key … --json` (no `--deliver`, so nothing reaches the chat).
- Your decision goes to the PM, which answers with an action: `relay` (the service sends the follow-up and counts a round), `card` (ask you again), or `close`.

Code keeps watch; the model only judges.

## The judge

A personal chat interleaves topics, so the context of a handoff is scattered across the conversation. Picking it out is a conversation disentanglement problem. The judge is [Jev](https://typesafe.ai), a model that answers typed questions with calibrated probabilities instead of generating text:

1. **Background.** At dispatch, one yes/no question per earlier message: is this part of the task's context? Messages above the threshold are kept verbatim. They carry your own words; a task brief is the agent's paraphrase.
2. **Attribution.** Later, when you mention a handoff in the chat, one choice among the open handoffs plus a yes/no "is it about any of them" and a "is this new work" check. A message moves only when all three agree with high probability. A follow-up triggered this way is shown as a card for you to confirm; it is never sent on its own.

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
