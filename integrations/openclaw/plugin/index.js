// OpenClaw Docket plugin: keeps OpenClaw's agents inside the Docket protocol (docs/architecture.md,
// section 8). It refuses writes into handoff sessions that do not come from Docket, taps arriving
// chat messages, lists open handoffs for the main agent, delivers conclusions back, and adds /docket.
// It decides nothing, and it must never break OpenClaw: every hook answers from memory or gives up.

import os from "node:os";
import path from "node:path";

import { Client } from "./lib/client.js";
import { conclusion, listing, listsFor, openHandoffs, status } from "./lib/context.js";
import { inspect, refusal } from "./lib/policy.js";

export const ID = "docket";
const TIMER = Symbol.for("openclaw-docket.refresh");
const DAY = 24 * 60 * 60 * 1000;

let define = (entry) => entry;
try {
  ({ definePluginEntry: define } = await import("openclaw/plugin-sdk/plugin-entry"));
} catch {
  // Loaded outside the Gateway (tests) or from a linked folder without the SDK: the plain entry object.
}

export function settings(raw = {}) {
  return {
    home: raw.home,
    refreshMs: Number.isInteger(raw.refreshMs) && raw.refreshMs >= 1000 ? raw.refreshMs : 5000,
    staleMs: 2 * 60 * 1000, // the list is left out of prompts when the view is older than this
    mainSessions: Array.isArray(raw.mainSessions) ? raw.mainSessions : ["agent:main:main"],
    noteCommand: raw.noteCommand || path.join(os.homedir(), ".openclaw/workspace/skills/desk-dispatch/scripts/docket"),
    enforce: raw.enforce !== false, // false: record what would be refused and let it through
  };
}

/** Everything the plugin does, on one api object; exported for the tests. */
export function createDocket(api, { client, now = Date.now } = {}) {
  const config = settings(api.pluginConfig || {});
  const log = api.logger || console;
  const docket = client || new Client(config);
  const state = { view: null, at: 0, failing: null, delivering: new Set() };

  function report(error) {
    const reason = String((error && error.message) || error);
    if (reason !== state.failing) log.warn?.(`docket: the service is not reachable (${reason}); tool calls go through unchecked`);
    state.failing = reason;
  }

  async function refresh() {
    try {
      state.view = await docket.view();
      state.at = now();
      if (state.failing) log.info?.("docket: the service answers again");
      state.failing = null;
      await deliver(state.view.conclusions || []);
    } catch (error) {
      report(error);
    }
    return state.view;
  }

  async function deliver(items) {
    const enqueue = api.session?.workflow?.enqueueNextTurnInjection;
    if (typeof enqueue !== "function") return; // the memory log still has the conclusion
    const done = [];
    for (const item of items) {
      if (!item.session || state.delivering.has(item.card)) continue;
      state.delivering.add(item.card);
      try {
        const result = await enqueue.call(api.session.workflow, {
          sessionKey: item.session,
          text: conclusion(item),
          idempotencyKey: `docket:conclusion:${item.card}`,
          ttlMs: DAY,
        });
        if (result && result.ok === false) throw new Error(result.reason || "not queued");
        done.push(item.card);
      } catch (error) {
        log.warn?.(`docket: conclusion of ${item.handoff} not queued: ${error.message || error}`);
      } finally {
        state.delivering.delete(item.card);
      }
    }
    if (done.length) await docket.delivered(done);
  }

  function beforeToolCall(event, ctx) {
    try {
      const hit = inspect({ toolName: event.toolName, params: event.params, ctx }, state.view);
      if (!hit) return undefined;
      const command = typeof event.params?.command === "string" ? event.params.command : "";
      docket.refused({ agent: ctx?.agentId, session: ctx?.sessionKey, tool: event.toolName, rule: hit.rule,
        command: command.slice(0, 300), target: hit.target, enforced: config.enforce }).catch(() => {});
      if (!config.enforce) return undefined;
      return { block: true, blockReason: refusal(hit, ctx, config.noteCommand) };
    } catch (error) {
      log.warn?.(`docket: tool check skipped: ${error.message || error}`);
      return undefined; // never refuse because the plugin failed
    }
  }

  function messageReceived(_event, ctx) {
    // Only a nudge: Docket reads the message from the transcript, where the judge's pipeline already is.
    docket.chat(ctx?.sessionKey || null).catch(() => {});
  }

  function beforePromptBuild(_event, ctx) {
    try {
      if (!state.view || now() - state.at > config.staleMs) return undefined;
      if (!listsFor(ctx?.sessionKey, state.view, config.mainSessions)) return undefined;
      const text = openHandoffs(state.view, ctx.sessionKey, config.noteCommand);
      return text ? { prependContext: text } : undefined;
    } catch {
      return undefined;
    }
  }

  async function command(ctx) {
    const [verb = "list", id, ...words] = String(ctx?.args || "").trim().split(/\s+/).filter(Boolean);
    try {
      const view = await refresh();
      if (!view) return { text: "Docket is not running." };
      if (verb === "list") return { text: listing(view) };
      if (verb === "status" && id) return { text: status(view, id) };
      if (verb === "note" && id && words.length) {
        if (ctx?.senderIsOwner === false) return { text: "Only the owner can pass words on to a handoff." };
        const answer = await docket.note(id, words.join(" "), ctx?.sessionKey || "openclaw:/docket");
        return { text: `Passed on to the PM of ${answer.id}.` };
      }
      return { text: 'Usage: /docket list | /docket status <id> | /docket note <id> <words>' };
    } catch (error) {
      return { text: `Docket: ${error.message || error}` };
    }
  }

  return { config, state, refresh, deliver, beforeToolCall, messageReceived, beforePromptBuild, command };
}

export function register(api) {
  const docket = createDocket(api);
  api.on("before_tool_call", docket.beforeToolCall,
    { matcher: ["exec", "process", "sessions_send", "sessions_spawn", "conversations_send", "conversations_turn"], priority: 90 });
  api.on("message_received", docket.messageReceived);
  api.on("before_prompt_build", docket.beforePromptBuild);
  api.registerCommand?.({
    name: "docket",
    description: "Docket handoffs: list, status <id>, note <id> <words>",
    acceptsArgs: true,
    handler: docket.command,
  });
  // One refresh loop per Gateway process: a reload replaces the previous one instead of adding another.
  clearInterval(globalThis[TIMER]);
  const timer = setInterval(() => docket.refresh(), docket.config.refreshMs);
  timer.unref?.();
  globalThis[TIMER] = timer;
  docket.refresh();
  return docket;
}

export default define({
  id: ID,
  name: "OpenClaw Docket",
  description: "Keeps handed-off coding work on Docket: one writer into coding-agent sessions, chat follow-ups passed on, conclusions delivered back.",
  register,
});
