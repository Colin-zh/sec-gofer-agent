"""验证 sec Budget 对调用限制、重试和上下文压力的决策。"""

from __future__ import annotations

import unittest

from sec._runtime.budget import Budget
from sec.config import RetrySettings, ToolRetrySettings
from sec.exceptions import RetryableProviderError, RetryableToolError
from sec.tools.tool import tool


class BudgetDecisionTests(unittest.TestCase):
    """覆盖 Budget 的允许、重试、压缩及停止决策。"""

    def setUp(self) -> None:
        self.budget = Budget(
            model_retries=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
            tool_retries=ToolRetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        )

    def test_call_limits_are_checked_without_changing_turn_semantics(self) -> None:
        self.assertEqual(
            self.budget.model_call(calls_used=1, calls_limit=2).action,
            "allow",
        )
        self.assertEqual(
            self.budget.model_call(calls_used=2, calls_limit=2).action,
            "stop",
        )
        self.assertEqual(
            self.budget.tool_call(calls_used=0, calls_limit=1).action,
            "allow",
        )
        self.assertEqual(
            self.budget.tool_call(calls_used=1, calls_limit=1).action,
            "stop",
        )

    def test_model_retry_requires_retryable_error_and_remaining_attempt(self) -> None:
        decision = self.budget.model_retry(
            attempt=1,
            error=RetryableProviderError("temporary"),
        )
        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.retry_delay_seconds, 0)
        self.assertEqual(
            self.budget.model_retry(
                attempt=1,
                error=RuntimeError("permanent"),
            ).action,
            "stop",
        )
        self.assertEqual(
            self.budget.model_retry(
                attempt=2,
                error=RetryableProviderError("temporary"),
            ).action,
            "stop",
        )

    def test_tool_retry_requires_explicit_safe_mark_and_remaining_attempt(self) -> None:
        unsafe_tool = tool(lambda: "unused", name="write")
        safe_tool = tool(lambda: "unused", name="read", retry_safe=True)
        error = RetryableToolError("temporary")

        self.assertEqual(
            self.budget.tool_retry(
                tool=unsafe_tool,
                attempt=1,
                error=error,
            ).reason,
            "tool_not_marked_retry_safe",
        )
        self.assertEqual(
            self.budget.tool_retry(
                tool=safe_tool,
                attempt=1,
                error=error,
            ).action,
            "retry",
        )
        self.assertEqual(
            self.budget.tool_retry(
                tool=safe_tool,
                attempt=2,
                error=error,
            ).action,
            "stop",
        )

    def test_context_threshold_requests_compaction_and_prompt_to_finish(self) -> None:
        self.assertEqual(
            self.budget.context_pressure(
                estimated_tokens=3199,
                context_window_tokens=4000,
                compression_threshold=0.8,
            ).action,
            "allow",
        )
        decision = self.budget.context_pressure(
            estimated_tokens=3200,
            context_window_tokens=4000,
            compression_threshold=0.8,
        )
        self.assertEqual(decision.action, "compact")
        self.assertTrue(decision.request_final)
        self.assertEqual(decision.threshold_tokens, 3200)


if __name__ == "__main__":
    unittest.main()
