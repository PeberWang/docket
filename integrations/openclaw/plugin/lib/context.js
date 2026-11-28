// What the plugin adds to the main agent's turns: the open handoffs, and each conclusion once.
// Prompts are English (RELEASE_GATE); the agent answers the person in their own language.

const MAX_HANDOFFS = 8;
const LINE = 200;

function clip(text, limit) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

const STATE = {
  running: "the coding agent is working",
  pending: "the PM is reading the result",
  awaiting: "waiting for the person",
  limit: "out of rounds: the person closes or takes over",
  taken_over: "the person drives it in OpenCode",
};

/** Whether this turn's session gets the list: a main session, or one that handed work off. */
export function listsFor(sessionKey, view, mainSessions) {
  if (!sessionKey || !view) return false;
  if (mainSessions.includes(sessionKey)) return true;
  return (view.handoffs || []).some((h) => h.source === sessionKey);
}

/** The short block for prependContext, or null when nothing is open. */
export function openHandoffs(view, sessionKey, noteCommand) {
  const items = (view && view.handoffs) || [];
  if (!items.length) return null;
  const lines = items.slice(0, MAX_HANDOFFS).map((h) => {
    const card = h.needs_you && h.card ? `: ${h.card}` : "";
    return `- ${h.id} "${clip(h.title, 60)}": ${STATE[h.state] || h.state}${card}`.slice(0, LINE);
  });
  if (items.length > MAX_HANDOFFS) lines.push(`- and ${items.length - MAX_HANDOFFS} more (docket list)`);
  const from = sessionKey ? ` --from ${sessionKey}` : "";
  return [
    "<docket>",
    "Open handoffs on Docket. A coding agent works on each in the background; only Docket talks to it.",
    ...lines,
    "When the person says something about one of these, pass their words on and reply in one line:",
    `  ${noteCommand} note <id> "<their words>"${from}`,
    "To redo one, hand it off again with desk-dispatch --supersedes <id>. Never write into these sessions yourself.",
    "</docket>",
  ].join("\n");
}

/** The text injected once into the dispatching session's next turn when a handoff closes. */
export function conclusion(item) {
  return [
    `<docket-conclusion handoff="${item.handoff}">`,
    `Docket closed the handoff "${clip(item.title, 80)}": ${clip(item.text, 1200)}`,
    "This is for your awareness. Mention it to the person only if it bears on what you are doing now. When they ask about this work, answer from this conclusion in one or two sentences: Docket's PM already verified it, so do not open its files or run it yourself, and do not continue that work.",
    "</docket-conclusion>",
  ].join("\n");
}

/** /docket list and /docket status, for the person in the chat. */
export function listing(view) {
  const items = (view && view.handoffs) || [];
  if (!items.length) return "No open handoffs.";
  return items.map((h) => `${h.id}  ${h.state}  ${clip(h.title, 60)}${h.needs_you && h.card ? `\n    ${clip(h.card, 160)}` : ""}`).join("\n");
}

export function status(view, id) {
  const items = ((view && view.handoffs) || []).filter((h) => h.id === id || h.id.startsWith(id) || h.id.startsWith(`ses_${id}`));
  if (items.length !== 1) return items.length ? `${id} matches ${items.length} handoffs.` : `No open handoff matches ${id}.`;
  const h = items[0];
  return [`${h.title}  [${h.id}]`, `state   ${h.state} (${STATE[h.state] || h.state})`, `rounds  ${h.rounds}/${view.round_limit || 3}`,
    ...(h.card ? [`card    ${h.card}`] : [])].join("\n");
}
