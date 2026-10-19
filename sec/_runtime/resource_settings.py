"""Small atomic settings files shared by all workspace applications."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


def read_worker_extensions(workspace: Path) -> tuple[str, ...]:
    path = workspace / ".sec" / "worker-extensions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    if not isinstance(data, dict) or set(data) != {"enabled"}:
        raise ValueError(f"invalid worker extension configuration: {path}")
    return validate_extension_names(data["enabled"])


def validate_extension_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(name, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) is None
        for name in value
    ):
        raise ValueError("worker extensions must be a list of entry-point names")
    if len(value) != len(set(value)):
        raise ValueError("worker extension names must not be repeated")
    return tuple(value)


def write_worker_extensions(workspace: Path, names: tuple[str, ...]) -> None:
    names = validate_extension_names(names)
    path = workspace / ".sec" / "worker-extensions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".worker-extensions-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"enabled": names}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
