"""提供统一 MemoryStore 配置入口，将 semantic 与 episodic 委托给所选 adapter。

MemoryStore 不拥有共享 Database 与注入的 Embedder；应用负责其创建与关闭。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from sec._types.embedding import Embedder
from sec.storage.memory_store.episodic import (
    DatabaseEpisodicStore,
    FileEpisodicStore,
    InMemoryEpisodicStore,
)
from sec.storage.memory_store.semantic import (
    DatabaseSemanticStore,
    FileSemanticStore,
    InMemorySemanticStore,
)
from sec.storage.resources.database import Database

MemoryStoreBackend = Literal["in_memory", "database", "file"]


class MemoryStore:
    """按后端配置创建 semantic/episodic adapters，并统一初始化与关闭。

    ``file`` 后端将记忆以 JSON 落盘到 ``workspace/.sec/``，供无数据库时在
    本地工作区持久化长期记忆。
    """

    def __init__(
        self,
        backend: MemoryStoreBackend = "in_memory",
        *,
        database: Database | None = None,
        embedder: Embedder | None = None,
        vector_dim: int = 1536,
        workspace: Path | None = None,
    ) -> None:
        if backend == "in_memory":
            if database is not None:
                raise ValueError("database is only valid for the database backend")
            if embedder is not None:
                raise ValueError("embedder is only valid for the database backend")
            self._semantic = InMemorySemanticStore()
            self._episodic = InMemoryEpisodicStore()
        elif backend == "file":
            if workspace is None:
                raise ValueError("file backend requires a workspace path")
            if database is not None or embedder is not None:
                raise ValueError(
                    "database and embedder are only valid for the database backend"
                )
            root = Path(workspace) / ".sec"
            self._semantic = FileSemanticStore(root / "memory-semantic.json")
            self._episodic = FileEpisodicStore(root / "memory-episodic.json")
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
    "FileEpisodicStore",
    "FileSemanticStore",
    "InMemoryEpisodicStore",
    "InMemorySemanticStore",
    "MemoryStore",
    "MemoryStoreBackend",
]
