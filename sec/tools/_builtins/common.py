"""提供内置工作区工具共用的路径及文件操作。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

MAX_FILE_BYTES = 1_000_000
MAX_RESULT_ITEMS = 200
MAX_WALK_ENTRIES = 10_000
MAX_SEARCH_BYTES = 20_000_000

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
PROTECTED_DIRECTORIES = {".ssh", ".aws", ".gnupg"}


def resolve_workspace_path(workspace_root: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    resolved = (
        candidate if candidate.is_absolute() else workspace_root / candidate
    ).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise PermissionError(
            "path is outside the configured workspace"
        ) from exc
    if is_protected_path(resolved):
        raise PermissionError("access to protected files is denied")
    return resolved


def is_protected_path(path: Path) -> bool:
    if any(
        part.casefold() in {".git", *PROTECTED_DIRECTORIES}
        for part in path.parts
    ):
        return True
    name = path.name.casefold()
    if name.startswith(".env") and name != ".env.example":
        return True
    return path.suffix.casefold() in {".pem", ".p12", ".pfx", ".key"}


def is_ignored_entry(path: Path) -> bool:
    return path.name in IGNORED_DIRECTORIES or is_protected_path(path)


def read_text_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError("read target must be a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file must not exceed 1 MB")
    return path.read_text(encoding="utf-8")


def walk_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        raise ValueError("search path must be a directory or file")
    visited = 0
    for directory, directory_names, file_names in os.walk(
        root,
        followlinks=False,
    ):
        parent = Path(directory)
        directory_names.sort()
        file_names.sort()
        directory_names[:] = [
            name
            for name in directory_names
            if name not in IGNORED_DIRECTORIES
            and not is_ignored_entry(parent / name)
        ]
        for name in file_names:
            path = parent / name
            visited += 1
            if visited > MAX_WALK_ENTRIES:
                return
            if is_ignored_entry(path) or path.is_symlink():
                continue
            yield path
