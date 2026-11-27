// Which tool calls the plugin refuses (architecture section 8). Pure and synchronous: it answers from
// the view the plugin keeps in memory and never calls anything, because OpenClaw treats a tool hook
// that throws or times out as a refusal. No view, or a call it cannot read: null, the call goes through.

// Writes into an OpenCode session. Reads (GET a session, its messages, its diff) stay allowed.
const DISPATCH_FOLLOW_UP = /\bopencode-dispatch\b[^\n;|&]*?\s(?:-s|--session)(?:\s+|=)(ses_[A-Za-z0-9]+)/g;
const RUN_IN_SESSION = /\bopencode\s+run\b[^\n;|&]*?\s(?:-s|--session)(?:\s+|=)(ses_[A-Za-z0-9]+)/g;
const SESSION_ROUTE = /\/api\/session\/(ses_[A-Za-z0-9]+)\/(?:prompt|message|interrupt|abort|command|shell|revert|inbox|permission)\b/g;
const WRITE_METHOD = /\b(?:post|patch|put|delete)\b|(?:^|\s)(?:-X|--request)\s*(?:POST|PATCH|PUT|DELETE)\b|(?:^|\s)(?:-d|--data(?:-raw|-binary)?|--json)(?:\s|=)/i;

// What a PM may never run: it judges and answers with one action, and Docket carries the action out.
const PM_FORBIDDEN = [
  [/\bopencode\s+run\b/, "start a coding-agent run"],
  [/\bopencode\s+api\s+(?:post|patch|put|delete)\b/i, "write to OpenCode"],
  [/\bopencode-dispatch\b/, "dispatch work"],
  [/\bdesk-dispatch\b/, "dispatch work"],
  [/\bdocket\s+note\b/, "pass words to a handoff"],
  [/\bclaude\s+(?:-p|--print)\b/, "start a coding agent"],
  [/\bcodex\s+exec\b/, "start a coding agent"],
  [/\bopenclaw\s+(?:agent|message|sessions|cron)\b/, "reach another session"],
  [/\bgit\s+push\b/, "push"],
];

export const RULES = {
  write: "write-into-handoff",
  pmSend: "send-to-pm",
  pmAct: "pm-dispatch",
};

/** The command text of a tool call: exec's command, Code Mode's code, a process input. */
export function commandText(params) {
  if (!params || typeof params !== "object") return "";
  return ["command", "code", "cmd", "script", "input"]
    .map((key) => params[key])
    .filter((value) => typeof value === "string")
    .join("\n");
}

function strings(value, out = [], depth = 0) {
  if (depth > 4 || out.length > 200) return out;
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => strings(v, out, depth + 1));
  else if (value && typeof value === "object") Object.values(value).forEach((v) => strings(v, out, depth + 1));
  return out;
}

function isPmSession(key, view) {
  return typeof key === "string" && (view.pm_prefixes || []).some((prefix) => key.startsWith(prefix));
}

/** Whether this agent turn is a Docket PM's: its own agent, or a PM session of the main agent. */
export function fromPm(ctx, view) {
  if (!ctx) return false;
  if (view.pm_agent && view.pm_agent !== "main" && ctx.agentId === view.pm_agent) return true;
  return isPmSession(ctx.sessionKey, view);
}

function guardedWrite(text, view) {
  const sessions = view.sessions || {};
  for (const pattern of [DISPATCH_FOLLOW_UP, RUN_IN_SESSION]) {
    for (const match of text.matchAll(pattern)) {
      if (sessions[match[1]]) return match[1];
    }
  }
  if (WRITE_METHOD.test(text)) {
    for (const match of text.matchAll(SESSION_ROUTE)) {
      if (sessions[match[1]]) return match[1];
    }
  }
  return null;
}

/**
 * The refusal for one tool call, or null to let it through.
 * call: { toolName, params, ctx: { agentId, sessionKey } }; view: GET /api/openclaw.
 */
export function inspect(call, view) {
  if (!view || !call) return null;
  const { toolName, params, ctx } = call;
  const pm = fromPm(ctx, view);

  if (/^(?:sessions_send|conversations_send|conversations_turn|sessions_spawn)$/.test(toolName || "")) {
    if (pm) return { rule: RULES.pmAct, reason: "reach another session" };
    const target = strings(params).find((s) => isPmSession(s, view));
    if (target) {
      return { rule: RULES.pmSend, target, handoff: (view.pm_sessions || {})[target] || null };
    }
    return null;
  }

  const text = commandText(params);
  if (!text) return null;
  if (pm) {
    const hit = PM_FORBIDDEN.find(([pattern]) => pattern.test(text));
    if (hit) return { rule: RULES.pmAct, reason: hit[1] };
  }
  const session = guardedWrite(text, view);
  if (session) return { rule: RULES.write, target: session, handoff: view.sessions[session] };
  return null;
}

/** What the model reads when a call is refused: why, and the one command to use instead. */
export function refusal(hit, ctx, noteCommand) {
  const from = ctx && ctx.sessionKey ? ` --from ${ctx.sessionKey}` : "";
  const note = `${noteCommand} note ${hit.handoff || "<handoff id>"} "<the person's words>"${from}`;
  if (hit.rule === RULES.pmAct) {
    return `Docket refused this: a Docket PM does not ${hit.reason}. Answer with one JSON action; Docket carries it out.`;
  }
  if (hit.rule === RULES.pmSend) {
    return `Docket refused this: nobody carries out what a Docket PM answers in its own session. To pass the person's words on, run: ${note}`;
  }
  return `Docket refused this: only Docket sends instructions to a handoff's coding-agent session. To pass the person's words on, run: ${note}. To redo the work, hand off again with desk-dispatch --supersedes ${hit.handoff || "<handoff id>"}.`;
}
