"""定义工作区文件名搜索工具。"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import (
    MAX_RESULT_ITEMS,
    resolve_workspace_path,
    walk_files,
)
from sec.tools.tool import Tool, tool


def create_find_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="read",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def find(pattern: str, path: str = ".") -> list[str]:
        """Find workspace files by basename glob pattern."""
        if not pattern or len(pattern) > 256:
            raise ValueError("find pattern must contain 1 to 256 characters")
        target = resolve_workspace_path(root, path)
        matches: list[str] = []
        for candidate in walk_files(target):
            if fnmatch.fnmatch(candidate.name, pattern):
                matches.append(candidate.relative_to(root).as_posix())
                if len(matches) >= MAX_RESULT_ITEMS:
                    break
        return matches

    return find
