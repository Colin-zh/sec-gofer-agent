"""提供版本化、仅含数据的 Session 树导入与导出格式。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from sec import SessionSnapshot, SessionStoreError
from sec.storage.session_store import (
    decode_session_entry,
    encode_session_entry,
)

PORTABLE_SESSION_FORMAT = "sec-session"
PORTABLE_SESSION_VERSION = 1
MAX_SESSION_FILE_BYTES = 16 * 1024 * 1024


def encode_session(snapshot: SessionSnapshot) -> str:
    """将 Session 树序列化为不含可执行载荷的 JSON。"""
    payload = {
        "format": PORTABLE_SESSION_FORMAT,
        "format_version": PORTABLE_SESSION_VERSION,
        "session_id": snapshot.session_id,
        "active_leaf_id": snapshot.active_leaf_id,
        "entries": [
            json.loads(encode_session_entry(entry))
            for entry in snapshot.entries
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session export exceeds {MAX_SESSION_FILE_BYTES} bytes")
    return encoded


def decode_session(data: bytes) -> SessionSnapshot:
    """在固定大小上限内解析并校验可移植 Session 文件。"""
    if len(data) > MAX_SESSION_FILE_BYTES:
        raise ValueError(f"Session file exceeds {MAX_SESSION_FILE_BYTES} bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Session file must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Session file must contain a JSON object")
    if payload.get("format") != PORTABLE_SESSION_FORMAT:
        raise ValueError("unsupported Session file format")
    if payload.get("format_version") != PORTABLE_SESSION_VERSION:
        raise ValueError("unsupported Session file version")
    session_id = payload.get("session_id")
    active_leaf_id = payload.get("active_leaf_id")
    raw_entries = payload.get("entries")
    if not isinstance(session_id, str) or not isinstance(raw_entries, list):
        raise ValueError("Session file is missing session_id or entries")
    if active_leaf_id is not None and not isinstance(active_leaf_id, str):
        raise ValueError("active_leaf_id must be a string or null")
    entries = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("each Session entry must be a JSON object")
        try:
            encoded = json.dumps(raw_entry, ensure_ascii=False, allow_nan=False)
            entries.append(decode_session_entry(encoded))
        except (TypeError, ValueError, SessionStoreError) as exc:
            raise ValueError(f"invalid Session entry: {exc}") from exc
    try:
        return SessionSnapshot(
            session_id=session_id,
            version=0,
            entries=tuple(entries),
            active_leaf_id=active_leaf_id,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Session tree: {exc}") from exc


def read_session_file(path: Path) -> SessionSnapshot:
    """最多读取规定大小的 Session 文件并完成解码。"""
    with path.expanduser().open("rb") as stream:
        data = stream.read(MAX_SESSION_FILE_BYTES + 1)
    return decode_session(data)


def write_session_file(snapshot: SessionSnapshot, path: Path) -> Path:
    """以排他方式创建 Session 文件，拒绝覆盖已有路径。"""
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = encode_session(snapshot).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sec-session-",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def copy_to_clipboard(text: str) -> None:
    """通过系统已安装的原生剪贴板工具复制文本。"""
    candidates = {
        "Darwin": ("pbcopy",),
        "Linux": ("wl-copy", "xclip", "xsel"),
        "Windows": ("clip",),
    }.get(platform.system(), ())
    utility = None
    for candidate in candidates:
        utility = shutil.which(candidate)
        if utility is not None:
            break
    if utility is None:
        raise RuntimeError("no supported clipboard utility is installed")
    command = [utility]
    if Path(utility).name == "xclip":
        command.extend(("-selection", "clipboard"))
    elif Path(utility).name == "xsel":
        command.extend(("--clipboard", "--input"))
    completed = subprocess.run(
        command,
        input=text,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        message = f"clipboard utility exited with status {completed.returncode}"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message)


__all__ = [
    "MAX_SESSION_FILE_BYTES",
    "PORTABLE_SESSION_FORMAT",
    "PORTABLE_SESSION_VERSION",
    "decode_session",
    "encode_session",
    "read_session_file",
    "write_session_file",
    "copy_to_clipboard",
]
