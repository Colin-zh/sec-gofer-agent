<p align="center">
  <img src="https://raw.githubusercontent.com/Colin-zh/sec-gofer-agent/main/docs/images/icon.png" alt="Sec icon" width="96">
</p>

<h1 align="center">Sec</h1>

<p align="center"><strong>Your gofer. A second you, in a second.</strong></p>
<p align="center">你的数字孪生小二，随叫随到，迅速响应。</p>

<p align="center">
  <a href="https://pypi.org/project/sec-gofer-agent/"><img src="https://img.shields.io/pypi/v/sec-gofer-agent?color=blue" alt="PyPI version"></a>
  <a href="https://pypi.org/project/sec-gofer-agent/"><img src="https://img.shields.io/badge/Python-3.12%2B-blue" alt="Python 3.12 and newer"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
  <a href="https://github.com/Colin-zh/sec-gofer-agent/graphs/contributors"><img src="https://img.shields.io/github/contributors/Colin-zh/sec-gofer-agent?color=blue"></a>
  <a href="https://github.com/Colin-zh/sec-gofer-agent/stargazers"><img src="https://img.shields.io/github/stars/Colin-zh/sec-gofer-agent.svg?logo=github"></a>
  <a href="https://github.com/Colin-zh/sec-gofer-agent/network/members"><img src="https://img.shields.io/github/forks/Colin-zh/sec-gofer-agent.svg?color=blue&logo=github"></a>
  <a href="https://www.python.org/"><img src="https://upload.wikimedia.org/wikipedia/commons/c/c3/Python-logo-notext.svg" align="right" height="48" width="48" ></a>
</p>

Sec 是一个可编程的 AI Agent SDK，也提供终端聊天 CLI。你可以直接用 CLI 与模型协作，也可以通过 Python Client 将 Sec 集成到自己的应用，或用 Extension 添加工具、命令和运行时能力。

## 快速开始

要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv python install 3.12
uv sync
cp .env.example .env
```

编辑 `.env`，选择 OpenAI 或 Anthropic Provider，并填写模型名和 API key。也可以设置通用变量 `SEC_PROVIDER`、`SEC_MODEL`、`SEC_API_KEY`。不要提交包含真实凭据的 `.env`。

```sh
uv run python -m sec
```

CLI 支持交互式聊天、Session 管理、内置工作区工具和权限模式。第一次使用建议保持默认的 `normal` 模式；工具执行前会请求确认。详见[终端 CLI 教程](docs/tutorials/03-use-the-cli.md)和[权限与工具教程](docs/tutorials/06-tools-permissions-and-approvals.md)。

## 可选桌面应用

桌面应用位于 `sec/apps/desktop/`，与 CLI/SDK 共用主包和环境，Qt 仅作为
`desktop` 可选依赖。当前发行包名是 `sec-gofer-agent`，默认安装不包含 Qt：

```sh
pip install sec-gofer-agent
pip install 'sec-gofer-agent[desktop]'  # 需要桌面能力时安装
sec-desktop
```

仓库开发运行：

```sh
uv sync --extra desktop --no-editable
uv run --no-sync sec-desktop
# 或 uv run --no-sync python -m sec.apps.desktop
```

支持大屏专注/悬浮球切换、会话恢复、流式回答、工具审批及 CLI 斜杠命令。
Extension 安装到同一 `.venv` 后，可通过 `--extension <name>` 显式启用；
交互 CLI 也支持 `/reload` 查看已安装扩展，并用 `/reload enable <name...>` /
`/reload disable <name...>` 在当前 Client 生命周期内切换启用集合。
扩展代码更新仍需重启进程，且启停仅影响后续 Run。
详细说明见[桌面应用](sec/apps/desktop/README.md)。

## CLI 界面速览

交互式终端启动后会先显示当前运行环境摘要，再进入可编辑的 `you >` 输入框。下图为字段示意，实际路径、模型、Session ID 和资源数量取决于本机配置：

```text
╭ Sec v0.1.0 — Your gofer. A second you, in a second.
│
│ ⌂  Working directory  /path/to/project
│ ◈  Model              your-model
│ #  Session            <session-id>
│ ◉  Tool mode           normal
│ ✦  Resources           1 Skills · 1 Prompts
│
│ Ctrl+C cancel Run · Ctrl+D exit · Tab complete · /help commands
│ Press Ctrl+O for full startup help and loaded resources.
╰
you >
```

发出消息后，Sec 会显示流式回答和 Run/工具状态。工具需要审批时，会显示工具名称、参数和策略原因，等待输入 `y` 或 `n`；默认拒绝。交互终端支持多行编辑和命令补全：Enter 发送，Alt+Enter 换行，Run 运行中按 Alt+S steer 当前 Run，按 Alt+F 将当前输入排到 Run 之后，Tab 补全命令。Run 运行中直接发送普通文本也会排为 follow-up。

终端不支持原始按键输入时会退回逐行输入界面；非交互式管道输入没有审批提示界面，需确认的工具调用会拒绝。详见[CLI 界面与操作指南](docs/tutorials/03-use-the-cli.md)。

## Python Client 一分钟

普通脚本可从同步 `SecClient` 开始。Client 上下文中连续调用 `run()` 会沿用同一个 Session 历史：

```python
from sec.apis import SecClient

with SecClient("assistant", "遵循用户指令。") as client:
    first = client.run("解释当前项目的入口。")
    print(first.text)
    print("Session:", client.session.id)

    second = client.run("再说明它如何加载配置。")
    print(second.text)
```

需要 asyncio、流式事件、Session 恢复或精细 Run 控制时使用 `AsyncSecClient`。两类 Client 都应使用上下文管理器；失败通过异常传播，成功的 `RunResult` 提供文本、调用次数和用量信息。SDK Client 默认不附加 CLI 的内置文件/Bash 工具；通过 `tools=` 或 Extension 显式提供工具。完整示例见[同步 Client](docs/tutorials/07-build-a-sync-client.md)、[异步与流式 Client](docs/tutorials/08-build-an-async-streaming-client.md)和[Run 控制](docs/tutorials/09-control-runs-and-consume-events.md)。

## 选择使用方式

- **使用终端聊天**：从[安装与第一次对话](docs/tutorials/01-install-and-first-chat.md)开始。
- **集成 Python 应用**：查看[同步 Client](docs/tutorials/07-build-a-sync-client.md)或[异步与流式 Client](docs/tutorials/08-build-an-async-streaming-client.md)。
- **扩展 Sec**：跟随[创建第一个 Extension](docs/tutorials/11-create-your-first-extension.md)。
- **查询配置项**：查看[配置参数参考](docs/reference/configuration.md)。
- **浏览全部教程**：进入[教程首页](docs/tutorials/00-homepage.md)。

## 当前功能

- **CLI**：交互式与管道式对话、命令补全、Run 控制、权限模式、Session 分支和便携式导入/导出。
- **Python SDK**：同步和异步 Client、流式响应、工具调用、Run 事件与运行控制。
- **工具与策略**：类型化工具定义、参数校验、工作区内置文件工具、工具审批及可替换 Policy/Executor。
- **Skills 与 Prompts**：从 Markdown 文件加载可复用的操作说明和提示模板。
- **Extensions**：通过 Python entry point 显式启用，可注册工具、Provider、Policy、SessionStore、UI handler、CLI 命令和 Run hooks。
- **Session 与存储**：进程内或 SQLite SessionStore、会话恢复和分支操作；提供可实现的 ObjectStore 协议。
- **运行可靠性**：模型及安全声明工具的有限重试、可控的工具错误反馈、每轮调用预算、用量统计和可选上下文压缩。

## 安全与支持边界

- `plan`、`normal`、`auto` 模式对工具调用应用不同策略。`auto` 仅自动允许工作区范围内的内置文件工具；Bash 和其他高风险操作仍需审批。
- 内置 Bash 以当前用户权限在主机上运行，**不是安全沙箱**。不要执行不可信命令。
- CLI 的 Skills 和 Prompts 是提示文本，不执行 Markdown 文件中的代码；Extension 则是可执行 Python 包，只应启用可信扩展。
- 当前 Provider 支持 OpenAI 与 Anthropic 的文本和工具交互；多模态内容尚未适配。
- MCP 连接管理、Dashboard 和云厂商 ObjectStore 适配器尚未实现。`/share`、`/thinking` 和手动 `/compact` 也尚不可用。

## 开发与测试

```sh
uv sync
uv run python -m unittest discover -s tests -v
```

项目使用 `uv.lock` 锁定依赖。修改依赖后请一并提交 `pyproject.toml` 和 `uv.lock`。
