"""提供 JSON 文件后端 EpisodicStore adapter，用于无数据库时落盘到工作区。

与 InMemoryEpisodicStore 语义一致（确定性关键词检索、同 id 覆盖），但在每次
变更后原子写回 JSON 文件，跨进程重启保留。文件后端无向量检索，embedding 不落盘。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sec._types.memory import EpisodicRecord, EpisodicStoreProtocol
from sec.storage.memory_store._file import load_json, save_json
from sec.storage.memory_store._search import keyword_terms, rank_by_terms


class FileEpisodicStore(EpisodicStoreProtocol):
    """以 JSON 文件持久化情景消息索引；检索走确定性关键词匹配。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: dict[str, EpisodicRecord] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def initialize(self) -> None:
        async with self._lock:
            self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        data = load_json(self._path)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            for item in data["records"]:
                record = _record_from_dict(item)
                self._records[record.id] = record
        self._loaded = True

    def _save(self) -> None:
        payload = {
            "version": 1,
            "records": [_record_to_dict(record) for record in self._records.values()],
        }
        save_json(self._path, payload)

    async def index(self, records: Sequence[EpisodicRecord]) -> None:
        _validate_records(records)
        if not records:
            return
        async with self._lock:
            self._ensure_loaded()
            for record in records:
                self._records[record.id] = record
            self._save()

    async def search(self, query: str, *, top_k: int) -> tuple[EpisodicRecord, ...]:
        _validate_query(query)
        _validate_top_k(top_k)
        terms = keyword_terms(query)
        async with self._lock:
            self._ensure_loaded()
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
            self._ensure_loaded()
            records = list(self._records.values())
        records.sort(key=lambda record: (record.timestamp, record.id), reverse=True)
        return tuple(records[:limit])

    async def delete_session(self, session_id: str) -> None:
        _validate_session_id(session_id)
        async with self._lock:
            self._ensure_loaded()
            for record_id in [
                record_id
                for record_id, record in self._records.items()
                if record.session_id == session_id
            ]:
                self._records.pop(record_id, None)
            self._save()

    async def aclose(self) -> None:
        return None


def _record_to_dict(record: EpisodicRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "session_id": record.session_id,
        "entry_id": record.entry_id,
        "text": record.text,
        "timestamp": record.timestamp,
        "kind": record.kind,
    }


def _record_from_dict(data: object) -> EpisodicRecord:
    if not isinstance(data, dict):
        raise ValueError("episodic record must be an object")
    return EpisodicRecord(
        id=data["id"],
        session_id=data["session_id"],
        entry_id=data["entry_id"],
        text=data["text"],
        timestamp=data["timestamp"],
        kind=data.get("kind", "message"),
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
