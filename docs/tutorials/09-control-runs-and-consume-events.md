# 控制 Run 并处理事件

一个 Run 表示一次 Agent 执行。SDK Client 的 `run(prompt)` 会立即返回 Run 句柄；调用方可等待结果、订阅事件或提交控制请求。

```python
import asyncio

from sec.apis import AsyncSecClient


async def main() -> None:
    async with AsyncSecClient("assistant", "遵循用户指令。") as client:
        run = client.run("调查这个问题")
        run.follow_up("结束后再给我三点摘要")

        async for event in run.events():
            if event.type == "tool_approval_requested":
                answer = await asyncio.to_thread(
                    input,
                    f"Allow {event.tool_name}? [y/N] ",
                )
                if answer.strip().casefold() in {"y", "yes"}:
                    run.approve_tool(event.tool_call_id)
                else:
                    run.deny_tool(
                        event.tool_call_id,
                        "The application user declined this tool call.",
                    )
            elif event.type == "agent_settled":
                print("Run 状态:", event.status)

        result = await run.result()
        print(result.text)


asyncio.run(main())
```

Run 句柄支持：

- `steer(text)`：将指令加入当前 turn；运行结束后返回 `False`。
- `follow_up(text)`：请求当前 turn 正常完成后再启动一个新 turn。
- `cancel()`：请求协作式取消。
- `approve_tool(id)` / `deny_tool(id, reason)`：响应待处理的工具审批。

这些控制方法会返回是否接受了请求；例如 Run 已结束后，`steer()` 或 `follow_up()` 会返回 `False`。`steer()` 影响当前 turn，`follow_up()` 会在当前 turn 正常结束后开启新 turn，并重置该轮预算。

示例在收到 `tool_approval_requested` 时把决定交给终端用户。要实际看到此事件，Client 必须配置会返回 `ask` 的 Policy，并提供相应工具；SDK 默认不会自动附加 CLI 的工具或审批 UI。真实应用应将 `input()` 换成自己的 UI/API 审批流程，并只在明确批准后调用 `approve_tool()`。事件还包括 Agent 生命周期、文本增量、工具开始/结束、重试和 turn 结束。事件缓冲有界，慢订阅者可能收到 `SecSubscriptionOverflow`；它不是持久化审计日志。

取消不是回滚：已开始的同步工具运行在线程中，取消不能保证中止它或撤销副作用。工具审批默认没有自动超时，宿主应提供明确的审批和取消策略。CLI 的 `/steer`、`/followup` 和 `/stop` 是对应的交互入口。
