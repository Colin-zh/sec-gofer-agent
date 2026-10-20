"""处理 Session 恢复时未完成的工具调用。

本模块只根据已提交的消息历史识别未配对工具调用并生成安全的未知结果；
它不访问存储，也不执行或重放工具。
"""

from __future__ import annotations

from collections.abc import Iterable
from time import time

from sec._types.content import TextContent, ToolUseContent
from sec._types.messages import AssistantMessage, Message, ToolResultMessage


def build_interrupted_tool_result_message(
    messages: Iterable[Message],
) -> tuple[ToolResultMessage, ...]:
    """为恢复快照中没有结果配对的工具调用构造错误结果消息。"""
    pending: dict[str, ToolUseContent] = {}
    for message in messages:
        if isinstance(message, AssistantMessage):
            for tool_use in message.tool_uses:
                pending[tool_use.id] = tool_use
        elif isinstance(message, ToolResultMessage):
            pending.pop(message.tool_call_id, None)

    if not pending:
        return ()

    return tuple(
        ToolResultMessage(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            content=[
                TextContent(
                    text=(
                        "The previous Trace ended before this tool result was committed. "
                        "Its outcome is unknown; the tool was not replayed during recovery."
                    )
                )
            ],
            is_error=True,
            timestamp=time(),
            metadata={"source": "session_recovery"},
        )
        for tool_use in pending.values()
    )
