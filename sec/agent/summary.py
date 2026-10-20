"""trace/span 摘要：用小模型把一次 trace 的消息压缩成一句话摘要。

用于观测侧（桌面 Trace 列表、定时任务回执等）而非上下文压缩；上下文压缩
摘要仍由 ContextBuilder + 主循环独立处理。失败由调用方 fail-open。
"""

from __future__ import annotations

from collections.abc import Sequence

from sec._types.messages import Message
from sec._types.providers import ModelProvider
from sec.agent._util import complete_text

_SUMMARY_SYSTEM = (
    "Summarize the following conversation trace into one concise sentence "
    "describing what the user asked and what happened/resulted. Do not invent "
    "facts. Reply with ONLY the summary sentence."
)

_MAX_INPUT_CHARS = 12_000


class TraceSummarizer:
    """用注入的 ModelProvider 生成 trace 的一句话摘要。"""

    def __init__(self, provider: ModelProvider) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("summary provider must implement complete()")
        self._provider = provider

    async def summarize(self, messages: Sequence[Message]) -> str:
        text = _messages_text(messages)
        raw = await complete_text(self._provider, _SUMMARY_SYSTEM, text)
        return raw.strip()


def _messages_text(messages: Sequence[Message]) -> str:
    parts = [
        f"{message.role}: {message.text.strip()}"
        for message in messages
        if message.text.strip()
    ]
    text = "\n".join(parts)
    if len(text) > _MAX_INPUT_CHARS:
        text = text[: _MAX_INPUT_CHARS] + "…"
    return text
