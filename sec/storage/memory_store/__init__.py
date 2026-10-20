"""提供统一 MemoryStore 配置入口，将 semantic 与 episodic 委托给所选 adapter。

MemoryStore 不拥有共享 Database 与注入的 Embedder；应用负责其创建与关闭。
"""

from __future__ import annotations

from typing import Literal

from sec._types.embedding import Embedder
from sec.storage.memory_store.episodic import (
    DatabaseEpisodicStore,
    InMemoryEpisodicStore,
)
from sec.storage.memory_store.semantic import (
    DatabaseSemanticStore,
    InMemorySemanticStore,
)
from sec.storage.resources.database import Database

MemoryStoreBackend = Literal["in_memory", "database"]


class MemoryStore:
    """按后端配置创建 semantic/episodic adapters，并统一初始化与关闭。"""

    def __init__(
        self,
        backend: MemoryStoreBackend = "in_memory",
        *,
        database: Database | None = None,
        embedder: Embedder | None = None,
        vector_dim: int = 1536,
    ) -> None:
        if backend == "in_memory":
            if database is not None:
                raise ValueError("database is only valid for the database backend")
            if embedder is not None:
                raise ValueError("embedder is only valid for the database backend")
            self._semantic = InMemorySemanticStore()
            self._episodic = InMemoryEpisodicStore()
        elif backend == "database":
            if not isinstance(database, Database):
                raise TypeError("database backend requires a Database instance")
            self._semantic = DatabaseSemanticStore(
                database, embedder=embedder, vector_dim=vector_dim
            )
            self._episodic = DatabaseEpisodicStore(
                database, embedder=embedder, vector_dim=vector_dim
            )
        else:
            raise ValueError(f"unsupported MemoryStore backend: {backend!r}")

    @property
    def semantic(self):
        """返回语义记忆 adapter。"""
        return self._semantic

    @property
    def episodic(self):
        """返回情景消息索引 adapter。"""
        return self._episodic

    async def initialize(self) -> None:
        """初始化两个 adapter；内存后端无操作，数据库后端创建缺失的表与索引。"""
        await self._semantic.initialize()
        await self._episodic.initialize()

    async def aclose(self) -> None:
        """释放 adapter 自建资源；不关闭共享 Database 或注入的 Embedder。"""
        await self._semantic.aclose()
        await self._episodic.aclose()


__all__ = [
    "DatabaseEpisodicStore",
    "DatabaseSemanticStore",
    "InMemoryEpisodicStore",
    "InMemorySemanticStore",
    "MemoryStore",
    "MemoryStoreBackend",
]
