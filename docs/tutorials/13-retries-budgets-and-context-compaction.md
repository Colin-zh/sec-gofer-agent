# 配置重试、预算与上下文压缩

这些设置可通过 Client 显式参数或环境变量配置；默认值见 [`.env.example`](../../.env.example)。

## 重试

- 模型请求默认最多尝试 3 次（包含首次请求），只对可重试的连接、超时、限流和服务端错误有限重试。
- 工具默认最多尝试 1 次。工具必须显式声明 `retry_safe=True`，且执行器抛出 `RetryableToolError`，才会按配置重试。
- 参数校验错误会作为错误结果发回模型；由于调用尚未执行，模型可修正参数并以新的工具调用 ID 再次请求。
- 工具可抛出 `RecoverableToolError("safe message for the model")`，明确请求把错误反馈给模型。消息须由工具作者确保不含敏感信息，最多 2000 个字符；运行时不自动重放该调用，并提醒模型核实可能的不确定副作用。
- `retry_safe=True` 工具的有限重试耗尽后，运行时只返回不含 Executor 异常详情的通用错误结果。非安全重试、普通 Executor 异常和协议错误仍会使 Trace 失败；审批拒绝作为错误结果继续交给模型，取消则终止 Trace。
- 结构化错误结果不会触发运行时自动重试。不要为可能产生重复副作用的工具开启安全重试；模型后续自行发起的调用是新的调用，不是原请求重放。

模型逻辑请求次数与实际 attempt 数分别统计。`TraceResult` 提供 `model_calls`、`model_attempts`、`retries`、`tool_calls`、`tool_attempts` 和 `tool_retries`。

## 每轮预算

默认每个 turn 最多 8 次模型逻辑调用和 32 次工具调用。`follow_up()` 开启新 turn 后会重置该轮预算。超过预算时 Trace 会以错误/限制状态结束，不会返回伪成功结果。

Provider 报告的 token 用量由 `TraceResult.usage` 汇总；未报告的用量保持未知，不会按字符数推测费用。

## 上下文压缩

上下文压缩默认关闭。启用时必须同时配置 `context_window_tokens` 和实现 `TokenEstimator` 的估算器；估算器必须匹配所用 Provider 的实际请求格式。可配置 `compression_threshold`，当估算上下文达到比例时，Sec 会请求同一 Provider 生成摘要并保留在 Session 历史中。

摘要请求也占用当轮模型调用预算；若预算不足以安全完成压缩和答复，Sec 会跳过压缩。不要只设置窗口大小而不提供估算器，也不要用通用字符比例冒充 token 估算。
