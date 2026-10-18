# 构建异步与流式 Client

`AsyncSecClient` 适合 asyncio 应用。Client 应使用 `async with` 管理生命周期；调用 `client.run(prompt)` 会启动一轮执行并立即返回 `Run` 句柄，不需要再 `await client.run(...)`。最终答案由 `await run.result()` 取得，过程事件从 `run.events()` 单独消费。

```python
import asyncio

from sec.apis import AsyncSecClient


async def main() -> None:
    async with AsyncSecClient("assistant", "遵循用户指令。") as client:
        run = client.run("用几句话介绍这个项目。")
        async for event in run.events():
            if event.type == "message_update":
                message_event = event.assistant_message_event
                if message_event.type == "text_delta":
                    print(message_event.delta, end="", flush=True)
            elif event.type == "tool_execution_start":
                print(f"\n[tool] {event.tool_name} started")
            elif event.type == "tool_execution_end":
                print(f"\n[tool] {event.tool_name} finished")

        result = await run.result()
        print("\nRun finished.")
        print(
            f"model calls={result.model_calls}, "
            f"tool calls={result.tool_calls}, "
            f"tokens={result.usage.total_tokens}"
        )


asyncio.run(main())
```

如果只关心最终文本，可以省略事件循环，直接 `result = await client.run(prompt).result()`。成功时 `RunResult` 包含最终消息、调用次数和 Provider 报告的用量；执行失败时 `result()` 会抛出原始执行异常。

同一个 Session 同时只允许一个活动 Run。若需并发执行彼此独立的任务，请使用不同 Session/Client。多个事件订阅彼此独立，但每个订阅只能由一个读取者消费；队列有界，消费太慢可能抛出 `SecSubscriptionOverflow`。

不要在同步 Python `>>>` 提示符中直接使用顶层 `async with`；将异步代码放入 coroutine 并通过 `asyncio.run()` 启动，或由现有事件循环调用它。

下一步：[控制 Run 并处理事件](09-control-runs-and-consume-events.md)。
