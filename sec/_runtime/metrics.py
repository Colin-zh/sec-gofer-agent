"""累积单个 sec Run 的模型请求、尝试、工具调用和 Provider 用量。

调用次数由运行时准确统计；token 用量只汇总 Provider 明确报告的数值。
失败尝试及缺失用量单独标记，不推测未返回的账单数据。
"""

from __future__ import annotations

from sec._types.messages import AssistantMessage, Message
from sec._types.results import RunUsage, RunResult


class RunMetricsAccumulator:
    """在一次 Run 内累计调用计数及可验证的 token 用量。"""

    def __init__(self) -> None:
        self.model_calls = 0
        self.model_attempts = 0
        self.retries = 0
        self.tool_calls = 0
        self.tool_attempts = 0
        self.tool_retries = 0
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._reported_responses = 0
        self._unreported_responses = 0
        self._unreported_attempts = 0

    def start_model_attempt(self, *, retry: bool) -> None:
        """记录一次实际 Provider 请求；重试也属于独立 attempt。"""
        self.model_attempts += 1
        if retry:
            self.retries += 1

    def record_failed_attempt(self) -> None:
        """标记失败 attempt 的计量结果未知。"""
        self._unreported_attempts += 1

    def start_tool_attempt(self, *, retry: bool) -> None:
        """记录一次实际工具执行 attempt。"""
        self.tool_attempts += 1
        if retry:
            self.tool_retries += 1

    def record_response(self, message: Message) -> None:
        """累计完整响应中 Provider 明确报告的 token 用量。"""
        if not isinstance(message, AssistantMessage) or (
            message.metadata.get("usage_reported") is not True
        ):
            self._unreported_responses += 1
            return

        input_tokens = _read_token_count(message.usage.input)
        output_tokens = _read_token_count(message.usage.output)
        if input_tokens is None and output_tokens is None:
            self._unreported_responses += 1
            return

        self._reported_responses += 1
        if input_tokens is None or output_tokens is None:
            self._unreported_responses += 1
        if input_tokens is not None:
            self._input_tokens = (self._input_tokens or 0) + input_tokens
        if output_tokens is not None:
            self._output_tokens = (self._output_tokens or 0) + output_tokens

    def usage(self) -> RunUsage:
        """返回不可变的当前用量快照。"""
        return RunUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            reported_responses=self._reported_responses,
            unreported_responses=self._unreported_responses,
            unreported_attempts=self._unreported_attempts,
        )

    def merge_child(self, result: RunResult) -> None:
        self.model_calls += result.model_calls
        self.model_attempts += result.model_attempts
        self.retries += result.retries
        self.tool_calls += result.tool_calls
        self.tool_attempts += result.tool_attempts
        self.tool_retries += result.tool_retries
        usage = result.usage
        if usage.input_tokens is not None:
            self._input_tokens = (self._input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            self._output_tokens = (self._output_tokens or 0) + usage.output_tokens
        self._reported_responses += usage.reported_responses
        self._unreported_responses += usage.unreported_responses
        self._unreported_attempts += usage.unreported_attempts

def _read_token_count(value: float) -> int | None:
    if isinstance(value, bool) or value < 0:
        return None
    if isinstance(value, int):
        return value
    if value.is_integer():
        return int(value)
    return None
