"""定义工作区文本搜索工具。"""

from __future__ import annotations

from pathlib import Path

from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE
from sec.tools._builtins.common import (
    MAX_RESULT_ITEMS,
    MAX_SEARCH_BYTES,
    read_text_file,
    resolve_workspace_path,
    walk_files,
)
from sec.tools.tool import Tool, tool


def create_grep_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="read",
        source=BUILTIN_WORKSPACE_SOURCE,
        workspace_scoped=True,
    )
    def grep(pattern: str, path: str = ".") -> list[dict[str, str | int]]:
        """Search workspace text files for a literal, case-sensitive substring."""
        if not pattern or len(pattern) > 1_000:
            raise ValueError("grep pattern must contain 1 to 1000 characters")
        target = resolve_workspace_path(root, path)
        candidates = (
            (target,) if target.is_file() else walk_files(target)
        )
        matches: list[dict[str, str | int]] = []
        searched_bytes = 0
        for candidate in candidates:
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            searched_bytes += size
            if searched_bytes > MAX_SEARCH_BYTES:
                break
            try:
                content = read_text_file(candidate)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if pattern in line:
                    matches.append(
                        {
                            "path": candidate.relative_to(root).as_posix(),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= MAX_RESULT_ITEMS:
                        return matches
        return matches

    return grep
