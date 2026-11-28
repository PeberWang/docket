import assert from "node:assert/strict";
import { test } from "node:test";

import { commandText, inspect, refusal, RULES } from "../lib/policy.js";

const VIEW = {
  sessions: { ses_abc123XYZ: "ses_abc123XYZ", ses_moved99999: "ses_abc123XYZ" },
  pm_agent: "docket-pm",
  pm_prefixes: ["agent:docket-pm:", "agent:main:desk-", "agent:main:docket-"],
  pm_sessions: { "agent:docket-pm:abc123XYZ": "ses_abc123XYZ" },
};
const MAIN = { agentId: "main", sessionKey: "agent:main:main" };
const PM = { agentId: "docket-pm", sessionKey: "agent:docket-pm:abc123XYZ" };

const exec = (command, ctx = MAIN) => inspect({ toolName: "exec", params: { command }, ctx }, VIEW);

test("a follow-up written straight into a handoff's session is refused", () => {
  for (const command of [
    "opencode-dispatch -s ses_abc123XYZ 'go on'",
    "cd ~/p && opencode-dispatch --detach --session=ses_abc123XYZ \"next\"",
    "opencode run -s ses_abc123XYZ 'fix the test'",
    "opencode run --model x --session ses_moved99999 'again'",
    "opencode api post /api/session/ses_abc123XYZ/prompt --data '{\"text\":\"hi\"}'",
    "curl -X POST http://127.0.0.1:4096/api/session/ses_abc123XYZ/interrupt",
    "curl -s --json '{\"text\":\"x\"}' localhost:4096/api/session/ses_abc123XYZ/message",
    'opencode run -s "ses_abc123XYZ" "fix it"',
    "opencode-dispatch --session='ses_abc123XYZ' 'go on'",
  ]) {
    const hit = exec(command);
    assert.equal(hit?.rule, RULES.write, command);
    assert.equal(hit.handoff, "ses_abc123XYZ", command);
  }
});

test("reads, other sessions and new dispatches go through", () => {
  for (const command of [
    "opencode api get /api/session/ses_abc123XYZ/message",
    "curl -s localhost:4096/api/session/ses_abc123XYZ/diff",
    "opencode run -s ses_someoneElse1 'my own session'",
    "desk-dispatch --title 'list view' 'build it'",
    "ls -la && git status",
  ]) {
    assert.equal(exec(command), null, command);
  }
});

test("Code Mode's code is read like a command", () => {
  const hit = inspect({ toolName: "exec", params: { code: "await tools.exec({command: 'opencode run -s ses_abc123XYZ go'})" }, ctx: MAIN }, VIEW);
  assert.equal(hit?.rule, RULES.write);
  assert.equal(commandText({ command: "a", code: "b", other: 1 }), "a\nb");
});

test("nothing is refused without a view: the plugin fails open", () => {
  assert.equal(inspect({ toolName: "exec", params: { command: "opencode run -s ses_abc123XYZ x" }, ctx: MAIN }, null), null);
  assert.equal(inspect(null, VIEW), null);
  assert.equal(inspect({ toolName: "exec", params: null, ctx: MAIN }, VIEW), null);
});

test("sessions_send into a PM session is refused and names the handoff", () => {
  const hit = inspect({ toolName: "sessions_send", params: { sessionKey: "agent:docket-pm:abc123XYZ", message: "go on" }, ctx: MAIN }, VIEW);
  assert.deepEqual([hit.rule, hit.handoff], [RULES.pmSend, "ses_abc123XYZ"]);
  const old = inspect({ toolName: "sessions_send", params: { target: { key: "agent:main:desk-oldone" } }, ctx: MAIN }, VIEW);
  assert.equal(old.rule, RULES.pmSend);
  assert.equal(inspect({ toolName: "sessions_send", params: { sessionKey: "agent:main:telegram" }, ctx: MAIN }, VIEW), null);
});

test("a PM may read and test, never dispatch, reach a coding agent or another session", () => {
  for (const command of ["pytest -q", "cat src/app.py", "git diff", "npm test"]) {
    assert.equal(exec(command, PM), null, command);
  }
  for (const command of ["opencode run 'x'", "desk-dispatch --title t x", "docket note ses_a 'x'", "claude -p 'x'",
    "codex exec 'x'", "openclaw agent --session-key k", "git push origin main", "opencode api post /api/session"]) {
    assert.equal(exec(command, PM)?.rule, RULES.pmAct, command);
  }
  assert.equal(inspect({ toolName: "sessions_send", params: { sessionKey: "agent:main:main" }, ctx: PM }, VIEW).rule, RULES.pmAct);
  // A PM running in the main agent is known by its session key.
  assert.equal(exec("opencode run x", { agentId: "main", sessionKey: "agent:main:docket-abc" })?.rule, RULES.pmAct);
});

test("the refusal names the one command to use instead", () => {
  const text = refusal({ rule: RULES.write, handoff: "ses_abc123XYZ" }, MAIN, "docket");
  assert.match(text, /docket note ses_abc123XYZ "<the person's words>" --from agent:main:main/);
  assert.match(text, /--supersedes ses_abc123XYZ/);
  assert.match(refusal({ rule: RULES.pmAct, reason: "push" }, PM, "docket"), /does not push/);
});
