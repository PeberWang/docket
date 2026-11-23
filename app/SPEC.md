# Docket.app — native panel spec

The app is only a face. All logic lives in the daemon (`docket/`, Python). The app reads
state from the daemon's local HTTP API and posts the user's decisions back.

## Daemon API

Discovery: `~/Library/Application Support/docket/daemon.json`
`{"url": "http://127.0.0.1:<port>", "token": "<hex>"}` — re-read when a request fails
(the port can change on restart). Every request sends header `X-Docket-Token: <token>`.

`GET /api/state` →

```json
{
  "locale": "en",                    // "en" | "zh": the app's interface language
  "awaiting": 2,
  "delegations": [
    {
      "id": "ses_…",                 // OpenCode session id
      "title": "开源监控改本机模式",
      "state": "awaiting",           // running | pending | awaiting | limit | taken_over | closed
      "rounds": 1, "round_limit": 3,  // rounds: times the user took part (every answered card, chat mentions)
      "dispatched_at": 1794937892000, "updated_at": 1794939000000,
      "card": {                      // latest card, or null while the first run is going
        "id": 12, "kind": "report",  // report | question | failure | closed | limit
        "open": true,                // false once the user answered it
        "progress": "本机已跑通一轮，服务器上的旧任务已停。",
        "decisions": [
          {
            "id": "d1",
            "question": "服务器上的旧数据删不删？",
            "options": [
              {"id": "a", "label": "先留 30 天", "why": "留回滚余地", "recommended": true},
              {"id": "b", "label": "现在删", "why": "", "recommended": false}
            ]
          }
        ],
        "decided_for_you": ["沿用政策监控的 gog 发信", "…"],
        "conclusion": null           // set on kind = closed
      }
    }
  ]
}
```

Order is already right (awaiting first). `state = pending` means the PM is writing a card.

`POST /api/cards/<card id>/decide` body
`{"choices": {"d1": "a"}, "reply": "optional one line"}` → `202 {"ok": true}`.
Either at least one choice or a non-empty reply. The card then shows as answered and the
delegation goes to `pending`/`running`; the next `GET /api/state` reflects it.

`POST /api/delegations/<id>/close` body `{"reply": "optional"}` → 202.
`POST /api/delegations/<id>/takeover` → 202. The daemon marks it `taken_over` and asks
OpenCode Desktop to open the session. The app should also hide nothing else; a taken-over
delegation stays listed (for a week) with "Open in OpenCode", which calls the same endpoint,
and "Hand back".
`POST /api/delegations/<id>/handback` → `202 {"ok": true, "state": "pending" | "awaiting"}`.
Only from `taken_over`, otherwise 409. The PM is woken to carry on; rounds are not reset,
`handbacks` in `GET /api/state` counts how often it happened. The app sets the returned
state locally at once, then refreshes.

Errors: JSON `{"error": "…"}` with 4xx/5xx. If the daemon is unreachable, show a quiet
"Docket service isn’t running" line instead of cards.

## UI

Language: English is the design language, Chinese the second. The app follows `locale`
from `GET /api/state`; before the first answer (or while the daemon is down) it follows
the system's preferred language (zh… → Chinese, otherwise English). All copy lives in one
en/zh table (`Sources/Docket/Strings.swift`); the examples below give English with Chinese
in brackets. Calm: no sounds, no notifications, no badges in red.

**Menu bar** (`NSStatusItem`): template SF Symbol `tray` when nothing awaits, `tray.full`
plus the count (`2`) when cards await. Click toggles the panel. Right-click: Show Panel /
Collapse Panel (展开面板 / 收起面板), Quit (退出).

**Panel** (`NSPanel`, `.nonactivatingPanel`, borderless, rounded, vibrancy material,
`level = .floating`, `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`
in both states, so unless hidden no window — full-screen apps included — covers it; Hide
is the only way to make room. Does not steal focus except when the user clicks into the
reply field):

- **Collapsed (default at launch):** a slim pill docked to the right edge of the main
  screen, vertically centered, about 34×120 pt, showing the same tray symbol and the
  awaiting count in the accent color.
  Click → expand. It must never cover more than that pill.
- **Expanded:** about 380×640 pt, docked to the right edge. Header: "Docket" (案头),
  "2 need you" (2 件待你定), a collapse button. Empty list: "No handoffs" (暂无交办). Body: scrollable list of delegation cards. Esc or the collapse
  button → back to the pill. Global hotkey ⌥⌘D toggles (use a Carbon `RegisterEventHotKey`,
  no accessibility permission needed).
- Remember collapsed/expanded across launches only as "start collapsed" (always start
  collapsed).

**Card** (one per delegation):
- Title line: title, then a small state label: Needs you / Preparing / Working /
  Taken over / Closed (待你定 / 处理中 / 执行中 / 已接管 / 已结案). Below it, "Round 1 of 3"
  (第 1/3 轮) and the relative update time (`RelativeDateTimeFormatter`, localized).
- `progress` as one line of body text.
- For each decision: the question, then one button per option; the recommended option is
  visually primary and shows "Recommended: why" on its own line (Chinese: inline
  `（推荐：why）`). Selecting a button marks it; with several
  decisions the user picks one per decision.
- `decided_for_you`: a disclosure "Handled for you (N) ▸" (已代你定 N 项) listing them.
- A single-line text field "Reply…" (回一句…) and a send button. Send posts `decide` with the
  selected choices and the text. With no decisions on the card, send posts just the reply.
- Buttons: Close → Confirm close (结案 → 确认结案, two clicks), Open in OpenCode
  (在 OpenCode 打开, takeover). When `kind = limit` (rounds used up) show a line
  "3 rounds used. Take it over in OpenCode." (已满 3 轮，建议接管) and make Open in OpenCode
  the primary button.
- Closed/answered cards are dimmed and show `conclusion` or the user's answer.
- `running`/`pending` cards show a small spinner text: Working… / Preparing the next
  report… (执行中… / PM 整理中…)

Refresh: poll `GET /api/state` every 2 s while expanded, every 10 s while collapsed
(the pill count must stay current).

## Build

SwiftPM executable, macOS 14+, Swift 6 toolchain from Command Line Tools (no Xcode).
`app/Package.swift`, sources in `app/Sources/Docket/`. A script `app/build.sh` that runs
`swift build -c release`, assembles `build/Docket.app` (Info.plist with
`LSUIElement = true`, bundle id `io.github.peberwang.docket`, name "Docket"; the
executable is `Docket`, which launchd points at), ad-hoc signs it
(`codesign -s - --force --deep`), and prints the path. No third-party packages.
For development, `app/dev-daemon.py` is a tiny fake daemon serving the API above with two
sample delegations (`--all` adds limit / taken over / closed, `--locale en|zh` picks the
copy, `--discovery PATH` writes a separate discovery file). Point a debug build at it with
launch arguments, which don't touch the installed app's preferences:
`build/Docket.app/Contents/MacOS/Docket -debugDiscoveryFile <path> -debugStartExpanded YES`.
