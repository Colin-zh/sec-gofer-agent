"""提供统一配置入口，并将 SessionStore 调用委托给所选 adapter。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from sec._types.storage import SessionStoreProtocol
from sec.storage.resources.database import Database
from sec.storage.session_store.database import DatabaseSessionStore
from sec.storage.session_store.in_memory import InMemorySessionStore
from sec._types.session import SessionEntry, SessionSnapshot, SessionSummary

SessionStoreBackend = Literal["in_memory", "database"]


class SessionStore(SessionStoreProtocol):
    """按后端配置创建并委托 SessionStore adapter，不拥有共享 Database。"""

    def __init__(
        self,
        backend: SessionStoreBackend = "in_memory",
        *,
        database: Database | None = None,
    ) -> None:
        if backend == "in_memory":
            if database is not None:
                raise ValueError("database is only valid for the database backend")
            self._store: SessionStoreProtocol = InMemorySessionStore()
        elif backend == "database":
            if not isinstance(database, Database):
                raise TypeError("database backend requires a Database instance")
            self._store = DatabaseSessionStore(database)
        else:
            raise ValueError(f"unsupported SessionStore backend: {backend!r}")

    async def initialize(self) -> None:
        """初始化后端；内存后端无须初始化，数据库后端显式创建缺失的表。"""
        if isinstance(self._store, DatabaseSessionStore):
            await self._store.initialize()

    async def load(self, session_id: str) -> SessionSnapshot:
        """从当前后端读取会话快照。"""
        return await self._store.load(session_id)

    async def list_sessions(self, limit: int = 20) -> tuple[SessionSummary, ...]:
        """从当前后端列举最近更新的会话摘要。"""
        return await self._store.list_sessions(limit)

    async def append(
        self,
        session_id: str,
        expected_version: int,
        entries: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> int:
        """将条目批次和活动叶选择原子追加到当前后端。"""
        return await self._store.append(
            session_id,
            expected_version,
            entries,
            active_leaf_id,
        )
