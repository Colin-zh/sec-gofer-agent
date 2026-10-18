"""统一评估运行调用、重试和上下文压力的 Budget 决策。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sec._runtime.retry import retry_delay_seconds
from sec.config import RetrySettings, ToolRetrySettings
from sec.exceptions import RetryableProviderError, RetryableToolError
from sec.tools.tool import Tool

BudgetAction = Literal["allow", "retry", "compact", "request_final", "stop"]


@dataclass(frozen=True)
class BudgetDecision:
    """描述 Budget 对一次运行条件给出的明确动作及其原因。"""

    action: BudgetAction
    reason: str
    retry_delay_seconds: float = 0.0
    estimated_tokens: int | None = None
    threshold_tokens: int | None = None
    request_final: bool = False

    def to_dict(self) -> dict[str, object]:
        """返回稳定的预算决策载荷。"""
        return {
            "action": self.action,
            "reason": self.reason,
            "retry_delay_seconds": self.retry_delay_seconds,
            "estimated_tokens": self.estimated_tokens,
            "threshold_tokens": self.threshold_tokens,
            "request_final": self.request_final,
        }


class Budget:
    """根据配置与运行观测作出允许、重试、压缩、收尾或停止决策。

    Budget 不执行模型、工具或摘要操作；运行时组件负责执行并发布决策。
    """

    def __init__(
        self,
        *,
        model_retries: RetrySettings | None = None,
        tool_retries: ToolRetrySettings | None = None,
    ) -> None:
        self.model_retries = (
            model_retries if model_retries is not None else RetrySettings()
        )
        self.tool_retries = (
            tool_retries if tool_retries is not None else ToolRetrySettings()
        )

    def model_call(
        self,
        *,
        calls_used: int,
        calls_limit: int,
    ) -> BudgetDecision:
        """判断当前 turn 是否还能开始一个逻辑模型请求。"""
        if calls_used < calls_limit:
            return BudgetDecision("allow", "model_call_within_turn_limit")
        return BudgetDecision("stop", "model_call_turn_limit_reached")

    def tool_call(
        self,
        *,
        calls_used: int,
        calls_limit: int,
    ) -> BudgetDecision:
        """判断当前 turn 是否还能执行一个新的逻辑工具调用。"""
        if calls_used < calls_limit:
            return BudgetDecision("allow", "tool_call_within_turn_limit")
        return BudgetDecision("stop", "tool_call_turn_limit_reached")

    def model_retry(
        self,
        *,
        attempt: int,
        error: Exception,
    ) -> BudgetDecision:
        """判断暂时性 Provider 失败是否仍有重试额度。"""
        if not isinstance(error, RetryableProviderError):
            return BudgetDecision("stop", "model_error_not_retryable")
        if attempt >= self.model_retries.max_attempts:
            return BudgetDecision("stop", "model_retry_limit_reached")
        delay = retry_delay_seconds(
            self.model_retries.base_delay_seconds,
            self.model_retries.max_delay_seconds,
            failed_attempt=attempt,
            retry_after_seconds=error.retry_after_seconds,
        )
        return BudgetDecision(
            "retry",
            "retryable_model_error_with_attempts_remaining",
            retry_delay_seconds=delay,
        )

    def tool_retry(
        self,
        *,
        tool: Tool,
        attempt: int,
        error: Exception,
    ) -> BudgetDecision:
        """仅允许显式安全工具对暂时性错误进行有限重试。"""
        if not isinstance(error, RetryableToolError):
            return BudgetDecision("stop", "tool_error_not_retryable")
        if not tool.retry_safe:
            return BudgetDecision("stop", "tool_not_marked_retry_safe")
        if attempt >= self.tool_retries.max_attempts:
            return BudgetDecision("stop", "tool_retry_limit_reached")
        delay = retry_delay_seconds(
            self.tool_retries.base_delay_seconds,
            self.tool_retries.max_delay_seconds,
            failed_attempt=attempt,
            retry_after_seconds=error.retry_after_seconds,
        )
        return BudgetDecision(
            "retry",
            "retryable_safe_tool_error_with_attempts_remaining",
            retry_delay_seconds=delay,
        )

    @staticmethod
    def context_pressure(
        *,
        estimated_tokens: int,
        context_window_tokens: int,
        compression_threshold: float,
        max_output_tokens: int = 0,
        model_calls_used: int | None = None,
        model_calls_limit: int | None = None,
        compacted: bool = False,
    ) -> BudgetDecision:
        """判断当前请求上下文是否达到压缩阈值。"""
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must not be negative")
        if context_window_tokens < 1:
            raise ValueError("context_window_tokens must be at least 1")
        if max_output_tokens < 0 or max_output_tokens >= context_window_tokens:
            raise ValueError(
                "max_output_tokens must be non-negative and less than "
                "context_window_tokens"
            )
        if not 0 < compression_threshold <= 1:
            raise ValueError("compression_threshold must be in (0, 1]")
        threshold_tokens = min(
            int(context_window_tokens * compression_threshold),
            context_window_tokens - max_output_tokens,
        )
        if estimated_tokens < threshold_tokens:
            return BudgetDecision(
                "allow",
                "context_below_compression_threshold",
                estimated_tokens=estimated_tokens,
                threshold_tokens=threshold_tokens,
            )
        if compacted:
            return BudgetDecision(
                "request_final",
                "context_still_pressured_after_compaction",
                estimated_tokens=estimated_tokens,
                threshold_tokens=threshold_tokens,
                request_final=True,
            )
        if model_calls_used is not None and model_calls_limit is not None:
            if model_calls_limit - model_calls_used < 2:
                return BudgetDecision(
                    "request_final",
                    "insufficient_model_calls_for_compaction_and_response",
                    estimated_tokens=estimated_tokens,
                    threshold_tokens=threshold_tokens,
                    request_final=True,
                )
        return BudgetDecision(
            "compact",
            "context_compression_threshold_reached",
            estimated_tokens=estimated_tokens,
            threshold_tokens=threshold_tokens,
            request_final=True,
        )
