"""定义工作区文件写入工具。"""

from __future__ import annotations

from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import (
    MAX_FILE_BYTES,
    resolve_workspace_path,
)
from sec.tools.tool import Tool, tool


def create_write_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="write",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def write(path: str, content: str) -> str:
        """Create or replace a UTF-8 text file inside the workspace."""
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("file content must not exceed 1 MB")
        target = resolve_workspace_path(root, path)
        if target.exists() and not target.is_file():
            raise ValueError("write target must be a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {target.relative_to(root).as_posix()}."

    return write
