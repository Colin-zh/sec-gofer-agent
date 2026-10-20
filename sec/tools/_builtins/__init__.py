"""提供按工具拆分的内置工具工厂。"""

from __future__ import annotations

from pathlib import Path

from sec.tools._builtins.bash import create_bash_tool
from sec.tools._builtins.edit import create_edit_tool
from sec.tools._builtins.find import create_find_tool
from sec.tools._builtins.grep import create_grep_tool
from sec.tools._builtins.ls import create_ls_tool
from sec.tools._builtins.question import create_question_tool
from sec.tools._builtins.read import create_read_tool
from sec.tools._builtins.write import create_write_tool
from sec.tools.registry import ToolRegistry
from sec.tools.tool import Tool


def create_builtin_registry(workspace_root: str | Path) -> ToolRegistry:
    """创建并返回绑定到指定工作区的内置工具注册表。"""
    root = Path(workspace_root).expanduser().resolve()
    registry = ToolRegistry()
    for create_tool in (
        create_read_tool,
        create_write_tool,
        create_edit_tool,
        create_ls_tool,
        create_find_tool,
        create_grep_tool,
        create_bash_tool,
    ):
        registry.register(create_tool(root))
    registry.register(create_question_tool())
    return registry


def create_builtin_tools(workspace_root: str | Path) -> tuple[Tool, ...]:
    """创建绑定到一个工作区的 read/write/edit/ls/find/grep/bash/question 工具。"""
    return create_builtin_registry(workspace_root).tools

__all__ = [
    "create_builtin_registry",
    "create_builtin_tools",
]
