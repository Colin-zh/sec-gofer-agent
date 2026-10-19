"""在当前 Python 进程中运行已授权的 sec 工具。

本地执行器把工具返回值规范化为 ToolResultMessage，不负责策略、审批或重试。
同步工具在线程中运行；本地执行器使用宿主进程权限，不是安全沙箱。
"""

from __future__ import annotations

import json
from time import time

from sec._types.content import TextContent, ToolUseContent
from sec._types.messages import ToolResultMessage
from sec.tools.tool import Tool, ToolOutput


class LocalToolExecutor:
    """通过 Tool.invoke 执行已授权工具并规范化其结果。"""

    async def execute(
        self,
        tool: Tool,
        tool_use: ToolUseContent,
    ) -> ToolResultMessage:
        """运行绑定函数，并保留工具调用 ID 以配对结果。"""
        value = await tool.invoke(tool_use.arguments)
        if isinstance(value, ToolOutput):
            metadata = dict(value.metadata or {})
            if value.structured_content is not None:
                metadata["structured_content"] = dict(value.structured_content)
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            text = value.text
            if not text and value.structured_content is not None:
                text = json.dumps(value.structured_content, ensure_ascii=False)
            return ToolResultMessage(
                tool_call_id=tool_use.id,
                tool_name=tool_use.name,
                content=[TextContent(text=text)],
                is_error=value.is_error,
                metadata=metadata,
                timestamp=time(),
            )
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return ToolResultMessage(
            tool_call_id=tool_use.id,
            tool_name=tool_use.name,
            content=[TextContent(text=content)],
            is_error=False,
            timestamp=time(),
        )
