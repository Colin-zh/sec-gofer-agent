"""原子 JSON 文件读写原语，供 File 内存后端复用。

File 后端把长期记忆持久化到工作区 ``.sec/`` 下的 JSON 文件；写入采用
「临时文件 + fsync + replace」保证崩溃时不会留下半截文件，读取失败与空文件
可区分（FileNotFoundError → 视为尚无记忆）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def load_json(path: Path) -> Any:
    """读取 JSON 文件；文件缺失返回 None，格式错误抛 ValueError。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid memory file: {path}") from exc


def save_json(path: Path, payload: Any) -> None:
    """原子写入 JSON：先写同目录临时文件再 replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".json",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
