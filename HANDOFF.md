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
