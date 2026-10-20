# 构建同步 Client

普通 Python 应用、脚本或同步 Web handler 可使用 `SecClient`。构造时给 Client 一个 Agent 名称和基础指令；进入上下文后，它会加载配置、创建 Provider、准备 Session 并装配执行运行时。

```python
from sec.apis import SecClient

with SecClient("assistant", "遵循用户指令。") as client:
    first = client.run("解释当前项目的入口。")
    print(first.text)
    print("model calls:", first.model_calls)
    print("tool calls:", first.tool_calls)

    second = client.run("再说明它如何加载配置。")
    print(second.text)
    print("Session:", client.session.id)
```

同一个 Client 管理同一个 Session；上述两次 `run()` 是连续两轮对话，第二轮会带上第一轮已提交的历史。每次 `run()` 返回成功的 `TraceResult`，常用字段如下：

- `text`：最终回答文本；也可读取完整的 `final_message` 和 `messages`。
- `model_calls`、`tool_calls`：逻辑调用计数；attempt 和重试次数另有独立字段。
- `usage`：Provider 报告的 token 用量；无法确认的用量保持未知。
- `stop_reason`：Provider 提供的停止原因，可能为空。

模型、工具或存储执行失败会抛出异常，不会返回一个伪装成功的 `TraceResult`。上下文管理器退出时会清理 Client 自己创建的资源。

先按[Provider 配置教程](02-configure-providers.md)配置模型和凭据。常用构造参数：

| 参数 | 作用 |
|---|---|
| `provider`、`model`、`api_key`、`base_url` | 覆盖 Provider 配置 |
| `tools` | 传入应用自己的 `Tool` 实例 |
| `policy`、`executor` | 注入策略和工具执行器 |
| `session_id` | 从配置的 SessionStore 恢复已有 Session |
| `session` | 注入已有 Session；不能同时传 `session_id`、`store` 或 `database` |
| `enabled_extensions` | 显式启用已安装 Extension |
| `max_model_calls`、`max_tool_calls` | 设置每个 turn 的调用预算 |

Client 显式参数优先于进程环境变量、`.env` 和代码默认值。`store` 与 `database` 不能同时注入；调用方注入的资源由调用方关闭。配置项较多时可先读 [`README.md`](../../README.md) 的快速开始，再查看[Provider 配置](02-configure-providers.md)和[Session 持久化](10-persist-and-transfer-sessions.md)。

与 CLI 不同，SDK Client 默认不附加 `read`、`write`、`edit`、`ls`、`find`、`grep` 或 `bash`。需要工具时，创建 `Tool` 后通过 `tools=(...)` 传入，或启用会注册工具的 Extension；工具定义示例见[Extension 工具教程](12-extend-sec-with-tools-and-hooks.md)。

## 什么时候使用同步接口

同步接口适合脚本、命令行任务或不需要自己管理 asyncio 的应用。`client.run(prompt)` 会阻塞当前线程直到该轮结束。需要逐步观察响应时，可使用 `client.stream(prompt)`：

```python
from sec.apis import SecClient

with SecClient("assistant", "遵循用户指令。") as client:
    with client.stream("介绍这个项目。") as stream:
        for event in stream:
            if event.type == "message_update":
                message_event = event.assistant_message_event
                if message_event.type == "text_delta":
                    print(message_event.delta, end="", flush=True)
        result = stream.result()
        print(f"\nStop reason: {result.stop_reason}")
```

详细的异步流式流程见[下一篇教程](08-build-an-async-streaming-client.md)。

若应用运行在已有 asyncio 事件循环（例如异步 Web 服务）中，不要从该事件循环调用阻塞接口；改用 `AsyncSecClient`。Client 离开上下文时会清理自身创建的 Provider/Database；调用方注入的 Database 或其他资源仍由调用方负责关闭。
