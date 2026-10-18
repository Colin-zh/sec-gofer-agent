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

## 观测模型请求

模型请求沿用同一条 `run.events()` 事件流，不需要单独包装 Provider：

- `model_request_start`（`ModelRequestStartEvent`）：在每次实际 Provider attempt 前发布，
  `input` 包含最终传给 Provider 的标准化 `messages` 和 `tools` 快照，包含上下文压缩、
  系统指令及最终答复提示生效后的内容，不是供应商的原始 HTTP 请求体。
- `model_request_end`（`ModelRequestEndEvent`）：记录该 attempt 的完成、失败或取消。
  `response` 是已有的 `AssistantMessage` 快照（无响应时为 `None`），`error` 保留错误信息，
  `elapsed_ms` 使用单调时钟计时，不包含重试等待。

两种事件都有 `request_id`、`turn_id`、`attempt` 和 `purpose`。同一次逻辑请求的重试共享
`request_id`，每次 attempt 从 1 开始递增；`purpose` 为 `response` 或 `compaction`。
开始时间 `started_at` 和结束时间 `ended_at` 使用 Unix wall-clock 时间。
取消发生在重试等待期间时，上一次 attempt 已是失败状态，不会为尚未发出的请求创建节点。
Steering 中断的 attempt 记录为取消，后续生成使用新的请求 ID。

```python
from sec.apis import ModelRequestEndEvent, ModelRequestStartEvent

async for event in run.events():
    if isinstance(event, ModelRequestStartEvent):
        print("模型请求", event.request_id, event.attempt, event.purpose)
    elif isinstance(event, ModelRequestEndEvent):
        print("模型结果", event.status, event.elapsed_ms)
```

`message_start` / `message_end` 仍表示会话消息的提交边界，不应用来测量模型请求耗时。
这些新增事件是进程内的原始观测数据，可能包含用户文本、模型输出和敏感字段；
SDK 不自动替订阅者脱敏或持久化。记录到日志或数据库前，宿主必须应用自己的脱敏、
内容保留及大小限制策略。Desktop 的本地 Trace 会脱敏和截断，并排除推理正文与二进制附件。
