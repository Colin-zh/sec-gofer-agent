"""定义模型 Provider 的异步调用和资源管理行为契约。

Provider 接收规范化消息和工具定义，并返回一条完整的规范化消息；供应商
适配细节留在 providers 子包，不进入 Agent 执行内核。
仅实现 ModelProvider 的后端使用完整响应；支持增量输出的后端可实现
StreamingModelProvider，最终消息仍需完整且规范化。自定义 Provider 通过
RetryableProviderError 显式声明可重试的暂时性请求失败。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sec._types.messages import AssistantMessage, Message
from sec._types.streaming import ModelStreamEvent
from sec.tools.tool import ToolDefinition


@dataclass(frozen=True)
class ModelRequestOptions:
    """可被 before_request hook 调整的单次模型请求选项。

    Provider 适配器将其合并进自身硬编码的请求参数；未知键应被安全忽略。
    """

    stream_options: dict[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    """通过单次异步调用返回完整消息的模型后端协议。"""

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        options: ModelRequestOptions,
    ) -> AssistantMessage:
        """返回完整消息；可重试的暂时性错误应抛出 RetryableProviderError。"""


@runtime_checkable
class StreamingModelProvider(Protocol):
    """通过异步迭代器返回增量及最终消息的流式模型后端协议。"""

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        options: ModelRequestOptions,
    ) -> AsyncIterator[ModelStreamEvent]:
        """返回文本增量和最终完整消息，并传播取消及 RetryableProviderError。"""


class ManagedStreamingModelProvider(StreamingModelProvider, Protocol):
    """支持显式释放底层异步客户端资源的流式 Provider。"""

    async def aclose(self) -> None:
        """关闭底层客户端并释放网络资源。"""
