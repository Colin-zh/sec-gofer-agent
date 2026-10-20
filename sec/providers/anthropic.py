"""将 Anthropic Messages API 接入 sec 的模型 Provider 协议。

适配器负责系统指令、消息、工具 schema 和工具结果转换，并通过官方异步
stream helper 转发文本增量；仅在取得完整最终消息后才提交给运行时。
当前明确拒绝未支持的多模态文件与推理内容块。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from time import time
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic

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
from sec._types.providers import ModelRequestOptions
from sec.tools.tool import ToolDefinition
from sec._types.streaming import ModelResponseCompleted, ModelStreamEvent, ModelTextDelta


class AnthropicProvider:
    """调用 Anthropic Messages，并提供完整及流式响应接口。"""

    def __init__(self, settings: ProviderSettings) -> None:
        if settings.name != "anthropic":
            raise ValueError("AnthropicProvider requires Anthropic provider settings")
        self._settings = settings
        self._client = AsyncAnthropic(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=0,
        )

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        options: ModelRequestOptions,
    ) -> AssistantMessage:
        """请求完整响应，并消费流直到 Provider 收尾。"""
        response: AssistantMessage | None = None
        async for item in self.stream(messages, tools, options):
            if isinstance(item, ModelResponseCompleted):
                response = item.message
        if response is None:
            raise ProviderProtocolError("Anthropic stream ended without a response")
        return response

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        options: ModelRequestOptions,
    ) -> AsyncIterator[ModelStreamEvent]:
        """逐步发送文本增量，最后返回已完整组装的 assistant 消息。"""
        system, wire_messages = _to_anthropic_messages(messages)
        request: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": self._settings.max_output_tokens,
            "messages": wire_messages,
        }
        if system:
            request["system"] = system
        wire_tools = _to_anthropic_tools(tools)
        if wire_tools:
            request["tools"] = wire_tools
        request.update(options.stream_options)

        try:
            async with self._client.messages.stream(**request) as response_stream:
                async for text in response_stream.text_stream:
                    if text:
                        yield ModelTextDelta(text)
                response = await response_stream.get_final_message()
        except APIConnectionError as exc:
            raise RetryableProviderError(
                "Anthropic request failed due to a connection error"
            ) from exc
        except APIStatusError as exc:
            retryable_error = retryable_status_error(
                "Anthropic",
                exc.status_code,
                exc.response.headers,
            )
            if retryable_error is not None:
                raise retryable_error from exc
            raise

        yield ModelResponseCompleted(_from_anthropic_message(response))

    async def aclose(self) -> None:
        """关闭 Anthropic 客户端并释放连接池资源。"""
        await self._client.close()


def _to_anthropic_messages(
    messages: Sequence[Message],
) -> tuple[str, list[dict[str, Any]]]:
    """提取 system 指令并映射其余消息及工具结果。"""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            if isinstance(message.content, str):
                system_parts.append(message.content)
            else:
                for block in message.content:
                    if not isinstance(block, TextContent):
                        _unsupported_block(block, "system")
                    system_parts.append(block.text)
            continue

        blocks: list[dict[str, Any]] = []
        if isinstance(message, UserMessage):
            text = message.content if isinstance(message.content, str) else None
            if text is not None:
                blocks.append({"type": "text", "text": text})
            else:
                for block in message.content:
                    if not isinstance(block, TextContent):
                        _unsupported_block(block, "user")
                    blocks.append({"type": "text", "text": block.text})
            role = "user"
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextContent):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseContent):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": dict(block.arguments),
                        }
                    )
                else:
                    _unsupported_block(block, "assistant")
            role = "assistant"
        elif isinstance(message, ToolResultMessage):
            if any(not isinstance(block, TextContent) for block in message.content):
                if message.content:
                    raise ProviderProtocolError(
                        "Anthropic Messages does not support tool-result attachments"
                    )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.text,
                    "is_error": message.is_error,
                }
            )
            role = "user"
        else:
            raise ProviderProtocolError(
                f"Anthropic cannot map sec message {type(message).__name__}"
            )

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        _append_message(converted, role, blocks)

    return "\n".join(system_parts), converted


def _append_message(
    messages: list[dict[str, Any]],
    role: str,
    content: list[dict[str, Any]],
) -> None:
    """合并相邻同角色消息，符合 Anthropic 对话序列的交替要求。"""
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
    else:
        messages.append({"role": role, "content": content})


def _to_anthropic_tools(
    tools: Sequence[ToolDefinition],
) -> list[dict[str, Any]]:
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "input_schema": dict(definition.input_schema),
        }
        for definition in tools
    ]


def _from_anthropic_message(response: Any) -> AssistantMessage:
    """将 Anthropic 最终消息块转换为 sec 消息及用量元数据。"""
    if response.stop_reason is None:
        raise ProviderProtocolError(
            "Anthropic stream ended without a stop reason"
        )
    content: list[TextContent | ToolUseContent] = []
    for block in response.content:
        if block.type == "text":
            content.append(TextContent(text=block.text))
        elif block.type == "tool_use":
            if not isinstance(block.input, Mapping):
                raise ProviderProtocolError(
                    "Anthropic tool input must be a JSON object"
                )
            content.append(
                ToolUseContent(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input),
                )
            )
        else:
            _unsupported_block(block, "assistant response")

    raw_usage = response.usage.model_dump(exclude_none=True)
    usage = _from_anthropic_usage(raw_usage)
    return AssistantMessage(
        content=content,
        api="anthropic-messages",
        provider="anthropic",
        model=response.model,
        usage=usage,
        stop_reason=_anthropic_stop_reason(response.stop_reason),
        timestamp=time(),
        metadata={"usage_reported": response.usage is not None},
        response_id=response.id,
        raw_stop_reason=response.stop_reason,
    )


def _unsupported_block(block: Any, role: str) -> None:
    if isinstance(block, (FileContent, ThinkingContent)):
        kind = block.type
    else:
        kind = getattr(block, "type", type(block).__name__)
    raise ProviderProtocolError(
        f"Anthropic does not support {kind} content in a {role} message"
    )


def _from_anthropic_usage(raw_usage: Mapping[str, Any]) -> Usage:
    input_tokens = _number(raw_usage.get("input_tokens"))
    output_tokens = _number(raw_usage.get("output_tokens"))
    cache_read = _number(raw_usage.get("cache_read_input_tokens"))
    cache_write = _number(raw_usage.get("cache_creation_input_tokens"))
    return Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        total_tokens=input_tokens + output_tokens,
        cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
    )


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return float(value)


def _anthropic_stop_reason(value: str) -> StopReason:
    if value == "end_turn" or value == "stop_sequence":
        return "stop"
    if value == "max_tokens":
        return "length"
    if value == "tool_use":
        return "tool_use"
    raise ProviderProtocolError(
        f"Anthropic returned unsupported stop reason {value!r}"
    )
