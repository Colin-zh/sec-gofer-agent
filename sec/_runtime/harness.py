"""装配 Provider、策略和 Session，并启动 sec Run。

Harness 为每次运行创建控制对象与事件流，将执行委托给内部循环，并负责
汇总计量以及发布成功、失败、预算耗尽或取消等终态事件。
"""

from __future__ import annotations

import asyncio
import inspect
from uuid import uuid4
from typing import Any

from sec._runtime.budget import Budget
from sec._runtime.capabilities import CapabilityBinding
from sec._runtime.control import RunControl
from sec._runtime.loop import run_loop
from sec._runtime.event_stream import (
    DEFAULT_EVENT_BUFFER_SIZE,
    RunEventStream,
)
from sec._runtime.metrics import RunMetricsAccumulator
from sec._runtime.state import RunState, RunTerminalStatus
from sec._types.events import AgentSettledEvent
from sec._types.extensions import (
    AfterRunHook,
    BeforeRunHook,
    RunEventHook,
    RunHookContext,
    RunHookOutcome,
)
from sec.agent import Agent, Run, Session
from sec.config import ConfigError, ContextSettings, RetrySettings, ToolRetrySettings
from sec.exceptions import RunLimitExceeded
from sec.executors import LocalToolExecutor
from sec._types.context import TokenEstimator
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.memory.context_builder import ContextBuilder
from sec._types.results import RunResult


class Harness:
    """装配模型 Provider 并启动由 Session 管理的 Run。"""

    def __init__(
        self,
        provider: ModelProvider | StreamingModelProvider,
        *,
        policy: PolicyEngine | None = None,
        executor: ToolExecutor | None = None,
        event_buffer_size: int = DEFAULT_EVENT_BUFFER_SIZE,
        retry_settings: RetrySettings | None = None,
        tool_retry_settings: ToolRetrySettings | None = None,
        context_settings: ContextSettings | None = None,
        token_estimator: TokenEstimator | None = None,
        max_output_tokens: int = 4096,
        before_run_hooks: tuple[BeforeRunHook, ...] = (),
        after_run_hooks: tuple[AfterRunHook, ...] = (),
        run_event_hooks: tuple[RunEventHook, ...] = (),
    ) -> None:
        if event_buffer_size < 1:
            raise ValueError("event_buffer_size must be at least 1")
        self._provider = provider
        self._before_run_hooks = before_run_hooks
        self._after_run_hooks = after_run_hooks
        self._run_event_hooks = run_event_hooks
        if policy is not None and not callable(getattr(policy, "decide", None)):
            raise TypeError("policy must implement decide()")
        self._policy = policy
        self._executor = executor if executor is not None else LocalToolExecutor()
        self._event_buffer_size = event_buffer_size
        self._retry_settings = (
            retry_settings if retry_settings is not None else RetrySettings()
        )
        self._tool_retry_settings = (
            tool_retry_settings
            if tool_retry_settings is not None
            else ToolRetrySettings()
        )
        settings = context_settings if context_settings is not None else ContextSettings()
        if (settings.context_window_tokens is None) != (token_estimator is None):
            raise ConfigError(
                "context compression requires both context_window_tokens and "
                "a TokenEstimator"
            )
        if settings.context_window_tokens is None:
            self._context_builder = None
        else:
            try:
                self._context_builder = ContextBuilder(
                    token_estimator,
                    context_window_tokens=settings.context_window_tokens,
                    max_output_tokens=max_output_tokens,
                    compression_threshold=settings.compression_threshold,
                )
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid context compression configuration: {exc}") from exc

    async def aclose(self) -> None:
        """释放 Provider 的异步客户端资源；无关闭接口时不执行操作。"""
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()

    def run(
        self,
        agent: Agent,
        prompt: str,
        *,
        session: Session | None = None,
        capabilities: CapabilityBinding | None = None,
    ) -> Run:
        """在当前事件循环中启动 Run，并返回可观察、可控制的句柄。"""
        loop = asyncio.get_running_loop()
        active_session = session if session is not None else Session()
        event_stream = RunEventStream(
            run_id=uuid4().hex,
            capacity=self._event_buffer_size,
            event_hooks=self._run_event_hooks,
        )
        metrics = RunMetricsAccumulator()
        budget = Budget(
            model_retries=self._retry_settings,
            tool_retries=self._tool_retry_settings,
        )
        state = RunState()
        control = active_session._begin_run()
        try:
            task = loop.create_task(
                self._execute(
                    agent,
                    prompt,
                    active_session,
                    control,
                    event_stream,
                    metrics,
                    budget,
                    state,
                    self._before_run_hooks,
                    self._after_run_hooks,
                    capabilities,
                ),
                name=f"agent-run-{active_session.id}",
            )
        except BaseException:
            active_session._finish_run(control)
            raise
        return Run(task, active_session, control, event_stream)

    async def _execute(
        self,
        agent: Agent,
        prompt: str,
        session: Session,
        control: RunControl,
        event_stream: RunEventStream,
        metrics: RunMetricsAccumulator,
        budget: Budget,
        state: RunState,
        before_run_hooks: tuple[BeforeRunHook, ...],
        after_run_hooks: tuple[AfterRunHook, ...],
        capabilities: CapabilityBinding | None = None,
    ) -> RunResult:
        """执行 Agent 循环，并在所有退出路径发布 Run 终态事件。"""
        try:
            hook_context = RunHookContext(
                run_id=event_stream.run_id,
                agent_name=agent.name,
                session_id=session.id,
                prompt=prompt,
            )
            result: RunResult | None = None
            error: BaseException | None = None
            status: RunTerminalStatus
            try:
                if capabilities is not None and capabilities.initialize is not None:
                    await capabilities.initialize()
                for hook in before_run_hooks:
                    await _call_hook(hook, hook_context)
                result = await run_loop(
                    provider=self._provider,
                    agent=agent,
                    session=session,
                    control=control,
                    events=event_stream,
                    state=state,
                    policy=self._policy,
                    executor=self._executor,
                    budget=budget,
                    metrics=metrics,
                    prompt=prompt,
                    context_builder=self._context_builder,
                    capabilities=capabilities,
                )
                hook_errors = event_stream.take_hook_errors()
                if hook_errors:
                    raise ExceptionGroup("Run event hooks failed", list(hook_errors))
                status = "completed"
            except asyncio.CancelledError as exc:
                hook_errors = event_stream.take_hook_errors()
                error = (
                    BaseExceptionGroup(
                        "Run was cancelled and event hooks failed",
                        [exc, *hook_errors],
                    )
                    if hook_errors
                    else exc
                )
                status = "cancelled"
            except Exception as exc:
                hook_errors = event_stream.take_hook_errors()
                error = (
                    ExceptionGroup(
                        "Run and event hooks failed",
                        [exc, *hook_errors],
                    )
                    if hook_errors
                    else exc
                )
                status = (
                    "limit_reached"
                    if isinstance(exc, RunLimitExceeded)
                    else "failed"
                )
            outcome = RunHookOutcome(
                context=hook_context,
                result=result if error is None else None,
                error=error,
                status=status,
            )
            hook_errors: list[Exception] = []
            for hook in after_run_hooks:
                try:
                    await _call_hook(hook, outcome)
                except Exception as exc:
                    hook_errors.append(exc)
            if capabilities is not None and capabilities.finalize is not None:
                try:
                    await capabilities.finalize()
                except Exception as exc:
                    hook_errors.append(exc)
            if hook_errors:
                if error is None:
                    error = ExceptionGroup("after-run hooks failed", hook_errors)
                    status = "failed"
                    result = None
                else:
                    error = BaseExceptionGroup(
                        "Run and after-run hooks failed",
                        [error, *hook_errors],
                    )
                    status = "failed" if status != "cancelled" else status
            state.finish(status)
            event_stream.publish(
                AgentSettledEvent(status=status),
                notify_hooks=False,
            )
            if error is not None:
                raise error
            if result is None:
                raise RuntimeError("Run completed without a result")
            return result
        finally:
            event_stream.close()
            session._finish_run(control)


async def _call_hook(hook: Any, context: Any) -> None:
    """Invoke a sync or async lifecycle callback without suppressing failures."""
    result = hook(context)
    if inspect.isawaitable(result):
        await result
