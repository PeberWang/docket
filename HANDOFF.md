# 交接：轮次预算三轮硬上限，案头自身故障不结案（对应 BRIEF_round_budget.md）

## 1. 计数点在哪

规则是人每参与一次就算一轮。计数只在两个地方发生，都走 `ledger.spend_round()`。这个函数在一个 `begin immediate` 事务里同时做三件事：把卡片置为已解决（条件是 `resolved_at is null`）、`rounds + 1`、写一条 `round` 事件。卡片已经被解决过时（双击、两个请求同时到达）返回 None，不计数。

- `Docket.decide()`：任何一张卡被人在面板上解决，包括普通简报卡、问题卡、权限卡，计一轮。权限卡答完不再另外计。
- `Docket.attribute()`：裁判把聊天里的一句话归到某个交办（mention），计一轮。这句话顺带解决了当时开着的卡，也只算这一轮。

不计数的情形：

- `relay()` 不再加一，计数已经在人解决卡片时完成。
- mention 之后弹出的「按这句话续派？」确认卡带 `round_spent: true`，确认它和那句聊天算同一次参与，不重复计。
- `replay()`（重放旧决定）不计。
- failure 卡上的「重试」按钮（`pm_retry`）不计，那是案头自己出的故障。
- 结案、接管、权限卡过期、卡片被取代、自动批准的权限、心跳、sweep、超时都不计。

## 2. 上限怎么挡

`ROUND_LIMIT = 3`，新增状态 `limit`。

- 进入 limit：第 3 轮那次参与先被执行，然后 `check_limit()` 调 `enter_limit()`。比如第 3 次回答触发的续派照常发给 OpenCode，不会被吞掉。`enter_limit()` 会撤下所有等待中的卡，挂一张 limit 卡，状态置为 `limit`。
- 第一道闸 `show_card()`：`rounds >= 3` 时，除 limit 卡本身之外一律拒绝，记 `card:blocked`（带被拒卡片的 kind 和 progress），返回 None。权限卡也走这条路，同样被拒。唯一的例外是 mention 的续派确认卡，因为那一轮已经由 mention 付过了。
- 第二道闸 `relay()`：保留。改成按真实续派次数判断（`relayed` 事件，fallback 重发不算），满 3 次再续派就记 `relay:blocked`，并进入 limit。
- `ledger.set_state()`：处于 limit 时，除 `closed` 和 `taken_over` 以外的状态切换一律留在 limit。不靠提示词自觉。
- `decide()`：limit 状态下或对 limit 卡作答，返回 409「轮次已用完，请接管或结案」。mention 遇到 rounds 已满的交办，记 `mention:blocked`，不计数也不唤醒 PM。
- 收口：limit 期间 OpenCode 跑完一轮时，`write_card()` 改走 `wrap_up()`，用 `pm.wrap_up_wake()` 让 PM 只准回 close，并且必须写 `remaining`（未完成事项）。这份交接说明写进 limit 卡，人点结案时直接用它；人结案时补了一句话，就重新让 PM 收口一次。`finish()` 会把 `remaining` 拼进结论（「未完成：…」），同时写进 OpenClaw 记忆。接管照旧。

## 3. 案头自身故障不再结案

- 解析（`pm.json_objects` / `extract_json` / `pick`）：依次尝试整段文本、代码块、散文里用 `raw_decode` 扫出的每个对象，还会把被整段引号包了一层的 JSON 再拆开一次。返回第一个通过校验的对象。
- 重试（`pm.ask`）：第 1 次用 `pm_model`，第 2 次同一模型并附上错误，第 3 次换 `pm_fallback_model`（config.json 的新键，没配就还用 `pm_model`）。每次失败都通过 `ask_pm()` 记一条 `pm:invalid` 事件，带原始回复原文（截前 8000 字）、模型和错误。`openclaw agent` 进程本身失败也算一次尝试。
- 三次都失败时抛 `PMFailed`，`submit()` 接住后调 `fail_card()`，开一张 failure 卡。卡上写明「案头自身出错，不是任务失败」，附原始回复开头 120 字，带一个「重试」选项（`pm_retry` 记下要重跑的 job 和参数）。卡片保持待处理，状态是 `awaiting`，不会是 `closed`。其他未预料的异常也走同一张卡，文案是「案头自身出错，不是任务失败，工作没有丢」。
- 面板侧：failure 卡不再算「待结案」（`readyToClose` 排除 failure），结案按钮不再高亮，重试按钮在前。
- 顺带改的两处（追加条款第 4 条，技术故障不在可结案的情形里）：`sweep` 发现 OpenCode 会话不存在、`maybe_register` 发现派发目录不存在，原来都会直接置 closed，现在只开 failure 卡。后者的旧测试 `test_missing_dispatch_directory_fails_visibly` 断言因此从 `closed` 改成 `awaiting`。
- 现在只剩三条路径会结案：PM 回 close（`act`）、人点结案（`close`）、limit 后收口。都经过 `finish()`。

### 08:17 那张卡到底怎么被结案的（核对事件表和日志）

「授权分级落地：六类三档」（ses_e0be3f7b…）的经过：

- 08:16:48，人回答卡 201，`act` 唤醒 PM。
- 08:17:11，PM 两次回复都没有 JSON，抛 `PMError`（daemon.log 第 182 行），`fail_card` 弹出卡 203。卡上没有任何选项。
- 面板对「没有选项的待处理卡」显示「待结案」，并高亮结案按钮。
- 08:17:23，卡 203 的 resolution 是 `{"action": "close"}`，也就是 12 秒后有人点了结案。
- 08:17:26，结案。

所以不是代码在故障时直接置 closed，而是故障卡被界面当成「可以结案」，人顺手点了。早上 07:58 那次（目录未展开，卡 196 → close）是同一个模式。这次修在两层：故障卡改由重试兜底、不再显示「待结案」；PM 回复更难被判为失败。

## 4. 卡片界面

- 页眉显示「第 N 轮 / 共 3 轮」（英文 Round N of 3）。卡片开着时，N 是回答它将要花掉的那一轮（已用 + 1），所以点之前就知道预算。limit 卡和 failure 卡显示已用轮数。
- limit 卡写「已满 3 轮，请接管。」，状态徽章是「请接管」，隐藏回复框，只剩结案和接管，接管按钮高亮。limit 状态计入菜单栏的待处理数。
- 改动在 `Models.swift`（`Phase.limit`、`readyToClose`）、`Strings.swift`、`CardView.swift`（`meta`、回复框）、`Store.swift`（待处理计数）。`swift build -c release` 通过。没有跑 `app/build.sh`，没有替换 `app/build/Docket.app`。

## 5. 提示词改了哪几条（`docket/pm.py`）

- `rules()` 第 5 条改成硬规则，三个子项：(a) 每次让人参与都耗一轮，包括权限询问，PM 自己查、自己跑的不算；(b) 出卡前先问自己能不能定，能定的自己定，要问就一次问全，别把一个问题拆成几轮；(c) 预算用尽时案头会拒绝所有卡片，必须收口，把没做完的写成交接说明，不要再问。
- 新增第 6 条，讲与 coding agent 的协作方式：续派的指令要写成完整任务书，验收标准写在前面，尽量一次写全，减少回头找人。
- 第 9 条（最终只回一个 JSON）补了「前后不要有散文，不要代码块」。原来重复的第 8 条（语言）和编号重复的第 7 条已合并整理，现在是 1 到 9。
- `budget_line()` 替换 `decision_wake` 和 `mention_wake` 里原来写死的 `Rounds used: n/3`。第 3 轮时明确告诉 PM「这是最后一轮，卡片会被拒，要么续派最后一条指令，要么结案并填 remaining」。
- 新增 `wrap_up_wake()`（limit 后的收口）；close action 加了可选的 `remaining` 字段，并做了校验。

## 6. 历史数据脚本

```
/usr/bin/python3 -I scripts/recount_rounds.py            # 只打印差异，只读打开（mode=ro）
/usr/bin/python3 -I scripts/recount_rounds.py --apply    # 写库，每行另记一条 rounds:recounted 事件
/usr/bin/python3 -I scripts/recount_rounds.py --db PATH
```

口径：人在面板上解决的卡（有选择或有回复，权限卡也算，重放过的只算一次），加上裁判归入的聊天消息（`attributed` 且 belongs ≥ move_threshold，和 via chat 解决的卡去重）。结案、接管、过期、被取代、重试、mention 的续派确认都不算。新代码写过 `round` 事件的行，直接数 `round` 事件。

在真实库上只跑到打印，没有加 `--apply`，没有写库。结果有 7 条不一致：

| 交办 | 状态 | 库里 | 重算 |
|---|---|---|---|
| 开源监控证据供给 v3.1 | closed | 1 | 3 |
| 对外发帖口径审计与沉淀 | taken_over | 3 | 8 |
| 案头菜单栏加设置项 | closed | 3 | 13（10 张是权限卡） |
| 产品命名讨论与撞名核查 | closed | 2 | 3 |
| 授权弹窗不再为已处理的请求弹出 | closed | 2 | 3 |
| 授权分级：六类与宿主配置合成 | closed | 0 | 2 |
| 授权分级落地：六类三档 | closed | 0 | 1 |

「授权分级：六类与宿主配置合成」（ses_e0e6ee8f…）实际是 2 轮：卡 193（追问「我什么时候有一律放行的兜底规则」）和卡 194（讲规则集叠加的设计）。你记得的第三次交互，是 07:59 在目录未展开的 failure 卡 196 上点了结案。按任务书的口径，结案是收尾，不算一轮。

偏差最大的是「案头菜单栏加设置项」，库里 3、实际 13，主要是一连串权限卡。这说明旧口径下权限打扰完全没进预算。按新规则，这类交办会在第 3 次权限询问时就进 limit。这一点值得留意：权限卡多的任务会很快耗尽预算。7 条全是已经结束的交办，还在跑的没有差异，要不要 `--apply` 由你定。

## 7. 动过的函数

- `docket/ledger.py`：`STATES`（加 `limit`）、新增 `LIMIT_EXITS`、`set_state`、新增 `spend_round`、`snapshot`（limit 排在最前，`pm_retry`/`wrap_up`/`round_spent` 不外露），以及 schema 里 rounds 的注释。
- `docket/pm.py`：模块说明、`configure`（加 fallback）、`rules`、`action_format`、新增 `budget_line`、`event_block`、`decision_wake`、`mention_wake`、新增 `wrap_up_wake`、`run`（加 model 参数）、新增 `json_objects`、`extract_json`、`validate_action`（remaining）、新增 `validate_wrap_up`、新增 `PMFailed`、新增 `pick`、`ask`。
- `docket/daemon.py`：`__init__`（configure 移到后面并传 fallback）、`submit`、新增 `ask_pm`、`show_card`、新增 `RETRYABLE`、`fail_card`、新增 `enter_limit`（取代已删除的 `limit_card`）、`sweep`、`maybe_register`、`write_card`、新增 `wrap_up`、`decide`、新增 `check_limit`、`act`、新增 `relayed_count`、`relay`、`close`、新增 `conclusion_text`、`finish`、`attribute`。
- `docket/i18n.py`：`limit` 改文案；新增 `err_limit`、`docket_fault`、`pm_failed`、`pm_retry_question`、`pm_retry`、`remaining`（中英文都有）。
- `docket/config.py`：新增 `pm_fallback_model` 键与说明。
- `app/Sources/Docket/`：`Models.swift`、`Strings.swift`、`CardView.swift`、`Store.swift`（见第 4 节）。
- `tests/test_flows.py`：`setUp` 的假 PM 接受 `record`；新增 `REAL_ASK`、`mention`、`rounds`、`real_pm` 三个辅助；`test_mention_relay_needs_confirmation` 改为走真实的 `attribute` 路径；`test_round_limit` 换成新规则下的 `test_three_answers_spend_three_rounds_then_the_limit`；`test_missing_dispatch_directory_fails_visibly` 的状态断言改了；新增 9 条，见第 8 节。
- 新文件 `scripts/recount_rounds.py`。`README.md` 架构图一行、`app/SPEC.md` 状态说明各改一行。

开工时工作树里没有任务书说的另一张卡（派发目录展开）的未提交改动，它已经是提交 bb39a3f。这次没有动它，也没有回滚任何东西。

## 8. 测试

```
python3 -I -m unittest discover -s tests -p 'test_*.py'
Ran 27 tests in 0.188s
OK
```

原有 18 条，加上新增 9 条：

- `test_three_answers_spend_three_rounds_then_the_limit`：连续解决三张卡，rounds 依次是 1、2、3；第三次之后进 limit，PM 的第 4 张卡被拒，第 4 次作答返回 409。
- `test_show_card_is_refused_at_the_limit`：rounds 为 3 时 `show_card` 返回 None，留下 `card:blocked`；权限卡同样被拒；`set_state("running")` 留在 limit。
- `test_an_answer_that_relays_is_one_round`：一张卡解决并触发续派，只加一。
- `test_a_chat_mention_is_one_round`：mention 加一，确认续派不再加；mention 顺带解决开着的卡，也只加一。
- `test_replay_spends_nothing`：replay 不加。
- `test_answering_a_permission_card_is_one_round`：权限卡解决加一，双击不加。
- `test_the_third_answer_is_carried_out_then_wrapped_up`：第 3 轮的续派照发；跑完后 PM 写交接说明；结案结论里有 remaining。
- `test_json_is_found_inside_prose`：从前后散文、代码块、整段被引号包住的 JSON、字符串里含 `}` 的对象中都能抽出 JSON；会跳过不合格的对象，取第一个合格的。
- `test_a_reply_without_json_is_retried_not_closed`：第 1 次是散文，自动重试；原文记进 `pm:invalid`。
- `test_three_bad_replies_become_a_failure_card_not_a_closed_handoff`：三次都失败时，模型依次是主、主、fallback；开出 failure 卡，写明不是任务失败，附原始回复，状态是 awaiting 不是 closed；点重试不计轮，重新出卡。

另外，`py_compile` 通过，`swift build -c release` 通过。

## 9. 真机演示

用的是真守护进程的代码和 HTTP 接口（`daemon.serve`，POST `/api/cards/<id>/decide`），账本放在临时 `DOCKET_HOME`。PM 和 OpenCode 换成了脚本替身：每次回答后 PM 都再出一张卡，这是对预算最不利的情况。没有启动 openclaw/opencode 进程，也没有碰正在跑的守护进程和真实库。

```
before answer 1: state=awaiting rounds=0 card=question 'PM question 1'
  answer 1: (202, {'ok': True})
before answer 2: state=awaiting rounds=1 card=question 'PM question 2'
  answer 2: (202, {'ok': True})
before answer 3: state=awaiting rounds=2 card=question 'PM question 3'
  answer 3: (202, {'ok': True})
after 3 answers: state=limit rounds=3 card=limit 'All 3 rounds used. Please take it over.'
4th card from the PM: None
4th answer via HTTP: (409, {'error': 'All rounds are used: take it over or close it.'})

== delegations
ses_demo|limit|3
== events
3|round|{"rounds": 1, "card": 1}
6|round|{"rounds": 2, "card": 2}
9|round|{"rounds": 3, "card": 3}
11|card:blocked|{"kind": "question", "progress": "PM question 4"}
12|state:limit|
14|card:blocked|{"kind": "question", "progress": "one more question?"}
```

（第 1 版替身写错了，act 收到的是卡片而不是 action。结果 PM 连续出错 4 次，每次都是 failure 卡加重试，rounds 始终停在 1，交办一直没有结案，等于顺带验证了故障路径。这条记录不作为验收依据。）

## 10. 要重启什么，对已在跑的卡有什么影响

- 重启 docket 守护进程（launchd `io.github.peberwang.docket`），由 OpenClaw 做。没有数据库迁移，`limit` 只是一个新的 state 字符串。
- 重新构建并重启 Docket.app（`app/build.sh`）。如果只重启守护进程、不换 app，旧 app 会把 `limit` 显示成「未知」，也不计入待处理数，failure 卡仍显示「待结案」。建议两者一起换。
- 可选：在 config.json 里加 `"pm_fallback_model"`。当前 `pm_model` 是 `deepseek/deepseek-flash`，第三次可以换一个更守规矩的模型。没配的话，第三次还用 flash。
- 对在跑的交办（只读查询，现在有两条活着）：
  - 「轮次预算收紧与案头故障不结案」，running，rounds 2；「Fork 插件：选区时序与面板空白」，awaiting，rounds 0，一张卡开着。
  - 重启后，它们库里的 rounds 原样接着用，不会补算。之后每回答一张卡都加一，权限卡也算，所以会比以前更快到 3。前一条再参与一次就到 3。
  - 旧代码出的 mention 确认卡没有 `round_spent`，回答时会计一轮，只影响当下这一张。
  - 旧代码出的 failure 卡没有重试按钮。回复它算一轮，也可以直接结案。
  - 旧行如果库里已经 rounds ≥ 3（旧口径下是续派满 3 次），下一张卡会被拒并进入 limit，和旧规则「3 轮后建议接管」一致。
- 没有提交，没有推送，没有改版本号，没有重启任何进程。

---

# 交接：派发目录里的变量展开（对应 BRIEF_path_expansion.md）

## 改了什么

公共函数都在 `docket/dispatches.py`：

- `expand_dir(raw)`：把 `~`、`$HOME`、`${HOME}`（以及环境里其他变量）按 shell 的读法展开，再 `resolve()`。`$HOME` 走 `Path.home()`，所以守护进程环境里没有 HOME 也能展开。`$HOMEDIR` 这类变量不会被误当成 `$HOME`。
- `existing_dir(raw)`：展开后检查目录是否存在，不存在就抛 `MissingDirectory(raw, expanded)`，原值和展开后的值都带着。
- `parse()`：`cd <path>` 的路径改用 `expand_dir`，原值另存在 `directory_raw` 里。
- `read_brief()`：任务书路径和目录也用 `expand_dir` 处理（`-f "$HOME/…/BRIEF.md"` 也能读到）。

调用方：

- `docket/registry.py` `find_dispatch`：去掉了原来自己做的 `expanduser`，把 `directory_raw` 一并传出去。
- `docket/daemon.py`
  - `maybe_register`：入库前先调 `existing_dir`。目录不存在时，`directory` 存 NULL（不存进去任何跑不起来的路径），事件表记一条 `directory:missing {raw, expanded}`，日志打 warning，弹出 failure 卡片「派发目录不存在：<原值> → <展开后>」，然后这件交办结案。没有派发目录、回落到 OpenCode 会话 `location.directory` 时，也走同一套校验。
  - `dispatch_cli`（`POST /api/dispatch`）：入库前校验，不存在就返回 409，错误信息是同一句人话。
  - `project_dir`：改用 `expand_dir`。行里没有 directory 时会回落到引擎参数 `--dir/--cwd/-C`，那里写的 `$HOME` 也一起展开；库里已经存着字面量 `$HOME/…` 的旧行，现在也能正确识别成真实目录。
  - `submit`：遇到 `MissingDirectory` 时弹的是人话卡片，不再是「案头处理出错：project directory not found…」。
- `docket/engines.py`：`OpenCode.resume`、`Cli.launch` 改用 `existing_dir`，`Cli.takeover` 改用 `expand_dir`。
- `docket/i18n.py`：新增 `missing_dir`（中英文各一条）。

## 测试

```
python3 -I -m unittest discover -s tests -p 'test_*.py'
```

（本机没有装 pytest，仓库一直用的是 unittest。）一共 18 条，新增 8 条：

- `Directories`：`$HOME/x`、`"$HOME/x"`、`'${HOME}/x'` 都展开成真实路径；`~/x` 照常展开；目录不存在时异常里同时带原值和展开值；`$HOMEDIR` 不会被误认成 `$HOME`；`$HOME` 形式的任务书路径能读到。
- `Flows`：从一条 `cd "$HOME/…"` 派发命令走 `maybe_register`，入库的是真实路径；目录不存在时卡片显示失败、交办结案，卡片文字和事件里都有原值和展开值；库里存着字面量 `$HOME` 的旧行，`project_dir` 也能解析出来。

## 需要重启什么

改动在重启 docket 守护进程（launchd 那一个）之后才生效。没有数据库迁移，Docket.app 不用重新构建。

## 对已在跑的交办有什么影响

- 守护进程重启前：没有任何影响，进程里跑的还是旧代码。
- 重启后：已经入库的行不会被改写。存着字面量 `$HOME/…` 的旧行，续派、接管、授权判定时都会在使用的那一刻展开，所以以后不会再报 `project directory not found`。2026-11-19 那条已经结案的不会自己恢复，要的话得重新派发。
- 已经存着真实路径的行，行为不变。


# 交接：状态栏菜单加「权限分级设置…」（对应 BRIEF_desk_menu_permission.md）

## 菜单项

状态栏图标右键（或按住 Control 点）弹出的菜单，现在依次是：展开面板 / 隐藏右侧条带 / 权限分级设置… / 设置… / 分隔线 / 退出。已有三项的顺序、快捷键和行为没动。新项英文是 `Permission Tiers…`，走 `Strings` 的 `.permissionSettings`。

点它会起一个 permission-notify 的 `permission-settings` 子进程，立刻返回，不等它退出，窗口由那个程序自己负责置前。窗口还开着时再点，只把它提到前面，不会再开一个去同写 settings.json。

## 路径解析顺序

按顺序取第一个可执行的：

1. `~/Library/Application Support/opencode/permission-notify/bin/permission-settings`（安装副本）
2. `~/Desktop/Code工作区/permission-notify/bin/permission-settings`（仓库）

都不可执行时弹一个原生警告框：「未找到 permission-settings，路径是：」，下面逐条列出试过的完整路径（已展开）。能找到但启动失败时，警告框写路径和错误原因。

## 可覆盖设置项

UserDefaults 键 `permissionSettingsPaths`（域 `io.github.peberwang.docket`）。可以是字符串数组，也可以是单个字符串，写了就完全替代上面两条缺省路径。支持 `~`、`$HOME`、`${HOME}`。每次点菜单时才读取，改完不用重启案头。

```
defaults write io.github.peberwang.docket permissionSettingsPaths -array \
  "~/Library/Application Support/opencode/permission-notify/bin/permission-settings" \
  "~/新位置/permission-notify/bin/permission-settings"
defaults delete io.github.peberwang.docket permissionSettingsPaths   # 回到缺省
```

代码在 `app/Sources/Docket/PermissionSettingsLauncher.swift`，菜单接线在 `StatusItemController.swift`。

## 要注意：桌面文件夹授权

现在安装副本目录里还没有 `permission-settings`，实际走的是第 2 条，也就是 `~/Desktop` 底下的路径。macOS 会对案头弹「"Docket" would like to access files in your Desktop folder」。点允许之前，子进程卡在加载自身那一步，不报错也不出窗口；点了允许就正常。案头是 ad-hoc 签名，每次重编重装签名都会变，可能要重新允许。长期办法是 permission-notify 安装时把 `permission-settings` 也拷进安装副本的 `bin/`，那样第 1 条直接命中，不再碰桌面。这一步属于 permission-notify 仓库，本卡没动。

## 验收（2026-11-19，跑的是 `app/build/Docket.app`，和已安装的案头同时开着）

1. 菜单六项顺序正确，AX 读到：展开面板、隐藏右侧条带、权限分级设置…、设置…、分隔线、退出。
2. 点新项后弹出「OpenCode 授权分级」窗口，六行三档。把类 6 改成案头 pm，settings.json 写成 `{"permissionClasses":{"6":"pm"}}`，`--print` 显示 source=user。原来没有 settings.json，测完把文件挪进废纸篓，`--print` 回到 source=host。
3. 把仓库里的可执行改名（安装副本本来就没有），点新项会弹警告框，列出两条路径；改回原名后又能正常开窗。
4. 展开/收起、隐藏/恢复右侧条带、退出都正常。`permissionSettingsPaths` 写数组时，第一条不存在会回退到第二条；写单个字符串指向不存在的路径时，警告框列出的是展开后的路径。测完已 `defaults delete`。
5. 深色和浅色主题下都截图看过，用的是系统菜单的原生配色，字都清楚。
6. `bash app/build.sh` 编译通过。只有两条 `ld: warning: search path … CommandLineTools … not found`，在干净的 HEAD 上编译也一样有，是工具链问题，不是这次加的。

## 安装

要 OpenClaw 按惯例把 `app/build/Docket.app` 装到 `~/Applications/Docket.app` 并重启。版本号没动。

# 交接：卡片队列卡死，修状态机（对应 BRIEF_card_queue_stuck.md）

代码随 `1d6badb` 入库。

## 改了哪几处

1. `Docket.show_card()`（`docket/daemon.py`）：卡片写入后一律置 `awaiting`。入队时仍记 `card:queued`；如果当时只有排队卡、没有开卡（例如续派时上一张刚答完），就把最早的排队卡提为开卡。`pending` 只剩一个意思：PM 作业正在写卡，作业一结束必然离开。
2. `ledger.push_card()`：写入 question 时，把同一交办所有未解决的 question 标 `resolved`，resolution 为 `{"action":"superseded"}`，并记 `card:superseded` 事件。被顶掉的那张如果正是开卡，新卡直接成为开卡，不进队列。report、failure、limit 不参与。
   - 与任务书的一处出入：**权限卡（带 `pending_permission` 的 question）既不会被顶掉，新的权限卡也不会顶掉别人**。OpenCode 那边还在等它的回答，顶掉就等于无声拒绝；它在 run 结束时由 `on_run_end()` 标 `expired`，不会成为孤儿。
3. `Docket.relay()`：续派后如果没有开卡但队列里有卡，提升下一张并置 `awaiting`。原来这里会留下一张看不见的排队卡，要等下一张卡写入才露面。
4. `Docket.submit()` / `busy()`：按交办计数已提交、未完成的 PM 作业（含还在排队等线程的），供 sweep 判断「当前没有在跑的 PM 作业」。
5. `Docket.sweep_orphans()`：sweep 每分钟先跑这一步，见下。

## 状态机现在怎么流转

- OpenCode 跑完 / 提问 → `on_run_end()` 置 `pending` → PM 写卡 → `show_card()` 置 `awaiting`（不论入队与否）。
- 人答卡 → `decide()` 置 `pending` → PM 动作：
  - 续派 → `running`；若有排队卡，提升并置 `awaiting`；
  - 出卡 → `show_card()` → `awaiting`；
  - 结案 → `closed`。
- 权限卡答完 → `advance()`：有排队卡 → `awaiting`，否则 → `running`。
- `limit` 只能走向 `closed`、`taken_over`（不变）。

## sweep 的阈值

`Docket.ORPHAN_AFTER = 2 小时`。孤儿的判定：卡片未解决、是开卡（`queued = 0`）、创建超过 2 小时、不是 limit/closed 卡，交办状态是 `running` 或 `pending`，且该交办没有在跑或排队的 PM 作业。命中后标 `{"action":"superseded","note":"orphan"}`，记 `sweep:orphan` 事件；还有开卡就置 `awaiting`，否则 `advance()` 提升下一张排队卡（没有就回 `running`）。

与任务书字面「任何卡片未解决超过两小时」的出入：**`awaiting` 状态下的卡不算孤儿**。那张卡就在面板上，人只是还没答（比如放过夜），自动撤掉会让人丢掉要做的决定。卡死的根源是「状态不是 awaiting，面板看不到卡」，判定只针对这种情形。排队卡（`queued = 1`）本身也不会被撤，只会被提升。

## 测试

`tests/test_flows.py` 新增 7 条，全套 `python3 -I -m unittest discover -s tests -p 'test_*.py'` 通过：

- `test_a_queued_card_leaves_the_handoff_awaiting_not_pending`：已有开卡、状态 pending 时写 report，状态为 awaiting，队列 1。
- `test_a_new_question_supersedes_the_old_one`：旧 question 变 superseded，新卡是开卡，队列 0。
- `test_a_question_taking_the_place_of_the_shown_one_jumps_the_queue`：顶掉开卡的新 question 直接成为开卡，排队的 report 继续排着。
- `test_reports_failures_and_permission_requests_are_never_superseded`。
- `test_sweep_supersedes_an_orphan_and_lifts_the_queue`：3 小时前的开卡在 pending 下被撤，排队卡提升、状态 awaiting，提升的卡能作答并结案。
- `test_sweep_leaves_cards_that_are_on_show_or_being_written`：awaiting 下不撤；有 PM 作业时不撤；作业结束后撤，回 running。
- `test_answering_shows_the_next_card_in_order`：两张卡依次出现，running/awaiting 正确切换，run 结束后 PM 写卡回到 awaiting。

## 手工核对 ses_e0b0f42d1ffe4KTGB9NUM7Jo8n

尚未做：真实账本在 `~/Library/Application Support/docket/docket.sqlite`，在工作区之外，要铭培同意后才能访问。守护进程按新代码重启后，sweep 会在一分钟内按上面的规则自动处理：217 是 12:00 的开卡、状态 pending、早已超过 2 小时，会被标 superseded，223 被提升，状态置 awaiting。

## 要重启什么

守护进程（由 OpenClaw 负责）。app 不用动。

# 交接：交还，让 taken_over 的卡回到案头（对应 BRIEF_docket_handback.md）

## 新增端点

`POST /api/delegations/<id>/handback` → `202 {"ok": true, "state": "pending" | "awaiting"}`，形状照 `/takeover`、`/close`。

- 只接受 `taken_over`。其他状态（含 closed、从没接管过、已经交还过）返回 409，文案「只有已接管的交办才能交还，这件现在不是接管状态」；不存在的交办同样 409「没有这件交办」（与其他端点一致）。
- 状态转换在 `ledger.hand_back()` 里一个事务完成（`begin immediate`，与 `spend_round` 同法），两次并发点击只有一次生效：
  - 接管时留下的排队卡（接管只撤掉开卡）提为开卡 → `awaiting`；
  - 否则 → `pending`，紧接着唤醒 PM。
- 记 `handback` 事件（带累计次数）和对应的 `state:*` 事件，事件表自带时间。
- 新列 `delegations.handbacks`（旧账本启动时自动 `alter table` 补上），`GET /api/state` 每件交办多一个 `handbacks` 字段。

## 交还后不进轮次重置（决定）

- `rounds` 原样保留，交还本身也不花轮次。理由：交还若清零，接管→交还一来一回就能无限续命，轮次预算形同虚设。
- 已满 3 轮的卡也能交还：PM 收到的消息里预算行写明「最后一轮，只能续派或结案」；它若还想出卡会被 `show_card` 拒掉，limit 卡回来、状态回 `limit`。它若续派，那一轮跑完 `write_card` 照旧走 `wrap_up`。
- 续派闸门 `relayed_count()` 不算 `via: handback` 的那次续派（和 fallback 重试同理），否则交还后 PM 推一块，就会占掉用户后面一次答卡的续派名额。每次交还只免这一次；之后的续派仍要靠用户答卡。

## 给 PM 的消息

`pm.handback_wake()`：一句「用户接管了一阵，现在交还给你，交还不花轮次」，一句「按你的节奏推进：先看现状（用户可能改过东西），把下一块续派给 OpenCode，长任务切成能验收的小块，拿不准才出卡，做完就结案」，加预算行、OpenCode 最后一条回复（用户自己在会话里跑过，PM 需要知道现在停在哪）、语言行和动作格式。没有计划，计划是 PM 的产出。

走现有 `act()`，因此失败时照常出带「重试」的故障卡，重试参数里带着 `via=handback`。

## 与任务书的出入

1. **daemon 里六处 `state in ('closed','taken_over')` 没改成只排除 closed。** 这些检查看的是当前状态，不是历史：交还后状态是 pending/running/awaiting，六处本来就放行，单测里交还后的 run 结束、答卡、limit、sweep 都按正常卡处理。反过来若改成只排除 closed，仍在接管中的卡就会被 `on_run_end`、权限请求重新出卡，和任务书自己要的「taken_over 仍然不接新请示」相反。
2. **交还后先是 `pending`，不是 `running`。** ledger 里 `pending` 的定义就是「PM 正在写」，而 `reconcile()` 靠它在守护进程重启后把中断的 PM 作业补回来；直接置 running 的话，重启时卡会停在 running、OpenCode 会话已空闲，sweep 也不会碰它（空闲时间早于 updated_at）。PM 续派后才是 `running`，出卡则 `awaiting`，与其他路径一致。
3. **面板快照**原来把 24 小时前接管的卡藏掉，交还就无从点起；现在接管的卡在面板上保留 7 天（`snapshot(taken_over_days=7)`），更早的不再堆在面板上，但 `scripts/handback.py` 对它们照样有效。

## 顺手修的两处

- `Docket.relay()`：续派后只要还有开卡（或能提升的排队卡）就置 `awaiting`。原来只在「没有开卡但有排队卡」时这样做，若开卡还在（交还时留下的那张），状态会落到 running，面板看不到那张卡，两小时后被 sweep 当孤儿撤掉。
- `Docket.act()`：PM 想完之后若交办已被再次接管或已结案，丢弃它的动作并记 `pm:dropped`。交还后用户立刻又接管时，PM 的续派不会再发进用户正在用的会话、也不会把状态从 taken_over 改掉。

## 界面

- `CardView`：`phase == .takenOver` 时动作行显示「在 OpenCode 打开 / 在终端接管」与「交还」两个按钮（原来是空的），两个方向都能走；「交还」悬停提示「交还给 PM 继续推进，已用的轮次不清零」。
- `Store.handback()`：请求成功后立刻把本地这张卡的状态改成返回的 `pending`/`awaiting`，不等下一次刷新；若别处已交还导致 409，按已处理收尾，不报错。
- 文案进 `Strings.swift`：`handBack`（Hand back / 交还）、`handBackHelp`。`Models.Delegation` 多一个可选的 `handbacks`。
- `app/dev-daemon.py` 也加了 `/handback`，方便不连真守护进程看界面。

## 命令行

```
/usr/bin/python3 -I scripts/handback.py <delegation-id>
```

从 `$DOCKET_HOME/daemon.json`（默认 `~/Library/Application Support/docket/`）读地址和 token，调上面的端点。成功打印一行，如 `handed back ses_…: pending, the PM is picking it up.`；被拒或连不上时在 stderr 打一句可读的话（`handback: Docket refused ses_…: <原因>` / `the Docket service is not running (no daemon.json)`），退出码 1，参数不对退出码 2，不打栈。供 OpenClaw 主会话在用户说「交还给你」时直接调。

## 测试

`tests/test_flows.py` 新增 7 条，`/usr/bin/python3 -I -m unittest discover -s tests` 65 条全过：

- `test_a_taken_over_handoff_can_be_handed_back`：接管中 run 结束不出卡；交还 → pending → PM 续派 → running；rounds 不变、handbacks=1、续派不占闸门、有 handback 事件；下一次 run 结束重新出卡。
- `test_a_closed_handoff_cannot_be_handed_back`：closed（以及对 closed 点接管后）交还被拒，状态与计数不变；不存在的交办被拒。
- `test_handing_back_twice_is_refused_and_counted_once`：没接管过被拒；第二次交还明确报错、计数仍为 1；再接管再交还计为 2。
- `test_a_card_left_waiting_goes_up_on_handback`：接管时排着的卡在交还后成为开卡，状态 awaiting，PM 续派后仍是 awaiting。
- `test_after_handback_the_limit_and_the_sweep_still_hold`：满轮后接管再交还，PM 出卡被拒、回到 limit、rounds 仍为 3、limit 卡不能作答；交还后的孤儿卡照常被 sweep 撤掉。
- `test_a_pm_reply_after_another_takeover_is_dropped`：PM 思考期间又被接管，续派被丢弃，状态保持 taken_over。

手工验收：`/usr/bin/python3 -I tests/e2e_handback.py`。临时 DOCKET_HOME 里起真守护进程的 HTTP 服务（PM 与编程代理用桩），走一遍「面板上有卡 → POST takeover → 跑 `scripts/handback.py` → 卡回到队列、PM 续派、rounds 0 handbacks 1 → 再交还一次得到一行报错 → 下一次 run 结束重新出卡」，跑完删掉沙箱。不碰 OpenClaw、OpenCode 和已安装的案头。

`swift build -c release` 通过；没有替换 `app/build/Docket.app`。

## 要重启什么

守护进程（新端点、新列迁移都在启动时生效）和案头 app（新按钮）。重启前已经 taken_over 的卡：7 天内的会重新出现在面板上，带「交还」按钮；更早的用脚本交还。
