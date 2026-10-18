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
- `register_before_run_hook`、`register_after_run_hook`、`register_run_event_hook`：接入 Run 生命周期。

Run event hook 必须同步且快速；before/after hooks 可为同步或异步回调。Hook 抛出异常会使 Run 失败，不会静默忽略。扩展应只释放自己拥有的资源，不要关闭宿主注入的 Database 等资源。

开发完成后可构建 wheel 并在隔离环境中验证 entry point；仓库的 Extension 安装烟雾测试可用：

```sh
uv run python -m unittest discover -s tests -p test_extension_installation.py -v
```
