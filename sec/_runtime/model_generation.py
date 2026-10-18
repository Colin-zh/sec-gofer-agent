"""处理一次模型生成的流式响应、重试、取消和 steering 中断。"""

from __future__ import annotations

import asyncio
from time import time
from typing import Any

from sec._types.content import TextContent
from sec._types.messages import AssistantMessage, Message, SystemMessage
from sec._types.events import (
    AutoRetryEndEvent,
    ContentDeltaEvent,
    MessageUpdateEvent,
    RetryStartEvent,
)
from sec._runtime.budget import Budget
from sec._runtime.control import RunControl
from sec._runtime.event_stream import RunEventStream
from sec._runtime.metrics import RunMetricsAccumulator
from sec.agent import Agent
from sec.exceptions import ProviderProtocolError, RetryableProviderError
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.agent import Session
from sec.tools.tool import ToolDefinition
from sec._types.streaming import ModelResponseCompleted, ModelTextDelta


async def complete_or_interrupt(
    *,
    provider: ModelProvider | StreamingModelProvider,
    agent: Agent,
    session: Session,
    control: RunControl,
    events: RunEventStream,
    budget: Budget,
    metrics: RunMetricsAccumulator,
    messages: tuple[Message, ...] | None = None,
    tools: tuple[ToolDefinition, ...] | None = None,
    publish_text: bool = True,
) -> tuple[AssistantMessage | None, str | None]:
    """等待模型响应，或因 steering/取消中断尚未提交的生成。"""
    control.raise_if_stopped()

    request_messages = messages
    if request_messages is None:
        system_message = SystemMessage(
            content=[TextContent(text=agent.instructions)],
            timestamp=time(),
        )
        request_messages = (system_message, *session.messages)
    request_tools = (
        tuple(tool.definition for tool in agent.tools)
        if tools is None
        else tools
    )
    completion = _complete_with_retries(
        provider=provider,
        messages=request_messages,
        tools=request_tools,
        events=events,
        budget=budget,
        metrics=metrics,
        publish_text=publish_text,
    )
    completion_task = asyncio.create_task(
        completion,
        name="model-generation",
    )
    steering_task = asyncio.create_task(control.wait_for_steering())
    stop_task = asyncio.create_task(control.wait_for_stop())

    try:
        completed, _ = await asyncio.wait(
            {completion_task, steering_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if completion_task in completed:
            model_completion = await completion_task
            steering_text = (
                steering_task.result() if steering_task in completed else None
            )
            return model_completion, steering_text

        if stop_task in completed:
            late_response = await _cancel_completion(completion_task)
            if late_response is not None:
                steering_text = (
                    steering_task.result() if steering_task in completed else None
                )
                return late_response, steering_text
            raise asyncio.CancelledError

        steering_text = steering_task.result()
        late_response = await _cancel_completion(completion_task)
        if late_response is not None:
            return late_response, steering_text
        return None, steering_text
    finally:
        await _cancel_waiter(steering_task)
        await _cancel_waiter(stop_task)
        if not completion_task.done():
            await _cancel_completion(completion_task)
        else:
            await asyncio.gather(completion_task, return_exceptions=True)


async def _complete_with_retries(
    *,
    provider: ModelProvider | StreamingModelProvider,
    messages: tuple[Message, ...],
    tools: tuple[ToolDefinition, ...],
    events: RunEventStream,
    budget: Budget,
    metrics: RunMetricsAccumulator,
    publish_text: bool,
) -> AssistantMessage:
    """对单个逻辑模型请求执行有限 attempt，并隔离流式增量。"""
    retry_started = False
    for attempt_number in range(1, budget.model_retries.max_attempts + 1):
        metrics.start_model_attempt(retry=attempt_number > 1)
        try:
            if isinstance(provider, StreamingModelProvider):
                response = await _collect_streamed_response(
                    provider=provider,
                    messages=messages,
                    tools=tools,
                    events=events,
                    publish_text=publish_text,
                )
            else:
                response = await provider.complete(messages, tools)
        except RetryableProviderError as exc:
            metrics.record_failed_attempt()
            decision = budget.model_retry(
                attempt=attempt_number,
                error=exc,
            )
            will_retry = decision.action == "retry"
            delay_seconds = decision.retry_delay_seconds
            if not will_retry:
                if retry_started:
                    events.publish(
                        AutoRetryEndEvent(
                            success=False,
                            attempt=attempt_number,
                            final_error=str(exc),
                        )
                    )
                raise
            events.publish(
                RetryStartEvent(
                    type="auto_retry_start",
                    attempt=attempt_number,
                    max_attempts=budget.model_retries.max_attempts,
                    delay_ms=delay_seconds * 1000,
                    error_message=str(exc),
                )
            )
            retry_started = True
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                events.publish(
                    AutoRetryEndEvent(
                        success=False,
                        attempt=attempt_number,
                        final_error="cancelled",
                    )
                )
                raise
        except asyncio.CancelledError:
            metrics.record_failed_attempt()
            if retry_started:
                events.publish(
                    AutoRetryEndEvent(
                        success=False,
                        attempt=attempt_number,
                        final_error="cancelled",
                    )
                )
            raise
        except Exception as exc:
            metrics.record_failed_attempt()
            if retry_started:
                events.publish(
                    AutoRetryEndEvent(
                        success=False,
                        attempt=attempt_number,
                        final_error=str(exc),
                    )
                )
            raise
        else:
            metrics.record_response(response)
            if retry_started:
                events.publish(
                    AutoRetryEndEvent(success=True, attempt=attempt_number)
                )
            return response

    raise AssertionError("model retry loop exited without a result")


async def _collect_streamed_response(
    *,
    provider: StreamingModelProvider,
    messages: tuple[Message, ...],
    tools: tuple[ToolDefinition, ...],
    events: RunEventStream,
    publish_text: bool,
) -> AssistantMessage:
    """发布文本增量并收集最终完整消息；增量本身不写入 Session。"""
    text_parts: list[str] = []
    response: AssistantMessage | None = None
    async for item in provider.stream(messages, tools):
        if response is not None:
            raise ProviderProtocolError(
                "model stream emitted an event after its completed response"
            )
        if isinstance(item, ModelTextDelta):
            if item.text:
                text_parts.append(item.text)
                if publish_text:
                    events.publish(
                        MessageUpdateEvent(
                            ContentDeltaEvent(
                                type="text_delta",
                                content_index=0,
                                delta=item.text,
                            )
                        )
                    )
        elif isinstance(item, ModelResponseCompleted):
            response = item.message
        else:
            raise ProviderProtocolError(
                f"model stream emitted unsupported event: {type(item).__name__}"
            )

    if response is None:
        raise ProviderProtocolError(
            "model stream ended without a completed response"
        )
    streamed_text = "".join(text_parts)
    if text_parts and streamed_text != response.text:
        raise ProviderProtocolError(
            "model stream text deltas do not match the completed response"
        )
    return response


async def _cancel_completion(
    task: asyncio.Task[AssistantMessage],
) -> AssistantMessage | None:
    if task.done():
        return await task
    task.cancel()
    try:
        return await task
    except asyncio.CancelledError:
        return None


async def _cancel_waiter(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
