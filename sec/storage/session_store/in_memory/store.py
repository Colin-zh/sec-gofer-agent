"""提供进程内 SessionStore adapter，供开发和测试使用。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence

from sec._types.storage import SessionStoreProtocol
from sec.exceptions import SessionStoreError, SessionVersionConflictError
from sec._types.session import SessionEntry, SessionSnapshot, SessionSummary


class InMemorySessionStore(SessionStoreProtocol):
    """以进程内字典实现原子比较并追加的会话存储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionSnapshot] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> SessionSnapshot:
        """返回独立副本；不存在的 Session 视为版本 0 的空会话。"""
        _validate_session_id(session_id)
        async with self._lock:
            snapshot = self._sessions.get(
                session_id,
                SessionSnapshot(session_id, 0, (), None),
            )
            return _copy_snapshot(snapshot)

    async def list_sessions(self, limit: int = 20) -> tuple[SessionSummary, ...]:
        """按最近条目时间返回非空会话摘要。"""
        _validate_limit(limit)
        async with self._lock:
            summaries = [
                SessionSummary.from_snapshot(snapshot)
                for snapshot in self._sessions.values()
            ]
        summaries.sort(
            key=lambda summary: (
                summary.updated_at is not None,
                summary.updated_at or 0,
                summary.session_id,
            ),
            reverse=True,
        )
        return tuple(summaries[:limit])

    async def append(
        self,
        session_id: str,
        expected_version: int,
        entries: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> int:
        """以版本比较原子追加一个非空消息批次。"""
        _validate_session_id(session_id)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected version must be a non-negative integer")
        _validate_entries(entries, active_leaf_id)

        async with self._lock:
            current = self._sessions.get(
                session_id,
                SessionSnapshot(session_id, 0, (), None),
            )
            if current.version != expected_version:
                raise SessionVersionConflictError(
                    session_id,
                    expected_version,
                    current.version,
                )

            detached_entries = _copy_entries(entries)
            all_entries = current.entries + detached_entries
            _validate_tree(session_id, current, detached_entries, active_leaf_id)
            updated = SessionSnapshot(
                session_id,
                current.version + 1,
                all_entries,
                active_leaf_id,
            )
            self._sessions[session_id] = updated
            return updated.version

    async def delete(self, session_id: str, expected_version: int) -> None:
        """仅删除版本匹配的会话，拒绝覆盖并发修改。"""
        _validate_session_id(session_id)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected version must be a non-negative integer")
        async with self._lock:
            current = self._sessions.get(session_id)
            version = 0 if current is None else current.version
            if version != expected_version:
                raise SessionVersionConflictError(session_id, expected_version, version)
            self._sessions.pop(session_id, None)


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session ID must not be empty")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit < 1:
        raise ValueError("session list limit must be a positive integer")


def _validate_entries(
    entries: Sequence[SessionEntry],
    active_leaf_id: str,
) -> None:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise TypeError("entries must be a sequence of SessionEntry instances")
    if not entries:
        raise ValueError("append requires at least one entry")
    if any(not isinstance(entry, SessionEntry) for entry in entries):
        raise TypeError("entries must contain SessionEntry instances")
    if not isinstance(active_leaf_id, str) or not active_leaf_id.strip():
        raise ValueError("active leaf ID must not be empty")


def _validate_tree(
    session_id: str,
    current: SessionSnapshot,
    additions: Sequence[SessionEntry],
    active_leaf_id: str,
) -> None:
    existing_ids = {entry.id for entry in current.entries}
    all_ids = set(existing_ids)
    children: set[str] = set()
    for entry in additions:
        if entry.id in all_ids:
            raise ValueError(f"duplicate session entry ID: {entry.id!r}")
        if entry.parent_id is not None and entry.parent_id not in all_ids:
            raise ValueError(
                f"entry {entry.id!r} has unknown or out-of-order parent "
                f"{entry.parent_id!r} in session {session_id!r}"
            )
        if entry.parent_id is not None:
            children.add(entry.parent_id)
        all_ids.add(entry.id)
    for entry in current.entries:
        if entry.parent_id is not None:
            children.add(entry.parent_id)
    if active_leaf_id not in all_ids or active_leaf_id in children:
        raise ValueError("active leaf must identify an existing leaf entry")


def _copy_entries(entries: Sequence[SessionEntry]) -> tuple[SessionEntry, ...]:
    try:
        return tuple(copy.deepcopy(entry) for entry in entries)
    except Exception as exc:
        raise SessionStoreError("could not copy Session entries") from exc


def _copy_snapshot(snapshot: SessionSnapshot) -> SessionSnapshot:
    return SessionSnapshot(
        session_id=snapshot.session_id,
        version=snapshot.version,
        entries=_copy_entries(snapshot.entries),
        active_leaf_id=snapshot.active_leaf_id,
    )
