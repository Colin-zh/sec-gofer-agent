"""提供进程内 SemanticStore adapter，供开发和测试使用。"""

from __future__ import annotations

import asyncio

from sec._types.memory import (
    MemoryEntry,
    SemanticStoreProtocol,
    memory_dedup_key,
)
from sec.storage.memory_store._search import keyword_terms, rank_by_terms


class InMemorySemanticStore(SemanticStoreProtocol):
    """以进程内字典实现语义记忆存储；检索走确定性关键词匹配。"""

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._dedup: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def add(self, entry: MemoryEntry) -> str:
        _validate_entry(entry)
        key = (entry.scope, entry.kind, entry.dedup_key)
        async with self._lock:
            existing = self._dedup.get(key)
            if existing is not None:
                return existing
            self._entries[entry.id] = entry
            self._dedup[key] = entry.id
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
        terms = keyword_terms(query)
        async with self._lock:
            rows = [
                (entry.id, _searchable(entry), entry.updated_at)
                for entry in self._entries.values()
                if scope is None or entry.scope == scope
            ]
        ranked_ids = rank_by_terms(rows, terms)
        return tuple(
            self._entries[item_id] for item_id in ranked_ids[:top_k] if item_id in self._entries
        )

    async def list(
        self,
        *,
        limit: int,
        scope: str | None = None,
    ) -> tuple[MemoryEntry, ...]:
        _validate_top_k(limit)
        async with self._lock:
            entries = [
                entry
                for entry in self._entries.values()
                if scope is None or entry.scope == scope
            ]
        entries.sort(
            key=lambda entry: (entry.updated_at, entry.id),
            reverse=True,
        )
        return tuple(entries[:limit])

    async def update(
        self,
        memory_id: str,
        *,
        content: str,
        subject: str | None = None,
    ) -> bool:
        _validate_id(memory_id)
        _validate_content(content)
        async with self._lock:
            entry = self._entries.get(memory_id)
            if entry is None:
                return False
            key = (entry.scope, entry.kind, entry.dedup_key)
            updated = _with_content(entry, content, subject)
            self._entries[memory_id] = updated
            self._dedup.pop(key, None)
            self._dedup[(updated.scope, updated.kind, updated.dedup_key)] = memory_id
            return True

    async def delete(self, memory_id: str) -> bool:
        _validate_id(memory_id)
        async with self._lock:
            entry = self._entries.pop(memory_id, None)
            if entry is None:
                return False
            self._dedup.pop((entry.scope, entry.kind, entry.dedup_key), None)
            return True

    async def aclose(self) -> None:
        return None


def _searchable(entry: MemoryEntry) -> str:
    subject = entry.subject or ""
    return f"{subject}\n{entry.content}"


def _with_content(
    entry: MemoryEntry,
    content: str,
    subject: str | None,
) -> MemoryEntry:
    from time import time

    now = time()
    return MemoryEntry(
        id=entry.id,
        content=content,
        kind=entry.kind,
        source=entry.source,
        created_at=entry.created_at,
        updated_at=now,
        dedup_key=memory_dedup_key(subject, content),
        subject=subject,
        scope=entry.scope,
    )


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
