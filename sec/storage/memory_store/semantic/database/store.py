"""使用共享 Database 资源持久化语义记忆（抽取事实）。

SQLite 后端走 FTS5（不可用时回退 LIKE）；opengauss / postgresql 后端走
ILIKE 关键词 + 原生 vector ANN，再用 RRF 融合。Database 拥有 Engine/连接池并
由应用统一关闭；本 adapter 不拥有注入的 Embedder，也不在 aclose 时关闭它。
"""

from __future__ import annotations

import asyncio
from time import time

from sqlalchemy import delete, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from sec._types.embedding import Embedder
from sec._types.memory import MemoryEntry, MemorySource, SemanticStoreProtocol
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
from sec.storage.memory_store.semantic.database.schema import (
    FTS_TABLE,
    VECTOR_COLUMN,
    create_or_upgrade_schema,
    memories,
)
from sec.storage.resources.database import Database


class DatabaseSemanticStore(SemanticStoreProtocol):
    """将语义事实写入 SQLAlchemy 支持的关系型数据库。"""

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
                    "could not initialize MemoryStore semantic schema: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            self._vector = is_vector_dialect(self._dialect)
            self._fts = self._dialect == "sqlite"
            if self._vector and self._embedder is None:
                raise MemoryStoreError(
                    "vector backend requires an Embedder for semantic memory"
                )
            self._initialized = True

    async def add(self, entry: MemoryEntry) -> str:
        _validate_entry(entry)
        self._require_initialized()
        embedding_json = await self._embed_text(_searchable(entry))

        async with self._write_lock:
            try:
                async with self._database.transaction() as db:
                    existing = await db.scalar(
                        select(memories.c.id).where(
                            memories.c.scope == entry.scope,
                            memories.c.kind == entry.kind,
                            memories.c.dedup_key == entry.dedup_key,
                        )
                    )
                    if existing is not None:
                        return existing
                    await db.execute(insert(memories).values(_entry_values(entry)))
                    await self._sync_search(db, entry.id, _searchable(entry), embedding_json)
            except IntegrityError as exc:
                return await self._resolve_dedup_id(entry, exc)
            except SQLAlchemyError as exc:
                raise MemoryStoreError("could not add semantic memory") from exc
        return entry.id

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        scope: str | None = None,
    ) -> tuple[MemoryEntry, ...]:
        _validate_query(query)
        _validate_top_k(top_k)
        self._require_initialized()
        try:
            ids = await self._search_ids(query, top_k, scope)
            if not ids:
                return ()
            async with self._database.session() as db:
                rows = (await db.execute(
                    select(memories).where(memories.c.id.in_(ids))
                )).all()
            by_id = {row.id: row for row in rows}
            return tuple(_row_to_entry(by_id[i]) for i in ids if i in by_id)
        except SQLAlchemyError as exc:
            raise MemoryStoreError("could not search semantic memory") from exc

    async def list(
        self,
        *,
        limit: int,
        scope: str | None = None,
    ) -> tuple[MemoryEntry, ...]:
        _validate_top_k(limit)
        self._require_initialized()
        try:
            statement = select(memories).order_by(
                memories.c.updated_at.desc(), memories.c.id
            )
            if scope is not None:
                statement = statement.where(memories.c.scope == scope)
            async with self._database.session() as db:
                rows = (await db.execute(statement.limit(limit))).all()
            return tuple(_row_to_entry(row) for row in rows)
        except SQLAlchemyError as exc:
            raise MemoryStoreError("could not list semantic memory") from exc

    async def update(
        self,
        memory_id: str,
        *,
        content: str,
        subject: str | None = None,
    ) -> bool:
        _validate_id(memory_id)
        _validate_content(content)
        self._require_initialized()
        from sec._types.memory import memory_dedup_key

        new_dedup = memory_dedup_key(subject, content)
        embedding_json = await self._embed_text(
            f"{subject or ''}\n{content}"
        )

        async with self._write_lock:
            try:
                async with self._database.transaction() as db:
                    result = await db.execute(
                        update(memories)
                        .where(memories.c.id == memory_id)
                        .values(
                            content=content,
                            subject=subject,
                            dedup_key=new_dedup,
                            updated_at=time(),
                        )
                    )
                    if result.rowcount != 1:
                        return False
                    if self._fts:
                        await db.execute(
                            text(f"DELETE FROM {FTS_TABLE} WHERE memory_id = :id"),
                            {"id": memory_id},
                        )
                    await self._sync_search(
                        db, memory_id, f"{subject or ''}\n{content}", embedding_json
                    )
            except IntegrityError as exc:
                raise MemoryStoreError(
                    "could not update semantic memory: dedup key conflicts"
                ) from exc
            except SQLAlchemyError as exc:
                raise MemoryStoreError("could not update semantic memory") from exc
        return True

    async def delete(self, memory_id: str) -> bool:
        _validate_id(memory_id)
        self._require_initialized()
        async with self._write_lock:
            try:
                async with self._database.transaction() as db:
                    result = await db.execute(
                        delete(memories).where(memories.c.id == memory_id)
                    )
                    if result.rowcount != 1:
                        return False
                    if self._fts:
                        await db.execute(
                            text(f"DELETE FROM {FTS_TABLE} WHERE memory_id = :id"),
                            {"id": memory_id},
                        )
            except SQLAlchemyError as exc:
                raise MemoryStoreError("could not delete semantic memory") from exc
        return True

    async def aclose(self) -> None:
        return None

    async def _embed_text(self, text_value: str) -> str | None:
        if not self._vector:
            return None
        (embedding,) = await self._embedder.embed([text_value])
        return encode_embedding(embedding)

    async def _sync_search(self, db, memory_id: str, searchable: str, embedding_json: str | None) -> None:
        if self._fts:
            await db.execute(
                text(f"INSERT INTO {FTS_TABLE}(memory_id, searchable) VALUES (:id, :t)"),
                {"id": memory_id, "t": searchable},
            )
        if self._vector:
            await db.execute(
                text(
                    f"UPDATE {memories.name} SET {VECTOR_COLUMN} = "
                    "CAST(CAST(:v AS text) AS vector) WHERE id = :id"
                ),
                {"v": embedding_json, "id": memory_id},
            )

    async def _resolve_dedup_id(self, entry: MemoryEntry, exc: IntegrityError) -> str:
        try:
            async with self._database.session() as db:
                existing = await db.scalar(
                    select(memories.c.id).where(
                        memories.c.scope == entry.scope,
                        memories.c.kind == entry.kind,
                        memories.c.dedup_key == entry.dedup_key,
                    )
                )
        except SQLAlchemyError as inner:
            raise MemoryStoreError("could not add semantic memory") from exc
        if existing is None:
            raise MemoryStoreError("could not add semantic memory") from exc
        return existing

    async def _search_ids(self, query: str, top_k: int, scope: str | None) -> list[str]:
        if self._vector:
            return await self._vector_search(query, top_k, scope)
        return await self._keyword_search(query, top_k, scope)

    async def _keyword_search(self, query: str, top_k: int, scope: str | None) -> list[str]:
        terms = keyword_terms(query)
        if not terms:
            return []
        fts = fts5_query(query)
        async with self._database.session() as db:
            if fts:
                ids = await self._fts_ids(db, fts, top_k, scope)
                if ids is not None:
                    return ids
            return await self._like_ids(db, terms, top_k, scope)

    async def _fts_ids(self, db, fts: str, top_k: int, scope: str | None) -> list[str] | None:
        try:
            result = await db.execute(
                text(
                    f"SELECT memory_id FROM {FTS_TABLE} "
                    f"WHERE {FTS_TABLE} MATCH :q ORDER BY bm25({FTS_TABLE}) LIMIT :k"
                ),
                {"q": fts, "k": top_k},
            )
        except OperationalError:
            return None
        ids = list(result.scalars())
        if scope is None:
            return ids
        valid = set(
            (await db.execute(
                select(memories.c.id).where(
                    memories.c.id.in_(ids), memories.c.scope == scope
                )
            )).scalars()
        )
        return [item_id for item_id in ids if item_id in valid]

    async def _like_ids(self, db, terms: list[str], top_k: int, scope: str | None) -> list[str]:
        statement = select(memories.c.id, memories.c.subject, memories.c.content, memories.c.updated_at)
        if scope is not None:
            statement = statement.where(memories.c.scope == scope)
        rows = (await db.execute(statement)).all()
        candidates = [
            (row.id, f"{row.subject or ''}\n{row.content}", row.updated_at)
            for row in rows
        ]
        return rank_by_terms(candidates, terms)[:top_k]

    async def _vector_search(self, query: str, top_k: int, scope: str | None) -> list[str]:
        terms = keyword_terms(query)
        (query_embedding,) = await self._embedder.embed([query])
        query_vec = encode_embedding(query_embedding)
        async with self._database.session() as db:
            keyword_ids = await self._ilike_ids(db, terms, top_k * 3, scope)
            ann_ids = await self._ann_ids(db, query_vec, top_k * 3, scope)
        return rrf_fuse(keyword_ids, ann_ids, limit=top_k)

    async def _ilike_ids(self, db, terms: list[str], limit: int, scope: str | None) -> list[str]:
        if not terms:
            return []
        statement = select(memories.c.id).where(
            or_(
                *[
                    memories.c.subject.ilike(f"%{term}%")
                    | memories.c.content.ilike(f"%{term}%")
                    for term in terms
                ]
            )
        )
        if scope is not None:
            statement = statement.where(memories.c.scope == scope)
        statement = statement.order_by(memories.c.updated_at.desc()).limit(limit)
        return list((await db.execute(statement)).scalars())

    async def _ann_ids(self, db, query_vec: str, limit: int, scope: str | None) -> list[str]:
        scope_clause = "AND scope = :scope" if scope is not None else ""
        statement = text(
            f"SELECT id FROM {memories.name} "
            f"WHERE {VECTOR_COLUMN} IS NOT NULL {scope_clause} "
            f"ORDER BY {VECTOR_COLUMN} <=> CAST(CAST(:q AS text) AS vector) LIMIT :k"
        )
        params = {"q": query_vec, "k": limit}
        if scope is not None:
            params["scope"] = scope
        return list((await db.execute(statement, params)).scalars())

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MemoryStoreError("initialize the MemoryStore before use")


def _entry_values(entry: MemoryEntry) -> dict:
    return {
        "id": entry.id,
        "scope": entry.scope,
        "kind": entry.kind,
        "subject": entry.subject,
        "content": entry.content,
        "dedup_key": entry.dedup_key,
        "origin": entry.source.origin,
        "source_session_id": entry.source.session_id,
        "source_entry_id": entry.source.entry_id,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def _row_to_entry(row) -> MemoryEntry:
    return MemoryEntry(
        id=row.id,
        content=row.content,
        kind=row.kind,
        source=MemorySource(
            origin=row.origin,
            session_id=row.source_session_id,
            entry_id=row.source_entry_id,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        dedup_key=row.dedup_key,
        subject=row.subject,
        scope=row.scope,
    )


def _searchable(entry: MemoryEntry) -> str:
    subject = entry.subject or ""
    return f"{subject}\n{entry.content}"


def _validate_entry(entry: MemoryEntry) -> None:
    if not isinstance(entry, MemoryEntry):
        raise TypeError("entry must be a MemoryEntry instance")


def _validate_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("search query must not be empty")


def _validate_top_k(top_k: int) -> None:
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")


def _validate_id(memory_id: str) -> None:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ValueError("memory id must not be empty")


def _validate_content(content: str) -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("memory content must not be empty")
