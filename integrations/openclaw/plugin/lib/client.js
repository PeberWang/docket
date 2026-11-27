// Talking to the Docket service: it writes its address and token to daemon.json (0600) in its data
// directory each time it starts, so the token is read from there again whenever a call is refused.

import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export function dataDir(config = {}) {
  return config.home || process.env.DOCKET_HOME || path.join(os.homedir(), "Library", "Application Support", "docket");
}

export class Client {
  constructor(config = {}) {
    this.file = path.join(dataDir(config), "daemon.json");
    this.timeoutMs = config.timeoutMs || 1500;
    this.service = null;
  }

  async discover() {
    this.service = JSON.parse(await readFile(this.file, "utf8"));
    return this.service;
  }

  /** One request; the address is looked up again once if the service restarted meanwhile. */
  async call(method, route, body, { timeoutMs } = {}) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const service = this.service && attempt === 0 ? this.service : await this.discover();
      let response;
      try {
        response = await fetch(`${service.url}${route}`, {
          method,
          headers: { "Content-Type": "application/json", "X-Docket-Token": service.token },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(timeoutMs || this.timeoutMs),
        });
      } catch (error) {
        if (attempt === 0 && error.name !== "TimeoutError") continue; // refused: a restarted service listens elsewhere
        throw error;
      }
      if (response.status === 401 && attempt === 0) continue; // a new token after a restart
      const answer = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(answer.error || `Docket answered ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return answer;
    }
    throw new Error("the Docket service is not reachable");
  }

  view() {
    return this.call("GET", "/api/openclaw");
  }

  delivered(cards) {
    return this.call("POST", "/api/openclaw/delivered", { cards });
  }

  refused(detail) {
    return this.call("POST", "/api/openclaw/refused", detail);
  }

  chat(session) {
    return this.call("POST", "/api/openclaw/chat", { session });
  }

  note(handoff, text, source) {
    return this.call("POST", `/api/delegations/${encodeURIComponent(handoff)}/note`, { text, source }, { timeoutMs: 10_000 });
  }
}
