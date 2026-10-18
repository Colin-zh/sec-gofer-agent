# 配置重试、预算与上下文压缩

这些设置可通过 Client 显式参数或环境变量配置；默认值见 [`.env.example`](../../.env.example)。

## 重试

- 模型请求默认最多尝试 3 次（包含首次请求），只对可重试的连接、超时、限流和服务端错误有限重试。
- 工具默认最多尝试 1 次。工具必须显式声明 `retry_safe=True`，且执行器抛出 `RetryableToolError`，才会按配置重试。
- 结构化的工具错误结果不会触发重试。不要为可能产生重复副作用的工具开启安全重试。

模型逻辑请求次数与实际 attempt 数分别统计。`RunResult` 提供 `model_calls`、`model_attempts`、`retries`、`tool_calls`、`tool_attempts` 和 `tool_retries`。

## 每轮预算

默认每个 turn 最多 8 次模型逻辑调用和 32 次工具调用。`follow_up()` 开启新 turn 后会重置该轮预算。超过预算时 Run 会以错误/限制状态结束，不会返回伪成功结果。

Provider 报告的 token 用量由 `RunResult.usage` 汇总；未报告的用量保持未知，不会按字符数推测费用。

## 上下文压缩

上下文压缩默认关闭。启用时必须同时配置 `context_window_tokens` 和实现 `TokenEstimator` 的估算器；估算器必须匹配所用 Provider 的实际请求格式。可配置 `compression_threshold`，当估算上下文达到比例时，Sec 会请求同一 Provider 生成摘要并保留在 Session 历史中。

摘要请求也占用当轮模型调用预算；若预算不足以安全完成压缩和答复，Sec 会跳过压缩。不要只设置窗口大小而不提供估算器，也不要用通用字符比例冒充 token 估算。
