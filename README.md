# OpenClaw Docket

*A panel between your OpenClaw chat and the coding agent, for the decisions that can wait.*

A chat is synchronous: one question you have not answered holds up the whole conversation. A coding agent (OpenCode, Claude Code or Codex) is parallel: handed-off tasks move forward in the background, each on its own. Between the two there is no place where a judgment can pause, pile up and arrive late. OpenClaw Docket is that place, a native macOS panel. Everything that needs you, but not now, lands there.

Behind the panel, every handoff gets a project manager: an agent that wakes when something happens, checks the work, decides the technical details itself and asks you only what it cannot decide. Your attention is treated as the scarce thing: a handoff may ask you three times, and everything it did without you is written down where you can look.

<p align="center"><img src="docs/panel-en.png" width="380" alt="A Docket card: where the work stands in one sentence, two decisions with a recommended option each, and what was decided for you"></p>

## Five-minute quick start

You need macOS 14+, `xcode-select --install`, a working [OpenClaw](https://openclaw.ai) and one coding agent: [OpenCode](https://opencode.ai) 2.2 or later, or the Claude Code or Codex CLI, logged in.

```sh
git clone https://github.com/PeberWang/docket.git && cd docket
/usr/bin/python3 -I scripts/openclaw_setup.py --dry-run   # optional: see what it will change in OpenClaw
bash scripts/install.sh
scripts/docket doctor                                     # every link, and the fix for any that fails
```

1. A tray icon appears in the menu bar and a pill on the right screen edge (⌥⌘D opens the panel).
2. Add two lines to `~/.openclaw/workspace/AGENTS.md`:
   `Hand coding work off with the desk-dispatch skill, reply with one line, and leave the rest to Docket.`
   `When I say more about a handed-off task, pass my words on with docket note and reply with one line.`
3. In your OpenClaw chat: "Hand off: add a section on testing to the README of ~/my-project."
4. The chat answers with one line. The handoff shows up on the panel at once; when the run ends, its PM checks the result and either closes it or puts a card up for you.

No judge key is needed to get here. Without one, Settings → Models shows the judge as not configured, and cards arrive without background picked from your chat.

## How it works

### Three rhythms

The chat stays a chat: handing off gets one line back ("Handed off: Testing docs. Follow-ups are on Docket."), and nothing from the work comes back into it to wait for an answer. What you said about the task still reaches it in your own words: a judge model picks the messages from your chat that belong to the task and hands them to the PM verbatim. On 17 real handoffs it kept 97% of the relevant messages and all of the ones that stated requirements.

The coding agent works in its own session, in the background. Docket starts the session itself, gives every instruction its own id and follows it until the run ends.

The panel is the part that can be late. A card says where things stand in one sentence and asks at most two questions, each with a recommended option. It waits, as a small pill on the screen edge with no notifications and no sounds, until you have a minute. You click or write one line, and the work goes on.

### A project manager per handoff

The PM is not a loop and never polls. Code keeps watch; the model only judges. It is woken once when a run ends, when you answer a card, or when you say something about the work, and each time it answers with exactly one action, which Docket checks in code and carries out:

- `relay`: the next instruction for the coding agent, when what you already decided settles it;
- `card`: what you must decide, as a brief with at most two questions;
- `close`: the work is done, with a one-sentence conclusion;
- `hold`: nothing to send now.

After a run the PM verifies the delivery itself (it reads the files and runs the project's tests) and lists the technical calls it made for you, folded away on the card. It may move the work on by itself up to five times in a row before it shows you where things stand.

### Your attention is the budget

- **A round is you answering a card.** Nothing else costs one: words you pass on, the PM's own relays, retries, taking over. After three rounds the PM writes a handoff note and you close it or continue in OpenCode yourself.
- **Old cards go.** A newer question replaces an older one, news never stands in front of a decision, and a card nobody answers closes the handoff after 12 hours with a note. A card is never closed just because a run ended.
- **No "did you mean this?" cards.** When the main agent passes your words on, or the judge finds them in the chat, the PM acts on them.
- **Risky acts wait for your yes.** An instruction that would push, publish, deploy, delete outside the project, run a script from the network or change the system goes on a card first, unless your own words asked for exactly that.
- **Permissions in two steps** (with [opencode-permission-notify](https://github.com/PeberWang/opencode-permission-notify)). It sorts every OpenCode permission request into six classes, and you choose, per class, to be asked, to let it through, or to hand it to Docket's PM. Handed to the PM, a reversible request goes through and the PM reviews it when it checks the run; an irreversible one is held until the PM decides, and comes to you when the PM is unsure. In a session that is not a handoff there is no PM, and you are asked.

### One writer, and a record of everything

Only Docket hands instructions to the coding agent and wakes a PM. Anyone else (you, the main chat, another session) passes a word on with `docket note <handoff> "<words>"` or **Pass a word on** on the card. An OpenClaw plugin holds the line: it refuses commands that would write into a handoff's session directly and tells the agent the command to use instead, gives the main agent a short list of open handoffs, and brings each conclusion back into the chat that dispatched the work, once. It never blocks OpenClaw: when it cannot tell, it lets the call through.

Every state change, instruction, PM wake, card, permission decision and refusal is an event in the ledger. `docket status <handoff>` shows one handoff's history, `docket audit report` writes a full report from a copy of the ledger and the logs, and a daily health summary counts how often work got stuck, healed by itself or needed you.

## Simpler alternatives

### Just keep following up in the chat

A chat is synchronous: a question you have not answered blocks the conversation, and every report lands between everything else you are doing. Your agent's habit of expanding on things is exactly wrong for a task you handed off so you would not have to think about the details. Docket is asynchronous and terse on purpose.

### Just open the session in OpenCode, or use an [inbox](https://github.com/PeberWang/opencode-openclaw-inbox)

That means reading the coding agent's transcript and, in practice, steering technical details yourself. An inbox tells you a session exists; Docket tells you what was decided and asks only what is left. The two fit together: start with the inbox if all you want is to see dispatched sessions and jump into them.

### Just let the agent run and check the result later

You would get a long technical summary written by the agent that did the work. Docket's PM is a second agent that verifies the delivery and turns it into a decision, and its output is checked in code: lengths, number of questions, exactly one recommended option.

### Just tell the coding agent to write shorter summaries

Shorter is not the problem; deciding what needs you is. The PM keeps a running record of what you said, what was decided and how many rounds are left, makes the technical calls itself, and lists them where you can check them.

## Architecture

```
 you ── chat ──▶ OpenClaw Gateway ─────────────────────────────────┐
  │               main agent: desk-dispatch, docket note             │
  │               docket plugin: one writer, open handoffs,          │
  │                 conclusions back, /docket                        │
  │               PM agent docket-pm: one session per handoff        │
  │               └──────────────────┬───────────────────────────────┘
  │                                  │ local HTTP (token)
  │   Docket.app ◀── local HTTP ───▶ Docket service (launchd)
  │   docket CLI                     │  ledger · state machine · PM wakes
  └──────── panel ───────────────────│  engines · watchdog · judge · audit
                                     ▼
                  OpenCode (HTTP and events) · Claude Code / Codex (CLI)
```

The design and its reasons are in [docs/design.md](docs/design.md); the parts, the state machine, the closings and the integration in [docs/architecture.md](docs/architecture.md). How version 0.2 was made stable from ten days of its own records is in [docs/case-study.md](docs/case-study.md).

## Installation

### Dependencies

- **Required:** macOS 14 or later and Command Line Tools (`xcode-select --install`, which brings Swift and `/usr/bin/python3`). Docket uses only the Python standard library and has no Swift packages.
- **Required:** [OpenClaw](https://openclaw.ai) with `openclaw` on your `PATH` (tested with 2026.11.6). The PM runs through `openclaw agent`, so it uses whichever model and credentials your OpenClaw has.
- **Required, at least one coding agent:** OpenCode 2.2 or later (driven over its HTTP API; tested with 2.2.7), or the [Claude Code](https://code.claude.com) / [Codex](https://developers.openai.com/codex) CLI, logged in.
- **Optional:** [opencode-permission-notify](https://github.com/PeberWang/opencode-permission-notify) for permission classes and the approval popup; the [inbox plugin](https://github.com/PeberWang/opencode-openclaw-inbox) to see dispatched sessions in OpenCode.
- **Optional, the judge:** a [TypeSafe](https://console.typesafe.ai) key for System One, or any OpenAI-compatible endpoint with its model and key.

### Install

`bash scripts/install.sh` does everything once and can be run again to upgrade:

1. builds Docket.app into `~/Applications`;
2. copies the service's code to `~/Library/Application Support/docket/lib` and runs it from there as a launch agent, so the clone can move or change without touching the running service (`scripts/deploy-service.sh` does just this step after a pull);
3. wires OpenClaw (`scripts/openclaw_setup.py`): links and enables the plugin, creates the `docket-pm` agent with its own workspace and tool policy, and installs the `desk-dispatch` skill, backing up any copy you changed. Every change to `openclaw.json` is one validated `openclaw config patch`; `--dry-run` shows it first.

Then add the two lines to `AGENTS.md` from the quick start, and optionally save a judge key in Settings → Models (it goes to the keychain).

`bash scripts/uninstall.sh` removes the launch agents, the app, the skill and the plugin link, keeps the PM agent and your handoff history (it prints where and how to delete them), and `--purge` deletes the history too.

## Daily use

- Ask OpenClaw to hand something off, as you normally would. To redo one, it hands off again with `--supersedes <handoff>`, and the old one closes as replaced.
- When a card is ready, the pill shows a count. Click it or press ⌥⌘D. Pick an option or write one line. **Mark done** closes the handoff; **Open in OpenCode** (or **Continue in Terminal**) hands the session to you, and **Hand back** returns it.
- Say more about a task in the chat whenever you like; the main agent passes it on. In the chat, `/docket list`, `/docket status <id>` and `/docket note <id> <words>` reach Docket directly.
- A card marked **Stuck** or **Docket fault** is Docket saying it could not go on by itself; **Retry** sends the same thing again. Docket's own faults never close a handoff.
- `desk-dispatch --engine claude` or `--engine codex` picks another coding agent for one task.

## Configuration

`~/Library/Application Support/docket/config.json`. Changes apply within seconds, except `locale`, which applies after a restart (`scripts/deploy-service.sh`); a value of the wrong type is logged and replaced by its default.

| Key | Default | Meaning |
|---|---|---|
| `watch` | `["*"]` | OpenClaw sessions whose handoffs go to Docket (`fnmatch` patterns), e.g. `["agent:main:main"]` |
| `locale` | `"auto"` | `"auto"` follows macOS; `"en"` or `"zh"`. The panel and the PM's cards follow it |
| `engine` | `"opencode"` | default coding agent: `opencode`, `claude` or `codex` |
| `pm_agent` | `"main"` | the OpenClaw agent PMs run in; the installer sets `"docket-pm"` once it has created that agent |
| `pm_model` | `null` | model for the PM; `null` uses the agent's default |
| `pm_fallback_model` | `null` | model for a third try when the PM's reply failed twice |
| `fallback_model` | `null` | OpenCode model (`provider/model`) to retry a run once with after a usage limit |
| `prefix` | `""` | a title prefix for handoff sessions, e.g. the inbox plugin's marker; Docket puts it on the sessions it opens and looks for it on sessions others open |
| `continue_limit` | `5` | how many times in a row the PM may move the work on by itself after a run |
| `watchdog` | `"observe"` | `"observe"` records what the watchdog would do; `"act"` resends an instruction that never showed up, went quiet or ended unfinished, once, then puts it on a card |
| `quiet_wake_minutes` | `180` | a card unanswered this long, with the agent idle, wakes its PM once to close it or ask one sharper question |
| `quiet_expire_minutes` | `720` | a card unanswered this long closes the handoff with a note |
| `stale_enabled` | `true` | look after unanswered cards at all (the two keys above) |
| `stale_threshold` | `0.9` | how sure the judge must be that you answered a card in the chat |
| `attribute` | `true` | recognise later mentions of a handoff in the chat |
| `context_threshold` | `0.5` | how sure the judge must be to keep a chat message as background |
| `move_threshold` | `0.85` | how sure it must be that a chat message belongs to a handoff |
| `reconcile_max_age_hours` | `6` | on restart, a pending handoff idle longer than this is closed as overdue instead of woken |
| `health_dir` | `null` | where the daily health summary goes; `null` = `health/` next to the ledger |
| `judge` | `{}` | `backend` (`"systemone"` or `"openai"`), `model`, `endpoint`, `keychain_service`; easier from Settings → Models |
| `engines` | | per-agent settings, e.g. `{"claude": {"args": [...]}}` or `{"opencode": {"transport": "cli"}}` |

## Privacy

- The service listens on `127.0.0.1` only, with a random token per start.
- The judge sees only the sessions you watch, only user and assistant text, and only a recent window; credentials are stripped first (`judge/redact.py`). [TypeSafe's policy](https://typesafe.ai/legal/privacy-policy) says inputs are not used for training.
- The PM runs through your own OpenClaw, so its prompts go to whichever model your OpenClaw uses. The plugin sends no chat text to Docket; it only says that a message arrived.

## Status

Used daily by its author with OpenCode. Known limits:

- macOS only. The transcript reader reads OpenClaw 2026.9.x and 2026.11.x; after upgrading OpenClaw, run `scripts/docket doctor` and `/usr/bin/python3 -I scripts/selfcheck.py`.
- The Claude Code path is tested end to end; the Codex path follows the official CLI docs but has not been run yet.
- The OpenClaw plugin API is experimental upstream; the plugin is tested against 2026.11.6.
- The round limit is fixed at 3. The panel docks to the main screen.

Development: `/usr/bin/python3 -I -m unittest discover -s tests -p 'test_*.py'` runs the service's tests and the plugin's (`node --test`); `bash app/build.sh` builds the app (with Command Line Tools only, `ld` warns that two search paths under `/Library/Developer/CommandLineTools/Developer` are not found, which is harmless); `app/dev-daemon.py` is a fake service for UI work; `eval/` holds the judge's evaluation harness, which needs your own data. Changes are in [CHANGELOG.md](CHANGELOG.md).

## 中文说明

*夹在 OpenClaw 聊天和编程代理之间的一块面板，放那些可以晚一点再定的事。*

聊天是同步的，一个没回答的问题会卡住整条对话；编程代理（OpenCode、Claude Code 或 Codex）是并行的，交办出去的任务在后台各自推进。两者之间缺一个可以停、可以攒、可以迟到的地方，「案头」就是这个地方，一块 macOS 原生面板。所有「需要你，但不是现在」的事都落在这里。

面板背后，每件交办有一个项目经理（PM）：出了事才被叫醒，自己核实交付、替你定技术细节，只把定不了的事问你。你的注意力被当作稀缺资源：一件交办最多问你三次，它没问你就做了的事，都记在你看得到的地方。

<p align="center"><img src="docs/panel-zh.png" width="380" alt="案头面板：一句话进展、两个带推荐选项的问题、已代你定的事"></p>

**三种节奏。** 聊天里只回一句「已交办」，不追问、不汇报；判断器从聊天里挑出你关于这件事的原话交给 PM，不靠转述。编程代理在自己的会话里干活，由案头自己建会话、给每条指令编号、一路跟到这一轮结束。面板可以迟到：一句话进展加最多两个问题，每个问题带推荐选项，等你有空再看。

**每件交办一个 PM。** 它不是循环，也不轮询：代码盯着，模型只做判断。这一轮结束、你答了卡、或你又说了相关的话，它才醒一次，每次只回一个动作（续派、出卡、结案、暂存），案头在代码里校验后执行。跑完一轮它自己核实交付，最多连续自己往下推五次，再停下来给你看进展。

**注意力是预算。** 一轮就是你答一张卡，转达原话、PM 自己续派、重试、接管都不算；三轮用完，PM 写好交接说明，由你结案或自己接着做。旧卡被取代就撤，12 小时没人答就带说明结案，不会因为「跑完了」就关掉你的卡。不出「这句话你是不是这个意思」的确认卡。推送、发布、部署、删项目外的文件、跑网上的脚本、动系统设置，一律先等你点头，除非你的原话正是要这么做。配合 opencode-permission-notify，权限分两步：它把 OpenCode 的每个授权请求归成六类，你按类选「问我」「放行」或「交给案头 PM」；交给 PM 的，可撤回的先放行、由 PM 在核实时复核，不可逆的先停下、由 PM 判断，拿不准再问你；不是案头交办的会话里没有 PM，照样问你。

**只有案头能写，每一步都有记录。** 只有案头能给编程代理派活、能叫醒 PM；别处想补一句，用 `docket note <交办> "<原话>"` 或卡片上的「转达一句」。OpenClaw 插件守着这条线：拦下直接往交办会话里写的命令并告诉对方该用哪条命令，给主代理一份在办清单，把结论送回派活的那段聊天，只送一次；它判断不了时一律放行，不会卡住 OpenClaw。每次状态变化、指令、PM 唤醒、卡片、授权判定和拦截都记进台账；`docket status`、`docket audit report` 和每日健康摘要都从这里来。

安装：`bash scripts/install.sh`（先跑 `scripts/openclaw_setup.py --dry-run` 可以预览它对 OpenClaw 的改动），装完用 `scripts/docket doctor` 检查每一环。卸载：`bash scripts/uninstall.sh`，保留交办记录，加 `--purge` 一并删除。没有判断模型的密钥也能用，设置窗口会显示「未配置」。

## License

MIT
