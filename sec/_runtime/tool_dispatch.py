"""校验、授权、审批并委托 Executor 执行模型提出的工具调用。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from time import time

from sec._types.approvals import ToolApprovalRequest
from sec._types.content import TextContent, ToolUseContent
from sec._types.hooks import HookContext
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
from sec._runtime.hooks import HookExecutor
from sec._runtime.metrics import TraceMetricsAccumulator
from sec._runtime.control import TraceControl
from sec._runtime.event_stream import TraceEventStream
from sec._runtime.state import TraceState
from sec.exceptions import (
    RecoverableToolError,
    RetryableToolError,
    TraceLimitExceeded,
    ToolExecutorProtocolError,
)
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec.tools.tool import Tool
from sec.tools._builtins.question import QUESTION_TOOL_NAME
from sec._types.policy import PolicyContext, PolicyDecision

_MAX_MODEL_TOOL_FEEDBACK_CHARS = 2000


@dataclass
class _PreparedToolCall:
    """A validated, authorized call ready for approval (ask) or execution (allow)."""

    tool_use: ToolUseContent
    registered: Tool
    mode_name: str | None
    decision: PolicyDecision


@dataclass
class _StagedTool:
    """Memoized prepare outcome for one tool_use index (exactly one field set)."""

    prepared: _PreparedToolCall | None = None
    result: ToolResultMessage | None = None
    terminate: bool = False
    exception: BaseException | None = None


def _parallel_eligible(prepared: _PreparedToolCall) -> bool:
    """A prepared call may join a parallel block iff safe_parallel and allowed."""
    return prepared.registered.safe_parallel and prepared.decision.outcome == "allow"


async def execute_tool_batch(
    tool_uses: list[ToolUseContent],
    *,
    tools_by_name: Mapping[str, Tool],
    agent_name: str,
    policy: PolicyEngine,
    executor: ToolExecutor,
    control: TraceControl,
    state: TraceState,
    remaining_tool_calls: int,
    events: TraceEventStream,
    turn_id: str,
    budget: Budget,
    metrics: TraceMetricsAccumulator,
    hooks: HookExecutor,
    hook_ctx: HookContext,
    is_revoked: Callable[[str], bool] | None = None,
    consume_tool: Callable[[], None] | None = None,
) -> tuple[list[ToolResultMessage], int, Exception | None, bool]:
    """执行一个工具调用批次，把连续的只读安全工具合并为并行块，其余按串行屏障执行。

    结果按 tool_uses 原始顺序回填；撤销、前置失败、停止与预算短路保持与原串行实现
    一致的语义。并行块只容纳策略直接 allow 的 safe_parallel 工具，因此不会触及
    单槽位审批（Trace 同一时间只能等待一个工具审批）。
    """
    total = len(tool_uses)
    results: list[ToolResultMessage | None] = [None] * total
    failure: Exception | None = None
    stopped = False
    prepared: dict[int, _StagedTool] = {}

    async def prepare(index: int) -> _StagedTool:
        """校验并授权一个工具调用，不消耗预算/配额；结果按索引 memo 一次。"""
        if index in prepared:
            return prepared[index]
        staged = _StagedTool()
        try:
            result_or_prepared, terminate = await _prepare_tool_call(
                tool_uses[index],
                tools_by_name=tools_by_name,
                agent_name=agent_name,
                policy=policy,
                hooks=hooks,
                hook_ctx=hook_ctx,
            )
        except Exception as exc:
            staged.exception = exc
        else:
            if isinstance(result_or_prepared, ToolResultMessage):
                staged.result = result_or_prepared
                staged.terminate = terminate
            else:
                staged.prepared = result_or_prepared
        prepared[index] = staged
        return staged

    def reserve(index: int) -> Exception | None:
        """为一次真正执行的工具预留预算/配额；耗尽时返回限制异常。"""
        decision = budget.tool_call(
            calls_used=index, calls_limit=remaining_tool_calls
        )
        if decision.action == "stop":
            return TraceLimitExceeded(
                "run reached its configured limit of tool calls"
            )
        if consume_tool is not None:
            try:
                consume_tool()
            except TraceLimitExceeded as error:
                return error
        return None

    async def execute_member(
        index: int, prepared_call: _PreparedToolCall,
    ) -> tuple[int, ToolResultMessage, bool, Exception | None]:
        """并发执行一个并行块成员；普通异常转为 executor_failure 并回传。"""
        if prepared_call.decision.outcome != "allow":
            raise RuntimeError("parallel tool unexpectedly requires approval")
        try:
            result, terminate = await _execute_prepared_tool(
                prepared_call,
                executor=executor,
                control=control,
                state=state,
                events=events,
                turn_id=turn_id,
                budget=budget,
                metrics=metrics,
                hooks=hooks,
                hook_ctx=hook_ctx,
                is_revoked=is_revoked,
            )
        except Exception as exc:
            tool_use = tool_uses[index]
            result = _error_result(
                tool_use,
                f"Tool execution failed ({type(exc).__name__}).",
                error_kind="executor_failure",
            )
            events.publish(
                ToolExecutionEndEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    result=result.text,
                    is_error=True,
                )
            )
            return index, result, False, exc
        return index, result, terminate, None

    i = 0
    while i < total:
        tool_use = tool_uses[i]
        # 串行前奏：撤销 / 前置失败 / 停止，短路顺序与原实现一致。
        if is_revoked is not None and is_revoked(tool_use.name):
            results[i] = _error_result(
                tool_use, "Capability was revoked before execution.",
                error_kind="capability_revoked",
            )
            i += 1
            continue
        if failure is not None:
            results[i] = _error_result(
                tool_use,
                "Not executed because an earlier tool call failed.",
                error_kind="not_executed",
            )
            i += 1
            continue
        if control.stop_requested:
            stopped = True
            results[i] = _error_result(
                tool_use,
                "Not executed because the run was stopped.",
                error_kind="cancelled",
            )
            i += 1
            continue

        staged = await prepare(i)
        if staged.exception is not None:
            failure = staged.exception
            results[i] = _error_result(
                tool_use,
                f"Tool execution failed ({type(staged.exception).__name__}).",
                error_kind="executor_failure",
            )
            events.publish(
                ToolExecutionEndEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    result=results[i].text,
                    is_error=True,
                )
            )
            i += 1
            continue
        if staged.prepared is None:
            results[i] = staged.result
            stopped = stopped or staged.terminate
            i += 1
            continue

        # 串行屏障：需要审批的 ask，或非并行安全的 allow。
        if not _parallel_eligible(staged.prepared):
            reserve_error = reserve(i)
            if reserve_error is not None:
                failure = reserve_error
                results[i] = _error_result(
                    tool_use,
                    "Not executed because the tool-call limit was reached.",
                    error_kind="tool_call_limit",
                )
                i += 1
                continue
            try:
                result, terminate = await _execute_prepared_tool(
                    staged.prepared,
                    executor=executor,
                    control=control,
                    state=state,
                    events=events,
                    turn_id=turn_id,
                    budget=budget,
                    metrics=metrics,
                    hooks=hooks,
                    hook_ctx=hook_ctx,
                    is_revoked=is_revoked,
                )
            except Exception as exc:
                failure = exc
                result = _error_result(
                    tool_use,
                    f"Tool execution failed ({type(exc).__name__}).",
                    error_kind="executor_failure",
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
                stopped = stopped or terminate
            results[i] = result
            i += 1
            continue

        # 并行块：收集连续可并行的索引。
        block = [i]
        j = i + 1
        while j < total:
            if is_revoked is not None and is_revoked(tool_uses[j].name):
                break
            if control.stop_requested:
                break
            staged_j = await prepare(j)
            if (
                staged_j.exception is not None
                or staged_j.prepared is None
                or not _parallel_eligible(staged_j.prepared)
            ):
                break
            block.append(j)
            j += 1

        # 按索引顺序预留预算；中途耗尽则其后的块成员不再执行。
        runnable: list[int] = []
        for position, k in enumerate(block):
            reserve_error = reserve(k)
            if reserve_error is not None:
                failure = reserve_error
                results[k] = _error_result(
                    tool_uses[k],
                    "Not executed because the tool-call limit was reached.",
                    error_kind="tool_call_limit",
                )
                for later in block[position + 1:]:
                    results[later] = _error_result(
                        tool_uses[later],
                        "Not executed because an earlier tool call failed.",
                        error_kind="not_executed",
                    )
                break
            runnable.append(k)

        executed = await asyncio.gather(
            *(execute_member(k, prepared[k].prepared) for k in runnable)
        )
        for k, result, terminate, exc in executed:
            results[k] = result
            stopped = stopped or terminate
            if exc is not None and failure is None:
                failure = exc
        i = j

    return [r for r in results if r is not None], total, failure, stopped


async def _prepare_tool_call(
    tool_use: ToolUseContent,
    *,
    tools_by_name: Mapping[str, Tool],
    agent_name: str,
    policy: PolicyEngine,
    hooks: HookExecutor,
    hook_ctx: HookContext,
) -> tuple[_PreparedToolCall | ToolResultMessage, bool]:
    """查找、校验、改写并授权一次工具调用；返回待执行项或立即错误结果。

    返回 `(_PreparedToolCall, False)` 表示 allow/ask；否则返回
    `(_error_result(...), terminate)` 表示 unknown_tool/invalid_arguments/
    extension_blocked/policy_denied。策略异常与 hook 异常向调用方上抛。
    """
    registered = tools_by_name.get(tool_use.name)
    if registered is None:
        return _error_result(
            tool_use,
            "Unknown tool.",
            error_kind="unknown_tool",
        ), False
    if not isinstance(tool_use.arguments, dict):
        return _error_result(
            tool_use,
            "Tool arguments must be a JSON object.",
            error_kind="invalid_arguments",
        ), False

    try:
        registered.validate_arguments(tool_use.arguments)
    except TypeError as exc:
        return _error_result(
            tool_use,
            f"Invalid tool arguments: {exc}. This call was not executed; "
            "correct the arguments and issue a new tool call.",
            error_kind="invalid_arguments",
        ), False
    args, block = await hooks.before_tool(
        tool_use.id, tool_use.name, tool_use.arguments, hook_ctx
    )
    if block is not None:
        return _error_result(
            tool_use,
            block.reason or "Tool call blocked by extension.",
            error_kind="extension_blocked",
        ), block.terminate
    if args != tool_use.arguments:
        tool_use = replace(tool_use, arguments=args)
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
        return _error_result(
            tool_use,
            decision.reason or "Tool call denied.",
            error_kind="policy_denied",
        ), False
    return _PreparedToolCall(
        tool_use=tool_use,
        registered=registered,
        mode_name=mode_name,
        decision=decision,
    ), False


async def _execute_prepared_tool(
    prepared: _PreparedToolCall,
    *,
    executor: ToolExecutor,
    control: TraceControl,
    state: TraceState,
    events: TraceEventStream,
    turn_id: str,
    budget: Budget,
    metrics: TraceMetricsAccumulator,
    hooks: HookExecutor,
    hook_ctx: HookContext,
    is_revoked: Callable[[str], bool] | None = None,
) -> tuple[ToolResultMessage, bool]:
    """执行已授权的工具调用：需审批的等待审批，其余委托 Executor 并保留重试语义。

    只消费 `prepared.decision`，绝不重新调用策略；ask 分支保持单槽位审批语义。
    """
    tool_use = prepared.tool_use
    registered = prepared.registered
    decision = prepared.decision

    if decision.outcome == "ask":
        request = ToolApprovalRequest(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            capability_type=registered.capability_type,
            source=registered.source,
            operation=tool_use.name,
            mode_name=prepared.mode_name,
            risk_level=registered.risk_level,
            reason=decision.reason or "Policy requested approval.",
            arguments=tool_use.arguments,
        )
        if not control.begin_tool_approval(request):
            return _error_result(
                tool_use,
                "Tool call was not approved because the Trace was stopped.",
                error_kind="cancelled",
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
                    reason="Trace stopped before tool approval completed.",
                )
            )
            return _error_result(
                tool_use,
                "Tool call was not approved because the Trace was stopped.",
                error_kind="cancelled",
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
            return _error_result(
                tool_use,
                denial_reason,
                error_kind="approval_denied",
            ), False
        state.resume_after_approval()
        events.publish(
            ToolApprovalResolvedEvent(
                tool_call_id=tool_use.id,
                tool_name=tool_use.name,
                outcome=approval.outcome,
            )
        )

    if registered.definition.name == QUESTION_TOOL_NAME:
        return await _execute_question_tool(
            prepared, events=events, hooks=hooks, hook_ctx=hook_ctx
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
            if is_revoked is not None and is_revoked(tool_use.name):
                result = _error_result(
                    tool_use, "Capability was revoked before execution.",
                    error_kind="capability_revoked",
                )
                break
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
            except RecoverableToolError as exc:
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
                    retry_started = False
                result = _error_result(
                    tool_use,
                    _recoverable_feedback(str(exc)),
                    error_kind="recoverable_failure",
                )
                break
            except RetryableToolError as exc:
                decision = budget.tool_retry(
                    tool=registered,
                    attempt=attempt,
                    error=exc,
                )
                will_retry = decision.action == "retry"
                if not will_retry:
                    if registered.retry_safe:
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
                            retry_started = False
                        result = _error_result(
                            tool_use,
                            _retry_exhausted_feedback(
                                budget.tool_retries.max_attempts
                            ),
                            error_kind="retry_exhausted",
                        )
                        break
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
    result, terminate = await hooks.after_tool(
        tool_use.id, tool_use.name, tool_use.arguments, result, hook_ctx
    )
    events.publish(
        ToolExecutionEndEvent(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            result=result.text,
            is_error=result.is_error,
        )
    )
    return result, terminate


async def _execute_question_tool(
    prepared: _PreparedToolCall,
    *,
    events: TraceEventStream,
    hooks: HookExecutor,
    hook_ctx: HookContext,
) -> tuple[ToolResultMessage, bool]:
    """向用户提问并等待回答（内置 ``question`` 工具），走 ctx.ui 阻塞通道。

    与普通工具一样经过 ``before_tool`` / ``after_tool``，但「执行」替换为阻塞 UI：
    有 ``options`` 时走 ``ui.select``，否则走 ``ui.input``。用户未回答（None / 超时 /
    Trace 停止）时以 ``user_cancelled`` 错误结果回喂模型，让模型继续推进。
    """
    tool_use = prepared.tool_use
    ui = hook_ctx.ui
    if ui is None:
        result = _error_result(
            tool_use,
            "This host does not support interactive questions (no blocking UI).",
            error_kind="no_blocking_ui",
        )
    else:
        question_text = tool_use.arguments.get("question")
        options = tool_use.arguments.get("options")
        if not isinstance(question_text, str) or not question_text.strip():
            result = _error_result(
                tool_use,
                "question must be a non-empty string.",
                error_kind="invalid_arguments",
            )
        elif options is not None and (
            not isinstance(options, list)
            or len(options) < 2
            or not all(isinstance(option, str) for option in options)
        ):
            result = _error_result(
                tool_use,
                "options must be a list of at least two strings when provided.",
                error_kind="invalid_arguments",
            )
        else:
            events.publish(
                ToolExecutionStartEvent(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    args=tool_use.arguments,
                )
            )
            answer = (
                await ui.select(question_text, options)
                if options is not None
                else await ui.input(question_text)
            )
            if answer is None:
                result = _error_result(
                    tool_use,
                    "The user did not provide an answer.",
                    error_kind="user_cancelled",
                )
            else:
                result = ToolResultMessage(
                    tool_call_id=tool_use.id,
                    tool_name=tool_use.name,
                    content=[TextContent(text=answer)],
                    is_error=False,
                    timestamp=time(),
                )
    result, terminate = await hooks.after_tool(
        tool_use.id, tool_use.name, tool_use.arguments, result, hook_ctx
    )
    events.publish(
        ToolExecutionEndEvent(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            result=result.text,
            is_error=result.is_error,
        )
    )
    return result, terminate


def _recoverable_feedback(message: str) -> str:
    """Warn that a reported failure does not prove no side effect occurred."""
    prefix = "Tool reported a recoverable failure: "
    suffix = (
        "\nThe call was not automatically repeated. Its side effects may be uncertain; "
        "verify the outcome before deciding whether to issue a new call."
    )
    available = _MAX_MODEL_TOOL_FEEDBACK_CHARS - len(prefix) - len(suffix)
    if len(message) > available:
        marker = "\n[Tool feedback truncated.]"
        message = message[: available - len(marker)] + marker
    return f"{prefix}{message}{suffix}"


def _retry_exhausted_feedback(max_attempts: int) -> str:
    """Summarize safe retry exhaustion without permitting further automatic attempts."""
    return (
        f"Tool retry limit reached after {max_attempts} safe attempt(s). "
        "The runtime withheld the executor error details. No more automatic attempts "
        "will run; verify the outcome before choosing another approach."
    )


def _error_result(
    tool_use: ToolUseContent,
    content: str,
    *,
    error_kind: str | None = None,
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tool_use.id,
        tool_name=tool_use.name,
        content=[TextContent(text=content)],
        is_error=True,
        timestamp=time(),
        metadata={} if error_kind is None else {"error_kind": error_kind},
    )
