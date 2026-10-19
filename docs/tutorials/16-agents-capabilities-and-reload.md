# AgentCard、能力发现与会话内刷新

Sec 的工作区 Client 使用同一套核心资源目录。CLI 和 Desktop 只是核心 API
的交互入口，不分别实现智能体加载、MCP 调用或工具热更新。

## 工作区与兼容性

已有 `AsyncSecClient("name", "instructions")` 不自动加载磁盘资源。
显式传入 `workspace=Path(...)` 后才启用工作区资源管理：

```python
from pathlib import Path
from sec import AsyncSecClient

async with AsyncSecClient(
    "assistant",
    "Follow the user and use relevant capabilities.",
    workspace=Path.cwd(),
) as client:
    print(client.agent_cards)
    await client.select_agent("researcher")
    result = await client.run("比较当前项目的两个实现方案").result()
    print(result.text)
    report = await client.reload_resources()
    print(report.generation, report.added, report.updated, report.removed)
```

同步 `SecClient` 提供同名阻塞方法。SDK 默认仍然没有内置文件/Bash 工具，
工具与 Policy 必须由宿主显式注入。启用工作区资源不会隐式提升权限。
完全由 Card 管理身份时也可直接使用
`AsyncSecClient(workspace=Path.cwd(), agent_id="researcher")`；
原有 `name` 和 `instructions` 位置参数继续兼容。

## AgentCard

用户级目录为 `~/.sec/agents/`，工作区级目录为 `.sec/agents/`。
每个智能体使用 `<id>/card.toml`，可引用同目录下的说明文件：

```toml
schema_version = 1
id = "researcher"
name = "研究助手"
description = "资料核验、项目分析和研究总结"
when_to_use = "需要比较方案或核验资料时"
instructions_file = "instructions.md"

[capabilities]
tools = ["builtin.read", "builtin.grep", "builtin.find", "builtin.ls"]
skills = ["research"]
mcp_servers = ["search"]
extensions = []

[limits]
max_model_calls = 8
max_tool_calls = 32

[delegation]
allowed_agents = ["writer"]
max_depth = 1
```

能力列表是限制，不是授权。没有列出的来源不会提供给该智能体；
`"*"` 表示允许该类别中的已启用能力，仍须经过宿主 Policy。
`default` 为保留 ID，代表使用原 SDK 指令和工具的默认智能体。
Card 的显示名称不影响稳定 ID；内容摘要用于记录实际执行版本。

切换智能体保留 Session 和历史。活动 Run 中选择新智能体会排队至下一个
Run，不会把一个尚未结束的模型请求突然变成另一个角色。选择写入会话树，
分支、复制、导出/导入和恢复沿用活动分支上的选择；原 Card 缺失时明确报错。
这是本地 Sec AgentCard，不是 A2A 网络协议。

Prompt 仍可作为一次性输入模板使用，但不再承担智能体身份管理。
已有角色 Prompt 不会被自动升级为获得工具权限的 Card：将角色说明放入
`instructions.md`，新建上述 Card 并显式列出允许的来源，再选择该 Agent。
旧的 `/prompt:name` 继续作为输入模板工作，不会偷偷改变当前 Agent 身份。

## Skill 不再只靠斜杠命令

目录沿用 `.sec/skills/<name>/SKILL.md` 和 `~/.sec/skills/`：

```markdown
---
name: research
description: 核验资料并给出可追溯的结论
activation: required
when_to_use: 用户要求研究、比较或核验资料时
match: 研究,比较,核验
---
先明确问题和证据范围，再调用已授权的检索能力。
区分观察到的事实、推断和未验证的假设。
```

- `auto` 为默认模式：模型每次请求看到简短目录，可调用 `skills_load` 加载正文。
- `manual`：仅供用户显式请求的工作流。
- `required`：必须配置非空 `match`；核心按大小写不敏感的子串匹配，
  在模型请求前加载正文，不依赖模型是否主动调用工具。

匹配不是语义分类器；只有显式触发词是确定性的。不要把普通词设为必用规则。
加载 Skill 只提供操作知识，不执行 Markdown 代码，也不批准其引用的工具。

`capabilities_search` 只检索该智能体范围内已启用的能力。大型工具集合使用
按需发现，匹配工具在后续模型请求中可调用。它不能安装软件、启用未知插件
或授予新的权限。

## MCP

MCP 服务由工作区配置和显式启用状态管理。支持本地 stdio 与远程
Streamable HTTP；服务的 tools、resources 和 prompts 保持不同语义。
连接凭据只能通过环境变量引用等配置机制提供，不写入 AgentCard。

配置放在 `.sec/mcp.json`，新条目只有显式 `enabled: true` 才会连接：

```json
{
  "servers": {
    "local-service": {
      "enabled": true,
      "transport": "stdio",
      "command": "/absolute/path/to/mcp-server",
      "args": [],
      "env": {"SERVICE_API_KEY": "${SERVICE_API_KEY}"}
    },
    "remote-service": {
      "enabled": false,
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${SERVICE_TOKEN}"}
    }
  }
}
```

将命令和 URL 换成实际服务，并在宿主环境提供引用的变量。配置不会安装
MCP 服务端。不要把凭据放进命令行参数或 URL；`env` 和认证 header 使用
变量引用。启用远程服务意味着允许建立连接，但其每次工具调用仍受策略约束。
`await client.set_mcp_enabled("remote-service", True)` 用于显式授权启用。

连接管理、工具发现和调用均由核心 Client 装配；MCP 工具与本地工具一样，
通过参数校验、Policy、审批和 Executor。服务自报的只读信息不自动产生
授权。调用结果保留工具级错误和结构化内容；不支持的模态明确报告，不把
失败伪装成成功。可能已经执行的非幂等调用不会自动重放。

声明 resources 或 prompts 的服务还会提供明确命名的协议适配工具：
`sec.resources.list` / `sec.resources.read` 和
`sec.prompts.list` / `sec.prompts.get`（实际模型工具名带有服务名和稳定摘要）。
它们只读取资源或展开模板，不把模板当作系统权限；同样受 Card 的
`mcp_servers` 范围、Policy 和审批控制。刷新期间已绑定的适配工具继续
访问旧连接。管理目录同时展示已启用和已禁用服务，但不会展示凭据。

## Reload 的准确语义

刷新流程是：

```text
发现和解析 -> 准备连接/worker -> 校验完整候选 -> 原子发布新 generation
                                                  |
旧请求及完整工具批次继续使用旧绑定 ------------------+
```

| 资源变化 | 生效边界 |
|---|---|
| Skill、MCP、工具目录 | 下一次安全的模型请求 |
| AgentCard 身份和指令 | 下一个 Run |
| 已发出的模型请求、该响应中的整个工具批次 | 保持原 generation |
| 进程内 Extension 的 Provider/Policy/hooks | 空闲 Run 边界 |
| SessionStore 更换 | 单独迁移，不属于普通刷新 |

解析、连接或注册失败时，候选被关闭，当前已发布版本保留；异常和结构化
刷新报告用于区分失败、已生效和等待边界，不能仅根据 UI 已更新判断成功。
旧连接和 worker 在占用它们的根 Run 完成后释放；关闭失败会明确传播。

紧急撤权与普通卸载不同：`await client.revoke_capability(tool_name)`
立即阻止尚未执行的调用，包括等待审批的调用；已经开始的外部副作用无法
通过目录刷新撤销。撤权不因后续 reload 自动解除。

## Python 插件代码热更新

原有 `enabled_extensions=` 保持进程内兼容模式，支持 Provider、存储、
命令和 hooks，但不能安全热替换已导入的任意 Python 对象。

需要无需重启宿主即可更新工具代码时，使用显式 `worker_extensions=`，
或 `set_worker_extensions(...)`。工具在新 Python worker 中重新导入，
校验后切换 generation。旧请求仍使用旧 worker，新请求使用新 worker。
更新失败不会替换当前执行版本。
通过管理 API 选择的 worker 扩展保存在 `.sec/worker-extensions.json`，
下次打开该工作区仍生效；显式构造参数可覆盖该列表。

worker 模式只接受可序列化的工具贡献。Provider、SessionStore、UI handler、
命令和任意 hooks 不会被静默丢弃，而是明确拒绝。一个扩展不要同时通过
进程内和 worker 两条路径启用。子进程隔离不是安全沙箱，仍只应启用可信代码。

Desktop 的“插件”页面可在停用状态选择 worker 或进程内执行方式；
已启用插件先停用再切换。worker 工具可在 Run 期间配置，并在下一安全请求
边界使用新版本；进程内扩展和全局权限仍需等待空闲。CLI 使用
`/plugins enable <name>` / `/plugins disable <name>` 管理 worker 插件，
`/reload enable <name>` / `/reload disable <name>` 保留进程内兼容语义。

### 实现位置与可运行样例

- [worker.py](../../sec/extensions/worker.py)：启动子进程、JSON-lines RPC、
  准备和发布工具版本、保留与释放旧 worker。
- [worker_host.py](../../sec/extensions/worker_host.py)：子进程入口，加载插件
  entry point、限制只注册工具、执行工具并返回结果。
- [client.py](../../sec/apis/client.py)：`_reload_resources()` 校验完整候选并
  切换能力快照；保留活动根 Run 使用的旧管理器，之后负责回收。
- [test_extension_workers.py](../../tests/test_extension_workers.py)：真实 worker
  子进程测试，包括同会话更新、旧绑定保留、失败回退和不支持的贡献。

[完整测试样例](../../examples/worker-hot-reload/README.md)提供一个可安装的只读插件、
无需模型或安装的自动演示，以及 CLI/Desktop 手动验收步骤。在仓库根目录运行：

```sh
PYTHONPATH=. .venv/bin/python examples/worker-hot-reload/demo.py
```

演示仅修改临时副本，不安装插件、不改变工作区配置；检查 v1 到 v2、
旧绑定仍用 v1、加载失败保留 v2、修复后加载 v3，以及旧进程释放。

## 多 Agent 委派

拥有显式 `allowed_agents` 的 Card 可使用 `agents_delegate`。子智能体：

- 使用独立 Session，只收到明确任务文本，不默认复制父会话。
- 同时受自身和祖先能力范围约束。
- 与父任务 turn 共用模型/工具调用总额度，并受最大深度限制；
  根 Run 的 follow-up 开启新 turn，保持既有每轮预算语义。
- 工具继续走相同 Policy；需要审批时转发给父 Run 的宿主。
- 接收父 Run 取消，结束后只返回结果文本。

父事件流记录父子 Run、Session、Agent 关联，并转发子工具和模型请求事件；
子正文不作为父助手的流式回答展示。根结果计量包含成功子 Run 的用量。
不支持多个 Agent 并发写同一个 Session，也不包含网络 A2A 或自动 swarm。

## 验收建议

先用本地确定性 Provider 和 MCP fixture 验证行为，再使用真实模型做交互验收：

1. 不输入 `/skill`，确认模型请求里存在能力目录，匹配的 required Skill 已加载。
2. 同一会话新增 Skill、刷新，检查下一次请求中的 generation。
3. 在模型请求未返回时更新工具，确认旧批次使用旧绑定。
4. 引入无效 Card、Skill 或失败连接，确认旧版本保留且错误可见。
5. 验证 MCP 工具级错误、复杂 schema 校验和审批。
6. 切换并恢复智能体，确认 Session ID 和历史不丢失。
7. 更新 worker 工具代码，确认旧绑定和新绑定分别执行对应代码。
8. 委派子任务，验证预算、审批、取消和父子关联。

自动化验收可以在安装 desktop 可选依赖后，在仓库根目录执行：

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=tests \
  .venv/bin/python -m unittest discover -s tests -q
```

本轮完整验收为 387 项通过（291 项核心、SDK、CLI 测试及 96 项 Desktop
测试），在同一个进程中运行，无需隔离 Qt 模块。包含真实本地 MCP
stdio／HTTP 服务、服务目录变更通知、真实 worker 子进程和同会话代码更新。
模型调用使用确定性测试 Provider，不代表已经测量真实模型主动选择能力的命中率。
