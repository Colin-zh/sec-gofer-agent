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
    MessageBoundaryEvent,
    MessageUpdateEvent,
    TurnEndEvent,
)
from sec._types.messages import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from sec._runtime.budget import Budget
from sec._runtime.control import RunControl
from sec._runtime.event_stream import RunEventStream
from sec._runtime.metrics import RunMetricsAccumulator
from sec._runtime.model_generation import complete_or_interrupt
from sec._runtime.state import RunState
from sec._runtime.tool_dispatch import execute_tool_batch
from sec.agent import Agent
from sec.exceptions import (
    ContextBuildError,
    ContextWindowExceeded,
    ProviderProtocolError,
    RunLimitExceeded,
)
from sec.memory.context_builder import ContextBuilder
from sec.policies.rules import DenyAllPolicy
from sec._types.execution import ToolExecutor
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.agent import Session
from sec._types.results import RunResult


async def run_loop(
    provider: ModelProvider | StreamingModelProvider,
    agent: Agent,
    session: Session,
    control: RunControl,
    events: RunEventStream,
    state: RunState,
    policy: PolicyEngine | None = None,
    *,
    prompt: str,
    budget: Budget,
    metrics: RunMetricsAccumulator,
    executor: ToolExecutor,
    context_builder: ContextBuilder | None = None,
) -> RunResult:
    """逐轮执行 Agent；每轮模型调用受预算限制，follow-up 会开启新预算。"""
    policy_engine = policy if policy is not None else DenyAllPolicy()
    start_index = len(session.messages)
    final_message: AssistantMessage | None = None
    turn_id = uuid4().hex
    turn_prompt = prompt
    turn_source = "user"

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
        events.publish(EmptySessionEvent("turn_start"))
        await append_user_message(turn_prompt, turn_source)
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
                raise RunLimitExceeded(
                    f"turn reached its limit of {agent.max_model_calls} model calls"
                )
            control.raise_if_stopped()
            for steering_text in _drain_steering(control):
                await append_user_message(steering_text, "steering")

            prepared_context = None
            if context_builder is not None:
                prepared_context = context_builder.prepare(
                    system_prompt=agent.instructions,
                    session_messages=session.messages,
                    tools=tuple(tool.definition for tool in agent.tools),
                    request_final=request_final,
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
                    events.publish(
                        CompactionStartEvent(reason=compaction_reason)
                    )
                    try:
                        plan = context_builder.plan_compaction(
                            system_prompt=agent.instructions,
                            session_messages=session.messages,
                            tools=tuple(tool.definition for tool in agent.tools),
                        )
                        turn_model_calls += 1
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
                                messages=plan.summary_messages,
                                tools=(),
                                publish_text=False,
                            )
                        )
                        if summary_completion is None:
                            events.publish(
                                CompactionEndEvent(
                                    reason=compaction_reason,
                                    aborted=True,
                                    will_retry=False,
                                )
                            )
                        else:
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
                            summary_message = ContextBuilder.create_summary_message(
                                summary_response.text,
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
                    )
                if request_final and prepared_context.estimated_tokens > (
                    context_builder.input_token_limit
                ):
                    raise ContextWindowExceeded(
                        "final response context exceeds the configured input token limit"
                    )

            turn_model_calls += 1
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
                    result = RunResult(
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


def _drain_steering(control: RunControl) -> list[str]:
    messages: list[str] = []
    while True:
        text = control.take_steering_nowait()
        if text is None:
            return messages
        messages.append(text)
