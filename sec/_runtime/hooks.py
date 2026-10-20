"""为单个 Trace 执行变换型 hook，并施加各自的聚合语义。

聚合策略与 pi-main 对齐：
- 链式（transform_context / before_request / before_payload / after_response /
  before_tool / after_tool）：前一个 handler 的输出作为后一个的输入。
- fail-closed（before_drive）：任一异常直接向上传播。
- first-wins（before_compaction / before_navigation）：第一个返回有效结果的生效。
- collect（before_trace_end）：汇总所有 handler 的 follow_up。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from sec._types.hooks import (
    AfterResponseEvent,
    AfterResponseResult,
    AfterToolEvent,
    AfterToolResult,
    BeforeCompactionEvent,
    BeforeCompactionResult,
    BeforeDriveEvent,
    BeforeNavigationEvent,
    BeforeNavigationResult,
    BeforePayloadEvent,
    BeforePayloadResult,
    BeforeRequestEvent,
    BeforeRequestResult,
    BeforeTraceEndEvent,
    BeforeTraceEndResult,
    BeforeTraceEvent,
    BeforeTraceResult,
    BeforeToolEvent,
    BeforeToolResult,
    BlockResult,
    HookContext,
    HookHandler,
    HookName,
    TransformContextEvent,
    TransformContextResult,
)
from sec._types.json import JSONObject, JSONValue
from sec._types.messages import AgentMessage, AssistantMessage, Message, ToolResultMessage
from sec._types.providers import ModelRequestOptions


class HookExecutor:
    """按 hook 名持有有序 handler，并对每次触发施加正确聚合。"""

    def __init__(self, hooks: Mapping[HookName, tuple[HookHandler, ...]]) -> None:
        self._hooks: dict[HookName, tuple[HookHandler, ...]] = dict(hooks)

    def _handlers(self, name: HookName) -> tuple[HookHandler, ...]:
        return self._hooks.get(name, ())

    async def before_trace(self, prompt: str, ctx: HookContext) -> tuple[Message, ...]:
        """收集所有 handler 注入的消息。"""
        event = BeforeTraceEvent(prompt=prompt)
        injected: list[Message] = []
        for handler in self._handlers("before_trace"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeTraceResult):
                raise TypeError("before_trace hook must return BeforeTraceResult or None")
            if result.messages is not None:
                injected.extend(result.messages)
        return tuple(injected)

    async def before_drive(
        self,
        operation: str,
        ctx: HookContext,
    ) -> None:
        """fail-closed：任一 handler 抛异常即中断本次 drive。"""
        event = BeforeDriveEvent(operation=operation)
        for handler in self._handlers("before_drive"):
            await _invoke(handler, event, ctx)

    async def before_trace_end(
        self,
        messages: tuple[AgentMessage, ...],
        ctx: HookContext,
    ) -> tuple[str, ...]:
        """收集所有 handler 声明的 follow-up。"""
        event = BeforeTraceEndEvent(messages=messages)
        follow_ups: list[str] = []
        for handler in self._handlers("before_trace_end"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeTraceEndResult):
                raise TypeError(
                    "before_trace_end hook must return BeforeTraceEndResult or None"
                )
            if result.follow_up is not None and result.follow_up.strip():
                follow_ups.append(result.follow_up)
        return tuple(follow_ups)

    async def transform_context(
        self,
        messages: tuple[Message, ...],
        system_prompt: str,
        ctx: HookContext,
    ) -> tuple[tuple[Message, ...], str]:
        """链式改写上下文消息与系统提示。"""
        current_messages = messages
        current_prompt = system_prompt
        event = TransformContextEvent(
            messages=current_messages, system_prompt=current_prompt
        )
        for handler in self._handlers("transform_context"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, TransformContextResult):
                raise TypeError(
                    "transform_context hook must return TransformContextResult or None"
                )
            if result.messages is not None:
                current_messages = tuple(result.messages)
            if result.system_prompt is not None:
                current_prompt = result.system_prompt
            event = TransformContextEvent(
                messages=current_messages, system_prompt=current_prompt
            )
        return current_messages, current_prompt

    async def before_request(
        self,
        options: ModelRequestOptions,
        ctx: HookContext,
    ) -> ModelRequestOptions:
        """链式改写请求选项。"""
        current = options
        event = BeforeRequestEvent(options=current)
        for handler in self._handlers("before_request"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeRequestResult):
                raise TypeError(
                    "before_request hook must return BeforeRequestResult or None"
                )
            if result.options is not None:
                current = result.options
                event = BeforeRequestEvent(options=current)
        return current

    async def before_payload(
        self,
        payload: JSONObject,
        ctx: HookContext,
    ) -> JSONObject:
        """链式改写 wire payload。"""
        current = payload
        event = BeforePayloadEvent(payload=current)
        for handler in self._handlers("before_payload"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforePayloadResult):
                raise TypeError(
                    "before_payload hook must return BeforePayloadResult or None"
                )
            if result.payload is not None:
                current = result.payload
                event = BeforePayloadEvent(payload=current)
        return current

    async def after_response(
        self,
        message: AssistantMessage,
        ctx: HookContext,
    ) -> AssistantMessage:
        """链式改写模型响应消息。"""
        current = message
        event = AfterResponseEvent(message=current)
        for handler in self._handlers("after_response"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, AfterResponseResult):
                raise TypeError(
                    "after_response hook must return AfterResponseResult or None"
                )
            if result.message is not None:
                current = result.message
                event = AfterResponseEvent(message=current)
        return current

    async def before_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, JSONValue],
        ctx: HookContext,
    ) -> tuple[dict[str, JSONValue], BlockResult | None]:
        """链式改写参数；任一 handler 返回 block 即短路。"""
        current = args
        event = BeforeToolEvent(
            tool_call_id=tool_call_id, tool_name=tool_name, args=current
        )
        for handler in self._handlers("before_tool"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeToolResult):
                raise TypeError("before_tool hook must return BeforeToolResult or None")
            if result.block is not None:
                return current, result.block
            if result.args is not None:
                current = result.args
                event = BeforeToolEvent(
                    tool_call_id=tool_call_id, tool_name=tool_name, args=current
                )
        return current, None

    async def after_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, JSONValue],
        result: ToolResultMessage,
        ctx: HookContext,
    ) -> tuple[ToolResultMessage, bool]:
        """链式改写工具结果；汇总 terminate 请求。"""
        current = result
        terminate = False
        event = AfterToolEvent(
            tool_call_id=tool_call_id, tool_name=tool_name, args=args, result=current
        )
        for handler in self._handlers("after_tool"):
            handler_result = await _invoke(handler, event, ctx)
            if handler_result is None:
                continue
            if not isinstance(handler_result, AfterToolResult):
                raise TypeError("after_tool hook must return AfterToolResult or None")
            if handler_result.result is not None:
                current = handler_result.result
                event = AfterToolEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    result=current,
                )
            terminate = terminate or handler_result.terminate
        return current, terminate

    async def before_compaction(
        self,
        reason: str,
        ctx: HookContext,
    ) -> BeforeCompactionResult:
        """first-wins：第一个返回结果的 handler 生效。"""
        event = BeforeCompactionEvent(reason=reason)
        for handler in self._handlers("before_compaction"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeCompactionResult):
                raise TypeError(
                    "before_compaction hook must return BeforeCompactionResult or None"
                )
            return result
        return BeforeCompactionResult()

    async def before_navigation(
        self,
        target_id: str,
        ctx: HookContext,
    ) -> BeforeNavigationResult:
        """first-wins：第一个返回结果的 handler 生效。"""
        event = BeforeNavigationEvent(target_id=target_id)
        for handler in self._handlers("before_navigation"):
            result = await _invoke(handler, event, ctx)
            if result is None:
                continue
            if not isinstance(result, BeforeNavigationResult):
                raise TypeError(
                    "before_navigation hook must return BeforeNavigationResult or None"
                )
            return result
        return BeforeNavigationResult()


async def _invoke(
    handler: HookHandler,
    event: Any,
    ctx: HookContext,
) -> Any:
    """调用单个 handler，支持同步或异步，不吞异常。"""
    result = handler(event, ctx)
    if inspect.isawaitable(result):
        result = await result
    return result


__all__ = ["HookExecutor"]
