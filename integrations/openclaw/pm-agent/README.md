# The `docket-pm` agent

Docket's PMs run as their own OpenClaw agent, so they never load the main agent's instructions (which describe an agent that dispatches and follows up) and have only the tools a PM needs.

| File | Becomes |
|---|---|
| `workspace/AGENTS.md`, `workspace/IDENTITY.md` | the agent's workspace: the PM contract, short and stable. The rules that change per wake come with each wake message (`docket/pm.py`) |
| `agent.json` | `agents.entries.docket-pm`: no skills, a small bootstrap budget, tools limited to reading and running commands; no messaging, session or automation tools, and no sending to other agents |

The plugin (`../plugin`) refuses the commands a PM must not run (dispatching, reaching a coding agent or another session, pushing), since an exec allowlist can only allow.

Installing (done by `scripts/install.sh`):

1. copy `workspace/` to `~/Library/Application Support/docket/pm-workspace`;
2. `openclaw agents add docket-pm --workspace <that dir> --non-interactive --json`;
3. merge `agent.json` into `agents.entries.docket-pm`, and remove a `BOOTSTRAP.md` that OpenClaw may have created (it starts a first-run conversation a PM must not have);
4. set `"pm_agent": "docket-pm"` in Docket's `config.json`. New handoffs get PM sessions `agent:docket-pm:<id>`; older ones keep the session they started with.
