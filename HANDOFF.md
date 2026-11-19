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
