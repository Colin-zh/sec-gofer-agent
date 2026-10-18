"""将 OpenAI Chat Completions API 接入 sec 的模型 Provider 协议。

适配器负责消息与工具 schema 转换、流式文本转发及最终工具调用组装；
仅在响应完整后返回规范化消息。当前不转换多模态文件或推理块。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from time import time
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from sec._types.content import FileContent, TextContent, ThinkingContent, ToolUseContent
from sec._types.messages import (
    AssistantMessage,
    Message,
    StopReason,
    SystemMessage,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from sec.config import ProviderSettings
from sec.exceptions import ProviderProtocolError, RetryableProviderError
from sec.providers._errors import retryable_status_error
from sec._types.streaming import ModelResponseCompleted, ModelStreamEvent, ModelTextDelta
from sec.tools.tool import ToolDefinition


@dataclass
class _ToolCallBuffer:
    """累积 OpenAI 分片到完整工具调用。"""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


class OpenAIProvider:
    """调用 OpenAI Chat Completions，并提供完整及流式响应接口。"""

    def __init__(self, settings: ProviderSettings) -> None:
        if settings.name != "openai":
            raise ValueError("OpenAIProvider requires OpenAI provider settings")
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        """请求完整响应，并消费流直到 Provider 收尾。"""
        response: AssistantMessage | None = None
        async for item in self.stream(messages, tools):
            if isinstance(item, ModelResponseCompleted):
                response = item.message
        if response is None:
            raise ProviderProtocolError("OpenAI stream ended without a response")
        return response

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelStreamEvent]:
        """逐步发送文本增量，结束时产出完整消息及组装后的工具调用。"""
        wire_messages = _to_openai_messages(messages)
        wire_tools = _to_openai_tools(tools)
        try:
            response_stream = await self._client.chat.completions.create(
                model=self._settings.model,
                messages=wire_messages,
                tools=wire_tools,
                max_completion_tokens=self._settings.max_output_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
        except APIConnectionError as exc:
            raise RetryableProviderError(
                "OpenAI request failed due to a connection error"
            ) from exc
        except APIStatusError as exc:
            retryable_error = retryable_status_error(
                "OpenAI",
                exc.status_code,
                exc.response.headers,
            )
            if retryable_error is not None:
                raise retryable_error from exc
            raise

        text_parts: list[str] = []
        tool_calls: dict[int, _ToolCallBuffer] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        saw_choice = False
        try:
            async for chunk in response_stream:
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump(exclude_none=True)
                if not chunk.choices:
                    continue

                saw_choice = True
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    if not isinstance(delta.content, str):
                        raise ProviderProtocolError(
                            "OpenAI returned non-text Chat Completions content"
                        )
                    text_parts.append(delta.content)
                    yield ModelTextDelta(delta.content)

                for partial in delta.tool_calls or ():
                    buffer = tool_calls.setdefault(
                        partial.index,
                        _ToolCallBuffer(),
                    )
                    if partial.id:
                        if buffer.call_id and buffer.call_id != partial.id:
                            raise ProviderProtocolError(
                                "OpenAI changed a tool call ID during streaming"
                            )
                        buffer.call_id = partial.id
                    if partial.function is not None:
                        if partial.function.name:
                            buffer.name += partial.function.name
                        if partial.function.arguments:
                            buffer.arguments += partial.function.arguments
                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason
        except APIConnectionError as exc:
            raise RetryableProviderError(
                "OpenAI stream failed due to a connection error"
            ) from exc
        except APIStatusError as exc:
            retryable_error = retryable_status_error(
                "OpenAI",
                exc.status_code,
                exc.response.headers,
            )
            if retryable_error is not None:
                raise retryable_error from exc
            raise
        finally:
            await response_stream.close()

        if not saw_choice or finish_reason is None:
            raise ProviderProtocolError(
                "OpenAI stream ended without a completed choice"
            )

        content: list[TextContent | ToolUseContent] = (
            [TextContent(text="".join(text_parts))] if text_parts else []
        )
        for index in sorted(tool_calls):
            partial = tool_calls[index]
            if not partial.call_id or not partial.name:
                raise ProviderProtocolError(
                    "OpenAI returned a tool call without an ID or function name"
                )
            try:
                arguments = json.loads(partial.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError(
                    "OpenAI returned invalid JSON tool arguments"
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderProtocolError(
                    "OpenAI tool arguments must decode to a JSON object"
                )
            content.append(
                ToolUseContent(
                    id=partial.call_id,
                    name=partial.name,
                    arguments=arguments,
                )
            )

        completed = AssistantMessage(
            content=content,
            api="openai-chat-completions",
            provider=self._settings.name,
            model=self._settings.model,
            usage=_from_openai_usage(usage),
            stop_reason=_openai_stop_reason(finish_reason),
            timestamp=time(),
            metadata={"usage_reported": usage is not None},
            raw_stop_reason=finish_reason,
        )
        yield ModelResponseCompleted(completed)

    async def aclose(self) -> None:
        """关闭 OpenAI 客户端并释放连接池资源。"""
        await self._client.close()


def _to_openai_messages(
    messages: Sequence[Message],
) -> list[ChatCompletionMessageParam]:
    """将 sec 消息逐块转换为 OpenAI Chat Completions 消息。"""
    converted: list[ChatCompletionMessageParam] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            text = _text_only(message.content, "system")
            converted.append({"role": "system", "content": text})
            continue

        if isinstance(message, AssistantMessage):
            text_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, TextContent):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseContent):
                    calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(
                                    block.arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    )
                else:
                    _unsupported_block(block, "assistant")
            converted.append(
                {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    **({"tool_calls": calls} if calls else {}),
                }
            )
            continue

        if isinstance(message, UserMessage):
            converted.append(
                {
                    "role": "user",
                    "content": _text_only(message.content, "user"),
                }
            )
            continue

        if isinstance(message, ToolResultMessage):
            if any(not isinstance(block, TextContent) for block in message.content):
                raise ProviderProtocolError(
                    "OpenAI Chat Completions does not support file tool results"
                )
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.text,
                }
            )
            continue

        raise ProviderProtocolError(
            f"OpenAI cannot map sec message role {message.role!r}"
        )
    return converted


def _text_only(content: str | Sequence[Any], role: str) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if not isinstance(block, TextContent):
            _unsupported_block(block, role)
        parts.append(block.text)
    return "".join(parts)


def _unsupported_block(block: Any, role: str) -> None:
    if isinstance(block, (FileContent, ThinkingContent)):
        kind = block.type
    else:
        kind = type(block).__name__
    raise ProviderProtocolError(
        f"OpenAI does not support {kind} content in a {role} message"
    )


def _from_openai_usage(raw_usage: dict[str, Any] | None) -> Usage:
    values = raw_usage or {}
    input_tokens = _usage_number(values.get("prompt_tokens"))
    output_tokens = _usage_number(values.get("completion_tokens"))
    raw_total_tokens = values.get("total_tokens")
    cache_read = _usage_number(
        (values.get("prompt_tokens_details") or {}).get("cached_tokens")
        if isinstance(values.get("prompt_tokens_details"), dict)
        else 0
    )
    return Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=0,
        total_tokens=_usage_number(raw_total_tokens)
        if raw_total_tokens is not None
        else input_tokens + output_tokens,
        cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
    )


def _usage_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return float(value)


def _openai_stop_reason(value: str) -> StopReason:
    if value == "stop":
        return "stop"
    if value == "length":
        return "length"
    if value in ("tool_calls", "function_call"):
        return "tool_use"
    if value == "content_filter":
        return "error"
    raise ProviderProtocolError(f"OpenAI returned unsupported finish reason {value!r}")


def _to_openai_tools(
    tools: Sequence[ToolDefinition],
) -> list[ChatCompletionToolParam]:
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": dict(definition.input_schema),
            },
        }
        for definition in tools
    ]
