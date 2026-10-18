"""校验、授权、审批并委托 Executor 执行模型提出的工具调用。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import time

from sec._types.approvals import ToolApprovalRequest
from sec._types.content import TextContent, ToolUseContent
from sec._types.events import (
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolRetryEndEvent,
    ToolRetryStartEvent,
)
from sec._types.messages import ToolResultMessage
from sec._runtime.budget import Budget
from sec._runtime.metrics import RunMetricsAccumulator
from sec._runtime.control import RunControl
from sec._runtime.event_stream import RunEventStream
from sec._runtime.state import RunState
from sec.exceptions import (
    RetryableToolError,
    RunLimitExceeded,
    ToolExecutorProtocolError,
)
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec.tools.tool import Tool
from sec._types.policy import PolicyContext, PolicyDecision


async def execute_tool_batch(
    tool_uses: list[ToolUseContent],
    *,
    tools_by_name: Mapping[str, Tool],
    agent_name: str,
    policy: PolicyEngine,
    executor: ToolExecutor,
    control: RunControl,
    state: RunState,
    remaining_tool_calls: int,
    events: RunEventStream,
    turn_id: str,
    budget: Budget,
    metrics: RunMetricsAccumulator,
) -> tuple[list[ToolResultMessage], int, Exception | None, bool]:
    """执行一个工具调用批次，并为每次调用保留独立结果和事件。"""
    results: list[ToolResultMessage] = []
    failure: Exception | None = None
    stopped = False

    for index, tool_use in enumerate(tool_uses):
        if failure is not None:
            results.append(
                _error_result(
                    tool_use,
                    "Not executed because an earlier tool call failed.",
                )
            )
            continue
        if control.stop_requested:
            stopped = True
            results.append(
                _error_result(tool_use, "Not executed because the run was stopped.")
            )
            continue
        call_decision = budget.tool_call(
            calls_used=index,
            calls_limit=remaining_tool_calls,
        )
        if call_decision.action == "stop":
            failure = RunLimitExceeded(
                "run reached its configured limit of tool calls"
            )
            results.append(
                _error_result(
                    tool_use,
                    "Not executed because the tool-call limit was reached.",
                )
            )
            continue
        try:
            result, call_stopped = await _execute_one_tool(
                tool_use,
                tools_by_name,
                agent_name,
                policy,
                executor,
                control,
                state,
                events,
                turn_id,
                budget,
                metrics,
            )
        except Exception as exc:
            failure = exc
            result = _error_result(
                tool_use,
                f"Tool execution failed ({type(exc).__name__}).",
            )
            events.publish(
                ToolExecutionEndEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    result=result.text,
                    is_error=True,
                )
            )
        else:
            stopped = stopped or call_stopped
        results.append(result)

    return results, len(tool_uses), failure, stopped


async def _execute_one_tool(
    tool_use: ToolUseContent,
    tools_by_name: Mapping[str, Tool],
    agent_name: str,
    policy: PolicyEngine,
    executor: ToolExecutor,
    control: RunControl,
    state: RunState,
    events: RunEventStream,
    turn_id: str,
    budget: Budget,
    metrics: RunMetricsAccumulator,
) -> tuple[ToolResultMessage, bool]:
    """校验并授权工具调用，必要时等待审批，再委托 Executor 执行。"""
    registered = tools_by_name.get(tool_use.name)
    if registered is None:
        return _error_result(tool_use, "Unknown tool."), False
    if not isinstance(tool_use.arguments, dict):
        return _error_result(
            tool_use,
            "Tool arguments must be a JSON object.",
        ), False

    registered.validate_arguments(tool_use.arguments)
    mode_name = getattr(policy, "mode_name", None)
    if mode_name is not None and not isinstance(mode_name, str):
        raise TypeError("policy mode_name must be a string or None")
    decision = policy.decide(
        PolicyContext(
            agent_name=agent_name,
            mode_name=mode_name,
            capability_type=registered.capability_type,
            capability_name=tool_use.name,
            source=registered.source,
            operation=tool_use.name,
            arguments=tool_use.arguments,
            risk_level=registered.risk_level,
            workspace_scoped=registered.workspace_scoped,
        )
    )
    if not isinstance(decision, PolicyDecision):
        raise TypeError("policy decide() must return PolicyDecision")
    if decision.outcome == "deny":
        return _error_result(tool_use, decision.reason or "Tool call denied."), False
    if decision.outcome == "ask":
        request = ToolApprovalRequest(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            capability_type=registered.capability_type,
            source=registered.source,
            operation=tool_use.name,
            mode_name=mode_name,
            risk_level=registered.risk_level,
            reason=decision.reason or "Policy requested approval.",
            arguments=tool_use.arguments,
        )
        if not control.begin_tool_approval(request):
            return _error_result(
                tool_use,
                "Tool call was not approved because the Run was stopped.",
            ), True
        events.publish(
            ToolApprovalRequestedEvent(
                tool_call_id=tool_use.id,
                tool_name=tool_use.name,
                args=tool_use.arguments,
                reason=request.reason,
            )
        )
        state.wait_for_approval()
        approval = await control.wait_for_tool_approval(tool_use.id)
        if approval is None or approval.outcome == "cancelled":
            events.publish(
                ToolApprovalResolvedEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    outcome="cancelled",
                    reason="Run stopped before tool approval completed.",
                )
            )
            return _error_result(
                tool_use,
                "Tool call was not approved because the Run was stopped.",
            ), True
        if approval.outcome == "denied":
            state.resume_after_approval()
            denial_reason = approval.reason or "Tool call was denied by the user."
            events.publish(
                ToolApprovalResolvedEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    outcome=approval.outcome,
                    reason=denial_reason,
                )
            )
            return _error_result(tool_use, denial_reason), False
        state.resume_after_approval()
        events.publish(
            ToolApprovalResolvedEvent(
                tool_call_id=tool_use.id,
                tool_name=tool_use.name,
                outcome=approval.outcome,
            )
        )

    events.publish(
        ToolExecutionStartEvent(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            args=tool_use.arguments,
        )
    )
    result: ToolResultMessage | None = None
    retry_started = False
    attempt = 0
    try:
        for attempt in range(1, budget.tool_retries.max_attempts + 1):
            metrics.start_tool_attempt(retry=attempt > 1)
            try:
                attempt_result = await executor.execute(registered, tool_use)
                if not isinstance(attempt_result, ToolResultMessage):
                    raise ToolExecutorProtocolError(
                        "tool executor must return a ToolResultMessage"
                    )
                if attempt_result.tool_call_id != tool_use.id:
                    raise ToolExecutorProtocolError(
                        "tool executor result ID must match the requested tool call"
                    )
                if attempt_result.tool_name != tool_use.name:
                    raise ToolExecutorProtocolError(
                        "tool executor result name must match the requested tool"
                    )
            except RetryableToolError as exc:
                decision = budget.tool_retry(
                    tool=registered,
                    attempt=attempt,
                    error=exc,
                )
                will_retry = decision.action == "retry"
                if not will_retry:
                    raise
                delay_seconds = decision.retry_delay_seconds
                retry_started = True
                events.publish(
                    ToolRetryStartEvent(
                        tool_call_id=tool_use.id,
                        tool_name=tool_use.name,
                        attempt=attempt,
                        max_attempts=budget.tool_retries.max_attempts,
                        delay_ms=delay_seconds * 1000,
                        error_message=str(exc),
                    )
                )
                await asyncio.sleep(delay_seconds)
            else:
                result = attempt_result
                break
    except BaseException as exc:
        if retry_started:
            events.publish(
                ToolRetryEndEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    success=False,
                    attempt=attempt,
                    final_error=str(exc),
                )
            )
        raise
    if result is None:
        raise AssertionError("tool retry loop exited without a result")

    if retry_started:
        events.publish(
            ToolRetryEndEvent(
                tool_call_id=tool_use.id,
                tool_name=tool_use.name,
                success=True,
                attempt=attempt,
            )
        )
    events.publish(
        ToolExecutionEndEvent(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            result=result.text,
            is_error=result.is_error,
        )
    )
    return result, False


def _error_result(tool_use: ToolUseContent, content: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tool_use.id,
        tool_name=tool_use.name,
        content=[TextContent(text=content)],
        is_error=True,
        timestamp=time(),
    )
