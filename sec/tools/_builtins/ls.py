"""定义工作区目录列表工具。"""

from __future__ import annotations

from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import (
    MAX_RESULT_ITEMS,
    is_ignored_entry,
    resolve_workspace_path,
)
from sec.tools.tool import Tool, tool


def create_ls_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="read",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def ls(path: str = ".") -> list[dict[str, str]]:
        """List workspace entries, hiding protected credentials and caches."""
        target = resolve_workspace_path(root, path)
        if not target.is_dir():
            raise ValueError("ls path must be a directory")
        results: list[dict[str, str]] = []
        for child in sorted(
            target.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            if is_ignored_entry(child):
                continue
            results.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                }
            )
            if len(results) >= MAX_RESULT_ITEMS:
                break
        return results

    return ls
