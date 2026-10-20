"""sec Agent 的异步主协调循环。

本模块协调 Session 历史、模型生成、工具派发、运行控制和事件发布；
具体模型 attempt 与工具调用实现位于同级 Runtime 子模块。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from time import time
from uuid import uuid4

from sec._types.content import TextContent
from sec._types.events import (
    AgentEndEvent,
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantStartEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    ContentDeltaEvent,
    EmptySessionEvent,
    MemoryGateEvent,
    MemoryRetrievalEvent,
    MessageBoundaryEvent,
    MessageUpdateEvent,
    TurnEndEvent,
    CapabilitySnapshotEvent,
)
from sec._types.hooks import HookContext
from sec._types.messages import (
    AgentMessage,
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)
from sec._runtime.budget import Budget
from sec._runtime.capabilities import CapabilityBinding
from sec._runtime.control import TraceControl
from sec._runtime.event_stream import TraceEventStream
from sec._runtime.hooks import HookExecutor
from sec._runtime.metrics import TraceMetricsAccumulator
from sec._runtime.model_generation import complete_or_interrupt
from sec._runtime.state import TraceState
from sec._runtime.tool_dispatch import execute_tool_batch
from sec.agent import Agent
from sec.exceptions import (
    ContextBuildError,
    ContextWindowExceeded,
    ProviderProtocolError,
    TraceLimitExceeded,
)
from sec.memory.context_builder import ContextBuilder
from sec.memory.gate import MemoryGate
from sec.memory.retrieval import MemoryRetriever
from sec._types.memory import MemoryGateDecision, MemoryRetrievalResult
from sec.policies.rules import DenyAllPolicy
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.agent import Session
from sec._types.results import TraceResult


async def trace_loop(
    provider: ModelProvider | StreamingModelProvider,
    agent: Agent,
    session: Session,
    control: TraceControl,
    events: TraceEventStream,
    state: TraceState,
    policy: PolicyEngine | None = None,
    *,
    prompt: str,
    budget: Budget,
    metrics: TraceMetricsAccumulator,
    executor: ToolExecutor,
    hooks: HookExecutor,
    hook_ctx: HookContext,
    context_builder: ContextBuilder | None = None,
    capabilities: CapabilityBinding | None = None,
    gate: MemoryGate | None = None,
    retriever: MemoryRetriever | None = None,
    initial_messages: tuple[Message, ...] = (),
) -> TraceResult:
    """逐轮执行 Agent；每轮模型调用受预算限制，follow-up 会开启新预算。"""
    policy_engine = policy if policy is not None else DenyAllPolicy()
    for message in initial_messages:
        await session._append_message(message)
    start_index = len(session.messages)
    final_message: AssistantMessage | None = None
    turn_id = uuid4().hex
    turn_prompt = prompt
    turn_source = "user"
    if capabilities is not None:
        capabilities.context = (control, events)
        capabilities.metrics = metrics

    state.start()
    events.publish(EmptySessionEvent("agent_start"))

    async def append_user_message(text: str, source: str) -> None:
        message = UserMessage(
            content=[TextContent(text=text)],
            timestamp=time(),
            metadata={
                "turn_id": turn_id,
                "source": source,
            },
        )
        events.publish(MessageBoundaryEvent("message_start", message))
        await session._append_message(message)
        events.publish(MessageBoundaryEvent("message_end", message))

    while True:
        if capabilities is not None and capabilities.depth == 0:
            capabilities.allowance.model_calls = 0
            capabilities.allowance.tool_calls = 0
        events.publish(EmptySessionEvent("turn_start"))
        await append_user_message(turn_prompt, turn_source)
        turn_memory: MemoryRetrievalResult | None = None
        if gate is not None and retriever is not None:
            turn_memory = await _retrieve_memory(gate, retriever, turn_prompt, events)
        turn_model_calls = 0
        turn_tool_calls = 0
        turn_tool_results: list[ToolResultMessage] = []
        next_turn_prompt: str | None = None
        compacted_this_turn = False
        request_final = False

        while True:
            call_decision = budget.model_call(
                calls_used=turn_model_calls,
                calls_limit=agent.max_model_calls,
            )
            if call_decision.action == "stop":
                raise TraceLimitExceeded(
                    f"turn reached its limit of {agent.max_model_calls} model calls"
                )
            control.raise_if_stopped()
            for steering_text in _drain_steering(control):
                await append_user_message(steering_text, "steering")
            if capabilities is not None:
                if capabilities.refresh is not None:
                    await capabilities.refresh()
                task_text = "\n".join(
                    message.text for message in session.messages
                    if message.role == "user" and message.metadata.get("turn_id") == turn_id
                )
                agent = capabilities.resolve(task_text)
                events.publish(CapabilitySnapshotEvent(
                    generation=capabilities.generation,
                    agent_id=capabilities.agent_id,
                    card_digest=capabilities.card_digest,
                    loaded_skills=capabilities.active_skills,
                    tool_names=tuple(tool.definition.name for tool in agent.tools),
                ))

            prepared_context = None
            if context_builder is not None:
                prepared_context = context_builder.prepare(
                    system_prompt=agent.instructions,
                    session_messages=session.messages,
                    tools=tuple(tool.definition for tool in agent.tools),
                    request_final=request_final,
                    memories=() if turn_memory is None else turn_memory.memories,
                    episodes=() if turn_memory is None else turn_memory.episodes,
                    procedures=(
                        () if turn_memory is None else turn_memory.procedures
                    ),
                )
                context_decision = budget.context_pressure(
                    estimated_tokens=prepared_context.estimated_tokens,
                    context_window_tokens=context_builder.context_window_tokens,
                    compression_threshold=context_builder.compression_threshold,
                    max_output_tokens=context_builder.max_output_tokens,
                    model_calls_used=turn_model_calls,
                    model_calls_limit=agent.max_model_calls,
                    compacted=compacted_this_turn,
                )
                request_final = request_final or context_decision.request_final
                if context_decision.action == "compact":
                    compaction_reason = "threshold"
                    compaction_hook = await hooks.before_compaction(
                        compaction_reason, hook_ctx
                    )
                    if not compaction_hook.decline:
                        events.publish(
                            CompactionStartEvent(reason=compaction_reason)
                        )
                        try:
                            plan = context_builder.plan_compaction(
                                system_prompt=agent.instructions,
                                session_messages=session.messages,
                                tools=tuple(tool.definition for tool in agent.tools),
                            )
                            if compaction_hook.summary is None:
                                turn_model_calls += 1
                                if capabilities is not None:
                                    capabilities.allowance.model()
                                metrics.model_calls += 1
                                events.publish(MessageUpdateEvent(AssistantStartEvent()))
                                summary_completion, interrupted_steering = (
                                    await complete_or_interrupt(
                                        provider=provider,
                                        agent=agent,
                                        session=session,
                                        control=control,
                                        events=events,
                                        budget=budget,
                                        metrics=metrics,
                                        turn_id=turn_id,
                                        hooks=hooks,
                                        hook_ctx=hook_ctx,
                                        messages=plan.summary_messages,
                                        tools=(),
                                        publish_text=False,
                                    )
                                )
                            else:
                                summary_completion = None
                                interrupted_steering = None

                            if summary_completion is None and compaction_hook.summary is None:
                                events.publish(
                                    CompactionEndEvent(
                                        reason=compaction_reason,
                                        aborted=True,
                                        will_retry=False,
                                    )
                                )
                            else:
                                if compaction_hook.summary is None:
                                    summary_response = summary_completion
                                    if summary_response.role != "assistant":
                                        raise ProviderProtocolError(
                                            "context summary provider response must be an assistant message"
                                        )
                                    if (
                                        summary_response.tool_uses
                                        or not summary_response.text.strip()
                                    ):
                                        raise ContextBuildError(
                                            "context summary response must contain non-empty text "
                                            "and no tool calls"
                                        )
                                    summary_text = summary_response.text
                                else:
                                    summary_text = compaction_hook.summary
                                summary_message = ContextBuilder.create_summary_message(
                                    summary_text,
                                    covers_through=plan.covers_through,
                                )
                                events.publish(
                                    MessageBoundaryEvent(
                                        "message_start",
                                        summary_message,
                                    )
                                )
                                await session._append_message(summary_message)
                                compacted_this_turn = True
                                events.publish(
                                    MessageBoundaryEvent(
                                        "message_end",
                                        summary_message,
                                    )
                                )
                                events.publish(
                                    CompactionEndEvent(
                                        reason=compaction_reason,
                                        aborted=False,
                                        will_retry=False,
                                    )
                                )
                        except BaseException as exc:
                            events.publish(
                                CompactionEndEvent(
                                    reason=compaction_reason,
                                    aborted=True,
                                    will_retry=False,
                                    error_message=f"{type(exc).__name__}: {exc}",
                                )
                            )
                            raise
                        if summary_completion is None:
                            if interrupted_steering is not None:
                                await append_user_message(
                                    interrupted_steering,
                                    "steering",
                                )
                            continue
                        if interrupted_steering is not None:
                            await append_user_message(interrupted_steering, "steering")
                        continue
                if request_final:
                    prepared_context = context_builder.prepare(
                        system_prompt=agent.instructions,
                        session_messages=session.messages,
                        tools=tuple(tool.definition for tool in agent.tools),
                        request_final=True,
                        memories=() if turn_memory is None else turn_memory.memories,
                        episodes=() if turn_memory is None else turn_memory.episodes,
                        procedures=(
                            () if turn_memory is None else turn_memory.procedures
                        ),
                    )
                if request_final and prepared_context.estimated_tokens > (
                    context_builder.input_token_limit
                ):
                    raise ContextWindowExceeded(
                        "final response context exceeds the configured input token limit"
                    )

            turn_model_calls += 1
            if capabilities is not None:
                capabilities.allowance.model()
            metrics.model_calls += 1
            events.publish(MessageUpdateEvent(AssistantStartEvent()))
            try:
                completion, interrupted_steering = await complete_or_interrupt(
                    provider=provider,
                    agent=agent,
                    session=session,
                    control=control,
                    events=events,
                    budget=budget,
                    metrics=metrics,
                    turn_id=turn_id,
                    hooks=hooks,
                    hook_ctx=hook_ctx,
                    messages=(
                        None
                        if prepared_context is None
                        else prepared_context.messages
                    ),
                    tools=tuple(tool.definition for tool in agent.tools),
                )
            except Exception:
                raise

            if completion is None:
                if interrupted_steering is not None:
                    await append_user_message(interrupted_steering, "steering")
                continue
            response = completion
            if not isinstance(response, AssistantMessage):
                raise ProviderProtocolError(
                    "model provider must return an assistant message"
                )

            committed_response = replace(
                response,
                metadata={
                    **response.metadata,
                    "turn_id": turn_id,
                    **(
                        {} if capabilities is None else {
                            "agent_id": capabilities.agent_id,
                            "card_digest": capabilities.card_digest,
                            "capability_generation": capabilities.generation,
                        }
                    ),
                },
            )
            events.publish(
                MessageBoundaryEvent("message_start", committed_response)
            )
            await session._append_message(committed_response)
            final_message = committed_response
            if committed_response.stop_reason in (
                "stop",
                "length",
                "tool_use",
                "deferred",
            ):
                events.publish(
                    MessageUpdateEvent(
                        AssistantDoneEvent(
                            reason=committed_response.stop_reason,
                            message=committed_response,
                        ),
                        usage=committed_response.usage,
                    )
                )
            elif committed_response.stop_reason in ("error", "aborted"):
                events.publish(
                    MessageUpdateEvent(
                        AssistantErrorEvent(
                            reason=committed_response.stop_reason,
                            error=committed_response,
                        ),
                        usage=committed_response.usage,
                    )
                )
            else:
                raise ProviderProtocolError(
                    "model response has an unsupported stop reason"
                )
            events.publish(
                MessageBoundaryEvent("message_end", committed_response)
            )

            if committed_response.tool_uses:
                tool_results, requested_tools, failure, stopped = (
                    await execute_tool_batch(
                        committed_response.tool_uses,
                        tools_by_name={
                            registered.definition.name: registered
                            for registered in agent.tools
                        },
                        agent_name=agent.name,
                        policy=policy_engine,
                        executor=executor,
                        control=control,
                        state=state,
                        remaining_tool_calls=agent.max_tool_calls - turn_tool_calls,
                        events=events,
                        turn_id=turn_id,
                        budget=budget,
                        metrics=metrics,
                        hooks=hooks,
                        hook_ctx=hook_ctx,
                        is_revoked=(
                            None if capabilities is None else capabilities.check_revoked
                        ),
                        consume_tool=(
                            None if capabilities is None
                            else lambda: capabilities.allowance.tools(1)
                        ),
                    )
                )
                turn_tool_calls += requested_tools
                metrics.tool_calls += requested_tools
                for result in tool_results:
                    result.metadata["turn_id"] = turn_id
                await session._append_messages(tool_results)
                turn_tool_results.extend(tool_results)
                if failure is not None:
                    raise failure
                if stopped:
                    raise asyncio.CancelledError
                if interrupted_steering is not None:
                    await append_user_message(interrupted_steering, "steering")
                continue

            if interrupted_steering is not None:
                await append_user_message(interrupted_steering, "steering")
                continue

            follow_ups = await hooks.before_trace_end(
                tuple(session.messages[start_index:]), hook_ctx
            )
            for follow_up_text in follow_ups:
                control.follow_up(follow_up_text)

            resume_turn = False
            while True:
                control.raise_if_stopped()
                queued_steering = _drain_steering(control)
                if queued_steering:
                    for steering_text in queued_steering:
                        await append_user_message(steering_text, "steering")
                    resume_turn = True
                    break

                next_turn_prompt = control.take_follow_up_nowait()
                if next_turn_prompt is not None:
                    if final_message is None:
                        raise ProviderProtocolError(
                            "turn completed without an assistant response"
                        )
                    events.publish(TurnEndEvent(final_message, turn_tool_results))
                    break
                if control.close_if_idle():
                    if final_message is None:
                        raise ProviderProtocolError(
                            "run finished without an assistant response"
                        )
                    events.publish(TurnEndEvent(final_message, turn_tool_results))
                    result = TraceResult(
                        final_message=final_message,
                        messages=session.messages[start_index:],
                        model_calls=metrics.model_calls,
                        tool_calls=metrics.tool_calls,
                        model_attempts=metrics.model_attempts,
                        retries=metrics.retries,
                        tool_attempts=metrics.tool_attempts,
                        tool_retries=metrics.tool_retries,
                        usage=metrics.usage(),
                    )
                    agent_messages: list[AgentMessage] = list(result.messages)
                    events.publish(AgentEndEvent(agent_messages, will_retry=False))
                    return result
                await asyncio.sleep(0)

            if resume_turn:
                continue
            if next_turn_prompt is not None:
                break
        if next_turn_prompt is None:
            raise ProviderProtocolError("loop exited without a follow-up turn")
        turn_id = uuid4().hex
        turn_prompt = next_turn_prompt
        turn_source = "follow_up"


def _drain_steering(control: TraceControl) -> list[str]:
    messages: list[str] = []
    while True:
        text = control.take_steering_nowait()
        if text is None:
            return messages
        messages.append(text)


async def _retrieve_memory(
    gate: MemoryGate,
    retriever: MemoryRetriever,
    prompt: str,
    events: TraceEventStream,
) -> MemoryRetrievalResult | None:
    """运行 MemGate 并按需检索；失败按 fail-open 处理且始终发布事件。"""
    try:
        decision = await gate.decide(prompt)
    except Exception as exc:
        decision = MemoryGateDecision(
            retrieve=True,
            query=prompt,
            error=f"{type(exc).__name__}: {exc}",
        )
    events.publish(
        MemoryGateEvent(
            status="failed" if decision.error is not None else "ok",
            retrieve=decision.retrieve,
            query=decision.query,
            error=decision.error,
        )
    )
    if not decision.retrieve:
        return None
    try:
        result = await retriever.retrieve(decision.query or prompt)
    except Exception as exc:
        result = MemoryRetrievalResult(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    events.publish(
        MemoryRetrievalEvent(
            status=result.status,
            memory_count=len(result.memories),
            episode_count=len(result.episodes),
            procedure_count=len(result.procedures),
            error=result.error,
        )
    )
    return result
