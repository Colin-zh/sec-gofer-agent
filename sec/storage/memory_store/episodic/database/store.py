"""使用共享 Database 资源持久化情景消息索引。

SQLite 后端走 FTS5（不可用时回退 LIKE）；opengauss / postgresql 后端走
ILIKE 关键词 + 原生 vector ANN，再用 RRF 融合。Database 拥有 Engine/连接池并
由应用统一关闭；本 adapter 不拥有注入的 Embedder。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy import delete, insert, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from sec._types.embedding import Embedder
from sec._types.memory import EpisodicRecord, EpisodicStoreProtocol
from sec.exceptions import MemoryStoreError
from sec.storage.memory_store._search import (
    fts5_query,
    keyword_terms,
    rank_by_terms,
    rrf_fuse,
)
from sec.storage.memory_store._vector import (
    encode_embedding,
    is_vector_dialect,
)
from sec.storage.memory_store.episodic.database.schema import (
    FTS_TABLE,
    VECTOR_COLUMN,
    create_or_upgrade_schema,
    episodes,
)
from sec.storage.resources.database import Database


class DatabaseEpisodicStore(EpisodicStoreProtocol):
    """将情景消息索引写入 SQLAlchemy 支持的关系型数据库。"""

    def __init__(
        self,
        database: Database,
        *,
        embedder: Embedder | None = None,
        vector_dim: int = 1536,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database instance")
        self._database = database
        self._embedder = embedder
        self._vector_dim = vector_dim
        self._initialized = False
        self._dialect = ""
        self._vector = False
        self._fts = False
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """创建缺失的表并按方言补齐索引；不创建或关闭共享 Database。"""
        async with self._initialize_lock:
            if self._initialized:
                return
            try:
                self._dialect = await self._database.run_sync(
                    lambda connection: create_or_upgrade_schema(
                        connection, vector_dim=self._vector_dim
                    )
                )
            except SQLAlchemyError as exc:
                raise MemoryStoreError(
                    "could not initialize MemoryStore episodic schema: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._vector = is_vector_dialect(self._dialect)
            self._fts = self._dialect == "sqlite"
            if self._vector and self._embedder is None:
                raise MemoryStoreError(
                    "vector backend requires an Embedder for episodic memory"
                )
            self._initialized = True

    async def index(self, records: Sequence[EpisodicRecord]) -> None:
        _validate_records(records)
        if not records:
            return
        self._require_initialized()
        embeddings: list[str] | None = None
        if self._vector:
            vectors = await self._embedder.embed([record.text for record in records])
            embeddings = [encode_embedding(vector) for vector in vectors]

        async with self._write_lock:
            try:
                async with self._database.transaction() as db:
                    for index, record in enumerate(records):
                        embedding_json = (
                            embeddings[index] if embeddings is not None else None
                        )
                        # 同 id 覆盖：先删后插，保持 FTS 与向量索引一致。
                        await db.execute(delete(episodes).where(episodes.c.id == record.id))
                        if self._fts:
                            await db.execute(
                                text(f"DELETE FROM {FTS_TABLE} WHERE episode_id = :id"),
                                {"id": record.id},
                            )
                        await db.execute(
                            insert(episodes).values(
                                {
                                    "id": record.id,
                                    "session_id": record.session_id,
                                    "entry_id": record.entry_id,
                                    "text": record.text,
                                    "timestamp": record.timestamp,
                                    "kind": record.kind,
                                }
                            )
                        )
                        if self._fts:
                            await db.execute(
                                text(
                                    f"INSERT INTO {FTS_TABLE}(episode_id, searchable) "
                                    "VALUES (:id, :t)"
                                ),
                                {"id": record.id, "t": record.text},
                            )
                        if self._vector:
                            await db.execute(
                                text(
                                    f"UPDATE {episodes.name} SET {VECTOR_COLUMN} = "
                                    "CAST(CAST(:v AS text) AS vector) WHERE id = :id"
                                ),
                                {"v": embedding_json, "id": record.id},
                            )
            except SQLAlchemyError as exc:
                raise MemoryStoreError("could not index episodic records") from exc

    async def search(self, query: str, *, top_k: int) -> tuple[EpisodicRecord, ...]:
        _validate_query(query)
        _validate_top_k(top_k)
        self._require_initialized()
        try:
            ids = await self._search_ids(query, top_k)
            if not ids:
                return ()
            async with self._database.session() as db:
                rows = (await db.execute(
                    select(episodes).where(episodes.c.id.in_(ids))
                )).all()
            by_id = {row.id: row for row in rows}
            return tuple(_row_to_record(by_id[i]) for i in ids if i in by_id)
        except SQLAlchemyError as exc:
            raise MemoryStoreError("could not search episodic memory") from exc

    async def list(self, *, limit: int) -> tuple[EpisodicRecord, ...]:
        _validate_top_k(limit)
        self._require_initialized()
        try:
            async with self._database.session() as db:
                rows = (await db.execute(
                    select(episodes)
                    .order_by(episodes.c.timestamp.desc(), episodes.c.id)
                    .limit(limit)
                )).all()
            return tuple(_row_to_record(row) for row in rows)
        except SQLAlchemyError as exc:
            raise MemoryStoreError("could not list episodic memory") from exc

    async def delete_session(self, session_id: str) -> None:
        _validate_session_id(session_id)
        self._require_initialized()
        async with self._write_lock:
            try:
                async with self._database.transaction() as db:
                    ids = list(
                        (await db.execute(
                            select(episodes.c.id).where(
                                episodes.c.session_id == session_id
                            )
                        )).scalars()
                    )
                    if self._fts:
                        for episode_id in ids:
                            await db.execute(
                                text(f"DELETE FROM {FTS_TABLE} WHERE episode_id = :id"),
                                {"id": episode_id},
                            )
                    await db.execute(
                        delete(episodes).where(episodes.c.session_id == session_id)
                    )
            except SQLAlchemyError as exc:
                raise MemoryStoreError("could not delete episodic session") from exc

    async def aclose(self) -> None:
        return None

    async def _search_ids(self, query: str, top_k: int) -> list[str]:
        if self._vector:
            return await self._vector_search(query, top_k)
        terms = keyword_terms(query)
        if not terms:
            return []
        fts = fts5_query(query)
        async with self._database.session() as db:
            if fts:
                ids = await self._fts_ids(db, fts, top_k)
                if ids is not None:
                    return ids
            statement = select(
                episodes.c.id, episodes.c.text, episodes.c.timestamp
            )
            rows = (await db.execute(statement)).all()
            candidates = [(row.id, row.text, row.timestamp) for row in rows]
            return rank_by_terms(candidates, terms)[:top_k]

    async def _fts_ids(self, db, fts: str, top_k: int) -> list[str] | None:
        try:
            result = await db.execute(
                text(
                    f"SELECT episode_id FROM {FTS_TABLE} "
                    f"WHERE {FTS_TABLE} MATCH :q ORDER BY bm25({FTS_TABLE}) LIMIT :k"
                ),
                {"q": fts, "k": top_k},
            )
        except OperationalError:
            return None
        return list(result.scalars())

    async def _vector_search(self, query: str, top_k: int) -> list[str]:
        terms = keyword_terms(query)
        (query_embedding,) = await self._embedder.embed([query])
        query_vec = encode_embedding(query_embedding)
        async with self._database.session() as db:
            keyword_ids = await self._ilike_ids(db, terms, top_k * 3)
            ann_ids = await self._ann_ids(db, query_vec, top_k * 3)
        return rrf_fuse(keyword_ids, ann_ids, limit=top_k)

    async def _ilike_ids(self, db, terms: list[str], limit: int) -> list[str]:
        if not terms:
            return []
        statement = select(episodes.c.id).where(
            or_(*[episodes.c.text.ilike(f"%{term}%") for term in terms])
        )
        statement = statement.order_by(episodes.c.timestamp.desc()).limit(limit)
        return list((await db.execute(statement)).scalars())

    async def _ann_ids(self, db, query_vec: str, limit: int) -> list[str]:
        statement = text(
            f"SELECT id FROM {episodes.name} WHERE {VECTOR_COLUMN} IS NOT NULL "
            f"ORDER BY {VECTOR_COLUMN} <=> CAST(CAST(:q AS text) AS vector) LIMIT :k"
        )
        return list((await db.execute(statement, {"q": query_vec, "k": limit})).scalars())

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MemoryStoreError("initialize the MemoryStore before use")


def _row_to_record(row) -> EpisodicRecord:
    return EpisodicRecord(
        id=row.id,
        session_id=row.session_id,
        entry_id=row.entry_id,
        text=row.text,
        timestamp=row.timestamp,
        kind=row.kind,
    )


def _validate_records(records: Sequence[EpisodicRecord]) -> None:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise TypeError("records must be a sequence of EpisodicRecord instances")
    if any(not isinstance(record, EpisodicRecord) for record in records):
        raise TypeError("records must contain EpisodicRecord instances")


def _validate_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("search query must not be empty")


def _validate_top_k(top_k: int) -> None:
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session id must not be empty")
