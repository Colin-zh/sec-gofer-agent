# 使用终端 CLI

在项目根目录运行 `uv run python -m sec`，或在安装后的环境中运行 `sec`。可用参数：

```sh
uv run python -m sec --help
uv run python -m sec --mode plan
uv run python -m sec --session <session-id>
uv run python -m sec --extension <extension-name>
```

`--extension` 可重复传入；Extension 必须已安装，并且只会在显式启用时加载。详见[创建第一个 Extension](11-create-your-first-extension.md)。

## 启动后会看到什么

CLI 首先显示 Sec 版本和运行信息：

```text
Sec v0.1.0 — Your gofer. A second you, in a second.

⌂  Working directory  /path/to/project
◈  Model              your-model
#  Session            <session-id>
◉  Tool mode           normal
✦  Resources           1 Skills · 1 Prompts

Ctrl+C cancel Run · Ctrl+D exit · Tab complete · /help commands
Press Ctrl+O for full startup help and loaded resources.
```

实际启动页还会在有启用项时显示 Extensions；发现无效或重名资源时显示 `Resource issues` 计数。按 `Ctrl+O` 或运行 `/help full` 可查看已加载资源、可用命令及资源问题。

界面底部是可编辑输入框。终端支持多行编辑时，按 Enter 发送，按 Alt+Enter 插入换行；输入以 `/` 开头时会显示匹配命令，按 Tab 补全。每次新请求会显示 Run 开始状态，模型回答逐段输出；工具开始/结束、审批和 Run 完成/失败会显示为状态行。终端不支持原始按键输入时会使用逐行输入回退界面。

## Run 期间的交互

一次只运行一个活动 Run。运行期间：

- 普通输入作为 follow-up，在当前 turn 后继续执行。
- `/steer <message>` 或 Alt+S 将指令加入当前 turn。
- `/followup <message>` 或 Alt+F 排入下一 turn。
- `/stop` 或 Ctrl+C 请求取消当前 Run。
- `/exit` 或 Ctrl+D 退出；若 Run 仍活动，会先请求取消再退出。

取消是协作式的，不会撤销已发生的工具副作用。

## 常用命令与按键

- Run 控制：`/steer <message>`、`/followup <message>`、`/stop`、`/exit`。
- Session：`/new`、`/resume`、`/name`、`/session`、`/tree`、`/fork`、`/clone`。
- 文件与资源：`/import <path>`、`/export [path]`、`/copy`、`/reload`、`/skill:<name> [request]`、`/prompt:<name> [request]`。
- `/mode [plan|normal|auto]` 查看或切换权限模式；`/trust [on|off]` 是本次聊天中的快捷切换，不写入持久配置。
- `/help` 查看命令；`Ctrl+O` 或 `/help full` 展开完整帮助；Tab 补全命令。
- `Ctrl+C` 取消当前 Run；`Ctrl+D` 退出；Alt+Enter 输入多行内容。

退出时 CLI 会显示当前 Session ID。更多 Session 操作见[管理 Sessions](04-manage-sessions.md)。

## 工具审批界面

在 `normal` 模式下，每次工具调用都需要确认。CLI 会显示工具名称、参数和策略原因，并提示 `[y/N]`：

```text
Allow tool write?
Policy: Normal mode requires approval for every tool call.
{
  "path": "notes.txt",
  "content": "..."
}
[y/N]
```

输入 `y` 或 `yes` 批准，输入 `n` 或 `no` 拒绝；其他普通输入会提示重新输入。默认选择是拒绝。`auto` 也不会自动批准 Bash。权限模式和安全边界见[工具、权限与审批](06-tools-permissions-and-approvals.md)。

## 管道输入

CLI 也可接收标准输入，例如：

```sh
printf '总结这个项目的入口文件\n' | uv run python -m sec
```

非交互模式没有工具审批界面，因此需要 `ask` 的工具调用会默认拒绝。不要假设管道模式会自动批准工具。

`/reload` 只重新发现 Skills 和 Prompts；当前 Client 的 Extensions 不支持安全热重载。
