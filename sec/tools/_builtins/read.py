"""定义工作区文件读取工具。"""

from __future__ import annotations

from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import read_text_file, resolve_workspace_path
from sec.tools.tool import Tool, tool


def create_read_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="read",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def read(path: str, start_line: int = 1, max_lines: int = 500) -> str:
        """Read a UTF-8 text file inside the workspace with numbered lines."""
        if start_line < 1 or max_lines < 1:
            raise ValueError("start_line and max_lines must be positive")
        if max_lines > 2_000:
            raise ValueError("max_lines must not exceed 2000")
        target = resolve_workspace_path(root, path)
        content = read_text_file(target)
        lines = content.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=start_line)
        )

    return read
