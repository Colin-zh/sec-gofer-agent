"""定义工作区文件精确替换工具。"""

from __future__ import annotations

from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import (
    MAX_FILE_BYTES,
    read_text_file,
    resolve_workspace_path,
)
from sec.tools.tool import Tool, tool


def create_edit_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="write",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def edit(path: str, old_text: str, new_text: str) -> str:
        """Replace one exact text occurrence in a workspace file."""
        if not old_text:
            raise ValueError("old_text must not be empty")
        target = resolve_workspace_path(root, path)
        content = read_text_file(target)
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ValueError(
                f"edit requires exactly one match; found {occurrences}"
            )
        updated = content.replace(old_text, new_text, 1)
        if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("edited file must not exceed 1 MB")
        target.write_text(updated, encoding="utf-8")
        return f"Edited {target.relative_to(root).as_posix()}."

    return edit
