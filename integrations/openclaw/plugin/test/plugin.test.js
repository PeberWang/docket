import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, before, beforeEach, test } from "node:test";

import entry, { createDocket, register } from "../index.js";

// A fake Docket service: daemon.json in a temp data directory, the four routes the plugin uses.
const calls = [];
let view;
let server;
let home;

function fresh() {
  view = {
    version: 1,
    round_limit: 3,
    sessions: { ses_abc123XYZ: "ses_abc123XYZ" },
    pm_agent: "docket-pm",
    pm_prefixes: ["agent:docket-pm:", "agent:main:desk-", "agent:main:docket-"],
    pm_sessions: { "agent:docket-pm:abc123XYZ": "ses_abc123XYZ" },
    handoffs: [{ id: "ses_abc123XYZ", title: "list view", state: "awaiting", rounds: 1, source: "agent:main:telegram",
      needs_you: true, card: "Sort by date or by name?" }],
    conclusions: [{ card: 7, handoff: "ses_old", title: "export", session: "agent:main:main", text: "Export done.", at: 0 }],
  };
}

before(async () => {
  home = await mkdtemp(path.join(os.tmpdir(), "docket-plugin-"));
  server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      if (req.headers["x-docket-token"] !== "t0ken") {
        res.writeHead(401).end("{}");
        return;
      }
      calls.push({ method: req.method, url: req.url, body: body ? JSON.parse(body) : null });
      const reply = (status, answer) => res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(answer));
      if (req.url === "/api/openclaw") return reply(200, view);
      if (req.url === "/api/openclaw/delivered") {
        const cards = JSON.parse(body).cards;
        view.conclusions = view.conclusions.filter((c) => !cards.includes(c.card));
        return reply(200, { ok: true, delivered: cards.length });
      }
      if (req.url.endsWith("/note")) return reply(202, { ok: true, id: "ses_abc123XYZ", rounds: 1 });
      return reply(202, { ok: true });
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  await writeFile(path.join(home, "daemon.json"), JSON.stringify({ url: `http://127.0.0.1:${server.address().port}`, token: "t0ken", pid: 1 }));
});

after(() => server.close());

beforeEach(() => {
  calls.length = 0;
  fresh();
});

function fakeApi({ enqueue = async () => ({ ok: true }), config = {} } = {}) {
  const hooks = {};
  const injected = [];
  const api = {
    pluginConfig: { home, noteCommand: "docket", ...config },
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    on(name, handler, opts) { hooks[name] = { handler, opts }; },
    registerCommand(def) { hooks["/docket"] = def; },
    session: { workflow: { enqueueNextTurnInjection: async (item) => { injected.push(item); return enqueue(item); } } },
  };
  return { api, hooks, injected };
}

const until = async (check) => {
  for (let i = 0; i < 100 && !check(); i += 1) await new Promise((r) => setTimeout(r, 10));
};

test("the entry is a plain plugin object with the manifest's id", () => {
  assert.equal(entry.id, "docket");
  assert.equal(typeof entry.register, "function");
});

test("register wires the hooks, the command and one refresh loop", async () => {
  const { api, hooks } = fakeApi();
  register(api);
  register(api); // a reload: still one loop
  assert.deepEqual(Object.keys(hooks).sort(), ["/docket", "before_prompt_build", "before_tool_call", "message_received"]);
  assert.ok(hooks.before_tool_call.opts.matcher.includes("exec"));
  clearInterval(globalThis[Symbol.for("openclaw-docket.refresh")]);
});

test("a write into a handoff session is refused and recorded; without a view it goes through", async () => {
  const { api } = fakeApi();
  const docket = createDocket(api);
  const call = { toolName: "exec", params: { command: "opencode-dispatch -s ses_abc123XYZ 'go on'" } };
  assert.equal(docket.beforeToolCall(call, { agentId: "main", sessionKey: "agent:main:main" }), undefined); // no view yet
  await docket.refresh();
  const answer = docket.beforeToolCall(call, { agentId: "main", sessionKey: "agent:main:main" });
  assert.equal(answer.block, true);
  assert.match(answer.blockReason, /docket note ses_abc123XYZ/);
  await until(() => calls.some((c) => c.url === "/api/openclaw/refused"));
  const refused = calls.find((c) => c.url === "/api/openclaw/refused").body;
  assert.deepEqual([refused.rule, refused.target, refused.agent], ["write-into-handoff", "ses_abc123XYZ", "main"]);
});

test("observe-only (enforce false) records the refusal and lets the call through", async () => {
  const { api } = fakeApi({ config: { enforce: false } });
  const docket = createDocket(api);
  await docket.refresh();
  const call = { toolName: "exec", params: { command: "opencode run -s ses_abc123XYZ x" } };
  assert.equal(docket.beforeToolCall(call, { agentId: "main", sessionKey: "agent:main:main" }), undefined);
  await until(() => calls.some((c) => c.url === "/api/openclaw/refused"));
  assert.equal(calls.find((c) => c.url === "/api/openclaw/refused").body.enforced, false);
});

test("a broken check never refuses", async () => {
  const { api } = fakeApi();
  const docket = createDocket(api);
  await docket.refresh();
  docket.state.view = { get sessions() { throw new Error("boom"); }, pm_prefixes: [] };
  assert.equal(docket.beforeToolCall({ toolName: "exec", params: { command: "opencode run -s ses_abc123XYZ x" } }, {}), undefined);
});

test("an unreachable service is one warning and no refusals", async () => {
  const warnings = [];
  const { api } = fakeApi({ config: { home: path.join(home, "nowhere") } });
  api.logger.warn = (text) => warnings.push(text);
  const docket = createDocket(api);
  await docket.refresh();
  await docket.refresh();
  assert.equal(warnings.length, 1);
  assert.equal(docket.beforeToolCall({ toolName: "exec", params: { command: "opencode run -s ses_abc123XYZ x" } }, {}), undefined);
});

test("conclusions are queued into their chat once and acknowledged", async () => {
  const { api, injected } = fakeApi();
  const docket = createDocket(api);
  await docket.refresh();
  assert.equal(injected.length, 1);
  assert.equal(injected[0].sessionKey, "agent:main:main");
  assert.equal(injected[0].idempotencyKey, "docket:conclusion:7");
  assert.match(injected[0].text, /Docket closed the handoff "export": Export done\./);
  assert.deepEqual(calls.find((c) => c.url === "/api/openclaw/delivered").body, { cards: [7] });
  await docket.refresh();
  assert.equal(injected.length, 1); // acknowledged: the service no longer lists it
});

test("a conclusion OpenClaw would not queue is not acknowledged", async () => {
  const { api, injected } = fakeApi({ enqueue: async () => { throw new Error("prompt modification disabled"); } });
  const docket = createDocket(api);
  await docket.refresh();
  assert.equal(injected.length, 1);
  assert.equal(calls.some((c) => c.url === "/api/openclaw/delivered"), false);
});

test("the main session and the dispatching chat get the list; others do not", async () => {
  const { api } = fakeApi();
  const docket = createDocket(api);
  assert.equal(docket.beforePromptBuild({}, { sessionKey: "agent:main:main" }), undefined); // no view yet
  await docket.refresh();
  const main = docket.beforePromptBuild({}, { sessionKey: "agent:main:main" });
  assert.match(main.prependContext, /ses_abc123XYZ "list view": waiting for the person: Sort by date or by name\?/);
  assert.match(main.prependContext, /docket note <id> "<their words>" --from agent:main:main/);
  assert.ok(docket.beforePromptBuild({}, { sessionKey: "agent:main:telegram" })); // it handed this one off
  assert.equal(docket.beforePromptBuild({}, { sessionKey: "agent:other:main" }), undefined);
  view.handoffs = [];
  await docket.refresh();
  assert.equal(docket.beforePromptBuild({}, { sessionKey: "agent:main:main" }), undefined); // nothing open: nothing added
});

test("a stale view is left out of prompts", async () => {
  let clock = 1_000_000;
  const { api } = fakeApi();
  const docket = createDocket(api, { now: () => clock });
  await docket.refresh();
  clock += 3 * 60 * 1000;
  assert.equal(docket.beforePromptBuild({}, { sessionKey: "agent:main:main" }), undefined);
});

test("a chat message is a nudge with no text", async () => {
  const { api } = fakeApi();
  createDocket(api).messageReceived({ content: "private words" }, { sessionKey: "agent:main:main" });
  await until(() => calls.some((c) => c.url === "/api/openclaw/chat"));
  assert.deepEqual(calls.find((c) => c.url === "/api/openclaw/chat").body, { session: "agent:main:main" });
});

test("/docket lists, shows and passes words on for the owner only", async () => {
  const { api } = fakeApi();
  const docket = createDocket(api);
  assert.match((await docket.command({ args: "" })).text, /ses_abc123XYZ {2}awaiting {2}list view/);
  assert.match((await docket.command({ args: "status abc" })).text, /rounds {2}1\/3/);
  assert.match((await docket.command({ args: "note abc 按时间排", senderIsOwner: false })).text, /Only the owner/);
  const passed = await docket.command({ args: "note abc 按时间排", senderIsOwner: true, sessionKey: "agent:main:main" });
  assert.match(passed.text, /Passed on to the PM of ses_abc123XYZ/);
  assert.deepEqual(calls.find((c) => c.url.endsWith("/note")).body, { text: "按时间排", source: "agent:main:main" });
  assert.match((await docket.command({ args: "frobnicate" })).text, /Usage/);
});
