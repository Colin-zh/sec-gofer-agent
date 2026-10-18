"""定义上下文构建器可用的 token 估算行为契约。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from sec._types.messages import Message
from sec.tools.tool import ToolDefinition


@runtime_checkable
class TokenEstimator(Protocol):
    """估算完整模型输入，包含消息、系统指令及工具定义。"""

    def estimate_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> int:
        """返回非负 token 数；实现应匹配实际使用的模型和请求格式。"""
