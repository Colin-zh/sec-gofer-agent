"""装配 Provider、策略和 Session，并启动 sec Trace。

Harness 为每次运行创建控制对象与事件流，将执行委托给内部循环，并负责
汇总计量以及发布成功、失败、预算耗尽或取消等终态事件。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from uuid import uuid4

from sec._runtime.budget import Budget
from sec._runtime.capabilities import CapabilityBinding
from sec._runtime.control import TraceControl
from sec._runtime.hooks import HookExecutor
from sec._runtime.loop import trace_loop
from sec._runtime.ui import HostBlockingUI
from sec._runtime.event_stream import (
    DEFAULT_EVENT_BUFFER_SIZE,
    TraceEventStream,
)
from sec._runtime.metrics import TraceMetricsAccumulator
from sec._runtime.state import TraceState, TraceTerminalStatus
from sec._types.events import (
    AgentSettledEvent,
    MemoryConsolidationEvent,
    MemoryIndexEvent,
    MemoryProceduralConsolidationEvent,
    TraceSummaryEvent,
)
from sec._types.hooks import HookContext, HookHandler, HookName
from sec.agent import Agent, Trace, Session
from sec.config import ConfigError, ContextSettings, RetrySettings, ToolRetrySettings
from sec.exceptions import TraceLimitExceeded
from sec.executors import LocalToolExecutor
from sec._types.context import TokenEstimator
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.providers.router import ModelRouter
from sec.agent.auxiliary import AuxiliaryRuntime
from sec.memory.context_builder import ContextBuilder
from sec._types.memory import (
    EpisodicKind,
    EpisodicRecord,
    MemoryConsolidationResult,
    ProceduralConsolidationResult,
)
from sec._types.messages import AssistantMessage, Message, ToolResultMessage
from sec._types.results import TraceResult
from sec._types.session import MessageEntry
from sec.memory.runtime import MemoryRuntime


class Harness:
    """装配模型 Provider 并启动由 Session 管理的 Trace。"""

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
        hooks: Mapping[HookName, tuple[HookHandler, ...]] | None = None,
        memory: MemoryRuntime | None = None,
        router: ModelRouter | None = None,
        auxiliary: AuxiliaryRuntime | None = None,
    ) -> None:
        if event_buffer_size < 1:
            raise ValueError("event_buffer_size must be at least 1")
        self._provider = provider
        self._router = router
        self._auxiliary = auxiliary
        self._memory = memory
        self._hook_executor = HookExecutor(hooks if hooks is not None else {})
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
        if self._router is not None:
            await self._router.aclose()
            return
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()

    async def _post_run_memory(
        self,
        session: Session,
        result: TraceResult,
        events: TraceEventStream,
    ) -> None:
        """post-run 阶段建 episodic 索引并抽取语义事实；失败仅发布事件。"""
        memory = self._memory
        if memory is None:
            return
        if memory.episodic is not None:
            entry_ids = {
                id(entry.message): entry.id
                for entry in session.entries
                if isinstance(entry, MessageEntry)
            }
            records = _build_episodic_records(
                session.id,
                result.messages,
                entry_ids=entry_ids,
            )
            if records:
                try:
                    await memory.episodic.index(records)
                    events.publish(
                        MemoryIndexEvent(status="ok", indexed=len(records))
                    )
                except Exception as exc:
                    events.publish(
                        MemoryIndexEvent(
                            status="failed",
                            indexed=0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
            else:
                events.publish(MemoryIndexEvent(status="empty", indexed=0))
        if memory.consolidator is not None:
            try:
                consolidation = await memory.consolidator.consolidate(
                    result.messages, session_id=session.id
                )
            except Exception as exc:
                consolidation = MemoryConsolidationResult(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            events.publish(
                MemoryConsolidationEvent(
                    status=consolidation.status,
                    added_ids=consolidation.added_ids,
                    error=consolidation.error,
                )
            )
        if memory.procedural_consolidator is not None:
            try:
                procedural = await memory.procedural_consolidator.consolidate(
                    result.messages
                )
            except Exception as exc:
                procedural = ProceduralConsolidationResult(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            events.publish(
                MemoryProceduralConsolidationEvent(
                    status=procedural.status,
                    added_count=len(procedural.added),
                    error=procedural.error,
                )
            )

    async def _post_run_summary(
        self,
        result: TraceResult,
        events: TraceEventStream,
    ) -> None:
        """post-run 阶段生成 trace 一句话摘要；失败仅发布事件，不改变结果。"""
        auxiliary = self._auxiliary
        if (
            auxiliary is None
            or not auxiliary.active
            or auxiliary.summarizer is None
        ):
            return
        try:
            summary = await auxiliary.summarizer.summarize(result.messages)
            events.publish(TraceSummaryEvent(summary=summary))
        except Exception as exc:
            events.publish(
                TraceSummaryEvent(
                    summary="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    def run(
        self,
        agent: Agent,
        prompt: str,
        *,
        session: Session | None = None,
        capabilities: CapabilityBinding | None = None,
        policy: PolicyEngine | None = None,
        resume_existing_turn: bool = False,
    ) -> Trace:
        """在当前事件循环中启动 Trace，并返回可观察、可控制的句柄。

        ``policy`` 允许单次运行覆盖 Harness 的默认策略（如定时任务的全放行）。
        """
        loop = asyncio.get_running_loop()
        active_session = session if session is not None else Session()
        event_stream = TraceEventStream(
            trace_id=uuid4().hex,
            capacity=self._event_buffer_size,
        )
        metrics = TraceMetricsAccumulator()
        budget = Budget(
            model_retries=self._retry_settings,
            tool_retries=self._tool_retry_settings,
        )
        state = TraceState()
        control = active_session._begin_trace()
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
                    capabilities,
                    policy,
                    resume_existing_turn,
                ),
                name=f"agent-run-{active_session.id}",
            )
        except BaseException:
            active_session._finish_trace(control)
            raise
        return Trace(task, active_session, control, event_stream)

    async def _execute(
        self,
        agent: Agent,
        prompt: str,
        session: Session,
        control: TraceControl,
        event_stream: TraceEventStream,
        metrics: TraceMetricsAccumulator,
        budget: Budget,
        state: TraceState,
        capabilities: CapabilityBinding | None = None,
        policy: PolicyEngine | None = None,
        resume_existing_turn: bool = False,
    ) -> TraceResult:
        """执行 Agent 循环，并在所有退出路径发布 Trace 终态事件。"""
        try:
            hook_ctx = HookContext(
                trace_id=event_stream.trace_id,
                session_id=session.id,
                agent_name=agent.name,
                ui=HostBlockingUI(control, event_stream, agent.name),
            )
            result: TraceResult | None = None
            error: BaseException | None = None
            status: TraceTerminalStatus
            try:
                if capabilities is not None and capabilities.initialize is not None:
                    await capabilities.initialize()
                injected = await self._hook_executor.before_trace(prompt, hook_ctx)
                result = await trace_loop(
                    provider=self._provider,
                    agent=agent,
                    session=session,
                    control=control,
                    events=event_stream,
                    state=state,
                    policy=policy if policy is not None else self._policy,
                    executor=self._executor,
                    budget=budget,
                    metrics=metrics,
                    prompt=prompt,
                    hooks=self._hook_executor,
                    hook_ctx=hook_ctx,
                    context_builder=self._context_builder,
                    capabilities=capabilities,
                    gate=self._memory.gate if self._memory is not None else None,
                    retriever=self._memory.retriever if self._memory is not None else None,
                    initial_messages=injected,
                    router=self._router,
                    auxiliary=self._auxiliary,
                    resume_existing_turn=resume_existing_turn,
                )
                status = "completed"
                if self._memory is not None and self._memory.has_post_run:
                    try:
                        await self._post_run_memory(session, result, event_stream)
                    except Exception:
                        # post-run 记忆整理失败不改变已完成的 Trace 结果。
                        pass
                if self._auxiliary is not None and self._auxiliary.active:
                    try:
                        await self._post_run_summary(result, event_stream)
                    except Exception:
                        # post-run 摘要失败不改变已完成的 Trace 结果。
                        pass
            except asyncio.CancelledError as exc:
                error = exc
                status = "cancelled"
            except Exception as exc:
                error = exc
                status = (
                    "limit_reached"
                    if isinstance(exc, TraceLimitExceeded)
                    else "failed"
                )
            finalize_errors: list[Exception] = []
            if capabilities is not None and capabilities.finalize is not None:
                try:
                    await capabilities.finalize()
                except Exception as exc:
                    finalize_errors.append(exc)
            if finalize_errors:
                if error is None:
                    error = ExceptionGroup("run finalization failed", finalize_errors)
                    status = "failed"
                    result = None
                else:
                    error = BaseExceptionGroup(
                        "Trace and finalization failed",
                        [error, *finalize_errors],
                    )
                    status = "failed" if status != "cancelled" else status
            state.finish(status)
            event_stream.publish(AgentSettledEvent(status=status))
            if error is not None:
                raise error
            if result is None:
                raise RuntimeError("Trace completed without a result")
            return result
        finally:
            event_stream.close()
            session._finish_trace(control)


_MAX_TOOL_TEXT = 2000


def _build_episodic_records(
    session_id: str,
    messages: Sequence[Message],
    *,
    entry_ids: Mapping[int, str] | None = None,
) -> list[EpisodicRecord]:
    """把本次 Trace 新增的对话消息与工具活动转成情景索引记录。

    - user/assistant 消息 → kind="message"；
    - 助手消息内的工具调用 → kind="tool_call"（含工具名与参数）；
    - 工具执行结果 → kind="tool_result"（含工具名、结果文本与错误标记）。

    文件改动不单独立类：write/edit 等工具调用的路径已出现在工具调用参数与
    结果文本中，可被同一关键字检索命中。
    """
    records: list[EpisodicRecord] = []
    for message_index, message in enumerate(messages):
        entry_id = (entry_ids or {}).get(
            id(message),
            f"{session_id}:{message_index}",
        )
        if isinstance(message, AssistantMessage):
            text = message.text.strip()
            if text:
                records.append(
                    _episode(session_id, entry_id, "message", text, message.timestamp)
                )
            for tool_use in message.tool_uses:
                records.append(
                    _episode(
                        session_id,
                        entry_id,
                        "tool_call",
                        _tool_call_text(tool_use.name, tool_use.arguments),
                        message.timestamp,
                    )
                )
            continue
        if isinstance(message, ToolResultMessage):
            text = message.text.strip()
            if text:
                records.append(
                    _episode(
                        session_id,
                        entry_id,
                        "tool_result",
                        _tool_result_text(message.tool_name, text, message.is_error),
                        message.timestamp,
                    )
                )
            continue
        text = message.text.strip()
        if text and message.role in ("user", "assistant"):
            records.append(
                _episode(session_id, entry_id, "message", text, message.timestamp)
            )
    return records


def _episode(
    session_id: str,
    entry_id: str,
    kind: EpisodicKind,
    text: str,
    timestamp: float,
) -> EpisodicRecord:
    """构造幂等情景索引；同一来源重复整理会得到相同 ID。"""
    identity = f"{session_id}\0{entry_id}\0{kind}\0{timestamp!r}\0{text}"
    return EpisodicRecord(
        id=f"epi:{session_id}:{sha256(identity.encode('utf-8')).hexdigest()}",
        session_id=session_id,
        entry_id=entry_id,
        text=text,
        timestamp=timestamp,
        kind=kind,
    )


def _tool_call_text(name: str, arguments: object) -> str:
    try:
        args_text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        args_text = str(arguments)
    return _truncate(f"tool call: {name} {args_text}")


def _tool_result_text(name: str, text: str, is_error: bool) -> str:
    suffix = " [error]" if is_error else ""
    return _truncate(f"tool result: {name}: {text}{suffix}")


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TOOL_TEXT:
        return text
    return text[:_MAX_TOOL_TEXT] + "…"
