# 注册工具及其他 Extension 能力

Extension 的 `activate(api)` 可通过 Extension API 注册多种贡献。所有贡献名称应使用小写 kebab-case；重名注册会在加载时明确失败。

## 注册工具

工具使用 Sec 现有的 `@tool` 工厂生成定义和参数 schema：

```python
from sec.tools import tool


@tool(name="hello-greeter", description="Greet a person.")
def greet(name: str) -> str:
    return f"Hello, {name}!"


class MyExtension:
    name = "my-extension"

    async def activate(self, api) -> None:
        api.register_tool(greet)
```

工具还可声明 `risk_level`、`source`、`workspace_scoped` 和 `retry_safe` 等元数据。参数会在调用前按函数签名校验。只有确认重复执行安全的工具才应设置 `retry_safe=True`；详细策略见[权限与工具教程](06-tools-permissions-and-approvals.md)和[重试教程](13-retries-budgets-and-context-compaction.md)。

桌面或其他 Python 应用也可直接使用 `from sec.tools import create_builtin_tools` 获取绑定到工作区的内置工具，并通过 Client 的 `tools=` 参数提供；工具审批仍应由宿主应用显式处理。

## 其他贡献类型

- `register_command(SlashCommand(...))`：增加 CLI 命令。
- `register_provider(name, factory)`：增加模型 Provider；运行时通过 `provider` 选择。
- `register_policy(name, policy)`：增加 Policy；通过 `policy_name` 选择。
- `register_storage_backend(name, factory)`：增加 SessionStore 后端；通过存储配置选择。
- `register_ui_handler(name, handler)`：供应用通过 Client 的 UI handler 接口调用。
- `register_hook(name, handler)`：注册变换型 hook（见下）。

扩展应只释放自己拥有的资源，不要关闭宿主注入的 Database 等资源。

## 变换型 Hook

`api.register_hook(name, handler)` 注册可改写 Trace 数据的 hook（与 pi-main 的
`on()` 对齐）。`handler(event, ctx)` 可同步或异步，返回对应 hook 的 result
类型或 `None`；多个 handler 按注册顺序执行，聚合语义由运行时的
`HookExecutor` 施加：

| hook | 语义 | 可改写 |
|---|---|---|
| `before_trace` | collect | 注入消息 |
| `before_drive` | fail-closed | 无（异常即中断） |
| `before_trace_end` | collect | 追加 follow-up |
| `transform_context` | 链式 | 上下文消息、系统提示 |
| `before_request` | 链式 | 请求选项 |
| `before_payload` | 链式 | wire payload（脱敏等） |
| `after_response` | 链式 | 模型响应消息 |
| `before_tool` | 链式 + block | 工具参数 / 拦截 |
| `after_tool` | 链式 | 工具结果 / terminate |
| `before_compaction` | first-wins | decline / 自定义摘要 |
| `before_navigation` | first-wins | decline / 自定义摘要 |

- **链式**：前一个 handler 的输出作为后一个的输入。
- **fail-closed**：任一 handler 抛异常即向上传播，使 Trace 失败，不静默忽略。
- **first-wins**：第一个返回有效结果的 handler 生效，其余跳过。
- **collect**：汇总所有 handler 的注入/follow-up。

`ctx.ui` 提供阻塞 UI 原语（`select` / `confirm` / `input` / `editor` /
`notify`），扩展可在 hook 内阻塞等待宿主（CLI / Desktop）渲染并回传响应。
工具拦截是两层 HITL：`before_tool` 先运行（可改写参数，`block` 则直接短路，
不再询问 Policy），随后才由 `PolicyEngine.decide` 门控。

开发完成后可构建 wheel 并在隔离环境中验证 entry point；仓库的 Extension 安装烟雾测试可用：

```sh
uv run python -m unittest discover -s tests -p test_extension_installation.py -v
```
