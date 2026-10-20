"""提供 JSON 文件后端 SemanticStore adapter，用于无数据库时落盘到工作区。

与 InMemorySemanticStore 语义一致（确定性关键词检索、按 (scope, kind,
dedup_key) 幂等去重），但在每次变更后原子写回 JSON 文件，跨进程重启保留。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import time
from typing import Any

from sec._types.memory import (
    MemoryEntry,
    MemorySource,
    SemanticStoreProtocol,
    memory_dedup_key,
)
from sec.storage.memory_store._file import load_json, save_json
from sec.storage.memory_store._search import keyword_terms, rank_by_terms


class FileSemanticStore(SemanticStoreProtocol):
    """以 JSON 文件持久化语义记忆；检索走确定性关键词匹配。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, MemoryEntry] = {}
        self._dedup: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def initialize(self) -> None:
        async with self._lock:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        data = load_json(self._path)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            for item in data["entries"]:
                entry = _entry_from_dict(item)
                self._entries[entry.id] = entry
                self._dedup[(entry.scope, entry.kind, entry.dedup_key)] = entry.id
        self._loaded = True

    def _save(self) -> None:
        payload = {
            "version": 1,
            "entries": [_entry_to_dict(entry) for entry in self._entries.values()],
        }
        save_json(self._path, payload)

    async def add(self, entry: MemoryEntry) -> str:
        _validate_entry(entry)
        key = (entry.scope, entry.kind, entry.dedup_key)
        async with self._lock:
            self._ensure_loaded()
            existing = self._dedup.get(key)
            if existing is not None:
                return existing
            self._entries[entry.id] = entry
            self._dedup[key] = entry.id
            self._save()
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
            self._ensure_loaded()
            rows = [
                (entry.id, _searchable(entry), entry.updated_at)
                for entry in self._entries.values()
                if scope is None or entry.scope == scope
            ]
        ranked_ids = rank_by_terms(rows, terms)
        return tuple(
            self._entries[item_id]
            for item_id in ranked_ids[:top_k]
            if item_id in self._entries
        )

    async def list(
        self,
        *,
        limit: int,
        scope: str | None = None,
    ) -> tuple[MemoryEntry, ...]:
        _validate_top_k(limit)
        async with self._lock:
            self._ensure_loaded()
            entries = [
                entry
                for entry in self._entries.values()
                if scope is None or entry.scope == scope
            ]
        entries.sort(key=lambda entry: (entry.updated_at, entry.id), reverse=True)
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
            self._ensure_loaded()
            entry = self._entries.get(memory_id)
            if entry is None:
                return False
            key = (entry.scope, entry.kind, entry.dedup_key)
            updated = _with_content(entry, content, subject)
            self._entries[memory_id] = updated
            self._dedup.pop(key, None)
            self._dedup[(updated.scope, updated.kind, updated.dedup_key)] = memory_id
            self._save()
            return True

    async def delete(self, memory_id: str) -> bool:
        _validate_id(memory_id)
        async with self._lock:
            self._ensure_loaded()
            entry = self._entries.pop(memory_id, None)
            if entry is None:
                return False
            self._dedup.pop((entry.scope, entry.kind, entry.dedup_key), None)
            self._save()
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
    return MemoryEntry(
        id=entry.id,
        content=content,
        kind=entry.kind,
        source=entry.source,
        created_at=entry.created_at,
        updated_at=time(),
        dedup_key=memory_dedup_key(subject, content),
        subject=subject,
        scope=entry.scope,
    )


def _entry_to_dict(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "content": entry.content,
        "kind": entry.kind,
        "subject": entry.subject,
        "scope": entry.scope,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "dedup_key": entry.dedup_key,
        "source": {
            "origin": entry.source.origin,
            "session_id": entry.source.session_id,
            "entry_id": entry.source.entry_id,
        },
    }


def _entry_from_dict(data: object) -> MemoryEntry:
    if not isinstance(data, dict):
        raise ValueError("semantic memory entry must be an object")
    source = data.get("source")
    if not isinstance(source, dict):
        raise ValueError("semantic memory entry source must be an object")
    return MemoryEntry(
        id=data["id"],
        content=data["content"],
        kind=data["kind"],
        source=MemorySource(
            origin=source["origin"],
            session_id=source.get("session_id"),
            entry_id=source.get("entry_id"),
        ),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        dedup_key=data["dedup_key"],
        subject=data.get("subject"),
        scope=data.get("scope", "default"),
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
