"""导出 sec 工具执行协议和本地实现。

Executor 只负责运行已校验且获准的工具；授权、审批和 Agent 循环由运行时处理。
"""

from __future__ import annotations

from sec._types.execution import ToolExecutor
from sec.executors.local import LocalToolExecutor

__all__ = ["LocalToolExecutor", "ToolExecutor"]
