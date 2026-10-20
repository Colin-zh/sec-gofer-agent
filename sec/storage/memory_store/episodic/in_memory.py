"""提供进程内 EpisodicStore adapter，供开发和测试使用。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sec._types.memory import EpisodicRecord, EpisodicStoreProtocol
from sec.storage.memory_store._search import keyword_terms, rank_by_terms


class InMemoryEpisodicStore(EpisodicStoreProtocol):
    """以进程内字典实现情景消息索引；检索走确定性关键词匹配。"""

    def __init__(self) -> None:
        self._records: dict[str, EpisodicRecord] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def index(self, records: Sequence[EpisodicRecord]) -> None:
        _validate_records(records)
        async with self._lock:
            for record in records:
                self._records[record.id] = record

    async def search(self, query: str, *, top_k: int) -> tuple[EpisodicRecord, ...]:
        _validate_query(query)
        _validate_top_k(top_k)
        terms = keyword_terms(query)
        async with self._lock:
            rows = [
                (record.id, record.text, record.timestamp)
                for record in self._records.values()
            ]
        ranked_ids = rank_by_terms(rows, terms)
        return tuple(
            self._records[item_id]
            for item_id in ranked_ids[:top_k]
            if item_id in self._records
        )

    async def list(self, *, limit: int) -> tuple[EpisodicRecord, ...]:
        _validate_top_k(limit)
        async with self._lock:
            records = list(self._records.values())
        records.sort(key=lambda record: (record.timestamp, record.id), reverse=True)
        return tuple(records[:limit])

    async def delete_session(self, session_id: str) -> None:
        _validate_session_id(session_id)
        async with self._lock:
            for record_id in [
                record_id
                for record_id, record in self._records.items()
                if record.session_id == session_id
            ]:
                self._records.pop(record_id, None)

    async def aclose(self) -> None:
        return None


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
