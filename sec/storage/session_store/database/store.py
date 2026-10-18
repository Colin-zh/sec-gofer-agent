"""使用共享 Database 资源持久化 Session 条目树和活动叶节点。

DatabaseSessionStore 负责安全编解码、CAS 版本检查及事务内原子追加；Database
拥有 Engine/连接池并由应用统一关闭。工具副作用不属于数据库事务。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sec._types.session import SessionEntry, SessionSnapshot, SessionSummary
from sec._types.storage import SessionStoreProtocol
from sec.exceptions import SessionStoreError, SessionVersionConflictError
from sec.storage.resources.database import Database
from sec.storage.session_store.codec import (
    decode_session_entry,
    encode_session_entry,
)
from sec.storage.session_store.database.schema import (
    create_or_upgrade_schema,
    entries,
    sessions,
)


class DatabaseSessionStore(SessionStoreProtocol):
    """将完整会话树写入 SQLAlchemy 支持的关系型数据库。"""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database instance")
        self._database = database
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """创建缺失的 SessionStore 表；不会创建或关闭共享 Database。"""
        async with self._initialize_lock:
            if self._initialized:
                return
            try:
                await self._database.run_sync(create_or_upgrade_schema)
            except SQLAlchemyError as exc:
                cause = exc.orig if isinstance(exc, DBAPIError) else exc
                raise SessionStoreError(
                    "could not initialize SQLAlchemy SessionStore schema: "
                    f"{type(cause).__name__}: {cause}"
                ) from exc
            self._initialized = True

    async def load(self, session_id: str) -> SessionSnapshot:
        """读取有序条目树及活动叶；不存在的会话视为版本 0。"""
        _validate_session_id(session_id)
        self._require_initialized()
        try:
            async with self._database.session() as db_session:
                state = await _read_state(db_session, session_id)
                if state is None:
                    return SessionSnapshot(session_id, 0, (), None)
                rows = await db_session.execute(
                    select(entries.c.payload)
                    .where(entries.c.session_id == session_id)
                    .order_by(entries.c.sequence)
                )
                loaded_entries = tuple(
                    decode_session_entry(payload) for payload in rows.scalars()
                )
                return SessionSnapshot(
                    session_id,
                    state[0],
                    loaded_entries,
                    state[1],
                )
        except SessionStoreError:
            raise
        except SQLAlchemyError as exc:
            raise SessionStoreError("could not load Session from database") from exc

    async def list_sessions(self, limit: int = 20) -> tuple[SessionSummary, ...]:
        """返回有历史记录的会话摘要，优先选择版本较新的会话。"""
        _validate_limit(limit)
        self._require_initialized()
        try:
            async with self._database.session() as db_session:
                result = await db_session.scalars(
                    select(sessions.c.session_id)
                    .order_by(sessions.c.version.desc(), sessions.c.session_id)
                    .limit(limit)
                )
                session_ids = result.all()
            snapshots = [await self.load(session_id) for session_id in session_ids]
        except SessionStoreError:
            raise
        except SQLAlchemyError as exc:
            raise SessionStoreError("could not list Sessions from database") from exc
        summaries = [
            SessionSummary.from_snapshot(snapshot)
            for snapshot in snapshots
            if snapshot.entries
        ]
        summaries.sort(
            key=lambda summary: (
                summary.updated_at is not None,
                summary.updated_at or 0,
                summary.session_id,
            ),
            reverse=True,
        )
        return tuple(summaries)

    async def append(
        self,
        session_id: str,
        expected_version: int,
        entries_to_append: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> int:
        """以 CAS 原子追加条目批次并更新活动叶。"""
        _validate_append(
            session_id,
            expected_version,
            entries_to_append,
            active_leaf_id,
        )
        self._require_initialized()
        encoded_entries = [
            encode_session_entry(entry) for entry in entries_to_append
        ]

        async with self._write_lock:
            try:
                async with self._database.transaction() as db_session:
                    result = await db_session.execute(
                        update(sessions)
                        .where(
                            sessions.c.session_id == session_id,
                            sessions.c.version == expected_version,
                        )
                        .values(
                            version=expected_version + 1,
                            active_leaf_id=active_leaf_id,
                        )
                    )
                    if result.rowcount == 1:
                        next_version = expected_version + 1
                        persisted = await db_session.execute(
                            select(entries.c.entry_id, entries.c.parent_id)
                            .where(entries.c.session_id == session_id)
                        )
                        existing_nodes = list(persisted.all())
                        last_sequence = await db_session.scalar(
                            select(func.max(entries.c.sequence)).where(
                                entries.c.session_id == session_id
                            )
                        )
                        first_sequence = (
                            0 if last_sequence is None else last_sequence + 1
                        )
                    else:
                        state = await _read_state(db_session, session_id)
                        if state is not None:
                            raise SessionVersionConflictError(
                                session_id,
                                expected_version,
                                state[0],
                            )
                        if expected_version != 0:
                            raise SessionVersionConflictError(
                                session_id,
                                expected_version,
                                0,
                            )
                        await db_session.execute(
                            insert(sessions).values(
                                session_id=session_id,
                                version=1,
                                active_leaf_id=active_leaf_id,
                            )
                        )
                        existing_nodes = []
                        first_sequence = 0
                        next_version = 1

                    _validate_tree_additions(
                        existing_nodes,
                        entries_to_append,
                        active_leaf_id,
                    )
                    await db_session.execute(
                        insert(entries),
                        [
                            {
                                "session_id": session_id,
                                "sequence": first_sequence + index,
                                "entry_id": entry.id,
                                "parent_id": entry.parent_id,
                                "payload": payload,
                            }
                            for index, (entry, payload) in enumerate(
                                zip(entries_to_append, encoded_entries, strict=True)
                            )
                        ],
                    )
            except SessionVersionConflictError:
                raise
            except IntegrityError as exc:
                actual_state = await self._read_current_state(session_id)
                observed_version = 0 if actual_state is None else actual_state[0]
                if observed_version != expected_version:
                    raise SessionVersionConflictError(
                        session_id,
                        expected_version,
                        observed_version,
                    ) from exc
                raise SessionStoreError(
                    "could not append Session entries atomically"
                ) from exc
            except SQLAlchemyError as exc:
                raise SessionStoreError(
                    "could not append Session entries atomically"
                ) from exc
        return next_version

    async def _read_current_state(self, session_id: str) -> tuple[int, str | None] | None:
        try:
            async with self._database.session() as db_session:
                return await _read_state(db_session, session_id)
        except SQLAlchemyError as exc:
            raise SessionStoreError(
                "could not verify Session version after append failure"
            ) from exc

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise SessionStoreError(
                "initialize the SQLAlchemy SessionStore before use"
            )


async def _read_state(
    db_session: AsyncSession,
    session_id: str,
) -> tuple[int, str | None] | None:
    result = await db_session.execute(
        select(sessions.c.version, sessions.c.active_leaf_id).where(
            sessions.c.session_id == session_id
        )
    )
    row = result.one_or_none()
    return None if row is None else (row[0], row[1])


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session ID must not be empty")


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit < 1:
        raise ValueError("session list limit must be a positive integer")


def _validate_append(
    session_id: str,
    expected_version: int,
    entries_to_append: Sequence[SessionEntry],
    active_leaf_id: str,
) -> None:
    _validate_session_id(session_id)
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected version must be a non-negative integer")
    if not isinstance(entries_to_append, Sequence) or isinstance(
        entries_to_append,
        (str, bytes),
    ):
        raise TypeError("entries must be a sequence of SessionEntry instances")
    if not entries_to_append:
        raise ValueError("append requires at least one entry")
    if any(not isinstance(entry, SessionEntry) for entry in entries_to_append):
        raise TypeError("entries must contain SessionEntry instances")
    if not isinstance(active_leaf_id, str) or not active_leaf_id.strip():
        raise ValueError("active leaf ID must not be empty")


def _validate_tree_additions(
    existing_nodes: Sequence[tuple[str, str | None]],
    additions: Sequence[SessionEntry],
    active_leaf_id: str,
) -> None:
    all_ids = {entry_id for entry_id, _ in existing_nodes}
    children = {
        parent_id for _, parent_id in existing_nodes if parent_id is not None
    }
    for entry in additions:
        if entry.id in all_ids:
            raise ValueError(f"duplicate session entry ID: {entry.id!r}")
        if entry.parent_id is not None and entry.parent_id not in all_ids:
            raise ValueError(
                f"entry {entry.id!r} has unknown or out-of-order parent "
                f"{entry.parent_id!r}"
            )
        if entry.parent_id is not None:
            children.add(entry.parent_id)
        all_ids.add(entry.id)
    if active_leaf_id not in all_ids or active_leaf_id in children:
        raise ValueError("active leaf must identify an existing leaf entry")
