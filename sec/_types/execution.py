"""定义 sec 工具执行后端的协议。

执行器接收已经校验并授权的工具绑定，负责运行方式、取消与结果表达；
它不负责模型请求、参数授权或审批 UI。
"""

from __future__ import annotations

from typing import Protocol

from sec._types.content import ToolUseContent
from sec._types.messages import ToolResultMessage
from sec.tools.tool import Tool


class ToolExecutor(Protocol):
    """执行运行时已经解析、校验并获准的单次工具调用。"""

    async def execute(
        self,
        tool: Tool,
        tool_use: ToolUseContent,
    ) -> ToolResultMessage:
        """返回与输入 tool_use ID 配对的规范化工具结果。"""
