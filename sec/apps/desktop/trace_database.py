"""Database-backed Desktop trace persistence with a synchronous read cache."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
from time import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from sec.apps.desktop.trace_store import TraceEventRecord, _json_text
from sec.storage.resources.database import Database

_metadata = MetaData()
_traces = Table(
    "sec_desktop_traces",
    _metadata,
    Column("trace_id", String(128), primary_key=True),
    Column("session_id", String(128), nullable=False, index=True),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float),
    Column("elapsed_ms", Float),
    Column("status", String(32), nullable=False),
)
_spans = Table(
    "sec_desktop_trace_spans",
    _metadata,
    Column("span_id", String(128), primary_key=True),
    Column(
        "trace_id",
        String(128),
        ForeignKey("sec_desktop_traces.trace_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "parent_id",
        String(128),
        ForeignKey("sec_desktop_trace_spans.span_id", ondelete="CASCADE"),
    ),
    Column("span_order", Integer, nullable=False),
    Column("kind", String(64), nullable=False),
    Column("label", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", Float, nullable=False),
    Column("ended_at", Float),
    Column("elapsed_ms", Float),
    Column("approval_wait_ms", Float),
    Column("details_json", Text, nullable=False),
    UniqueConstraint("trace_id", "span_order"),
)
_events = Table(
    "sec_desktop_trace_events",
    _metadata,
    Column("event_id", String(64), primary_key=True),
    Column(
        "trace_id",
        String(128),
        ForeignKey("sec_desktop_traces.trace_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "span_id",
        String(128),
        ForeignKey("sec_desktop_trace_spans.span_id", ondelete="SET NULL"),
    ),
    Column("event_order", Integer, nullable=False),
    Column("event_type", String(128), nullable=False),
    Column("occurred_at", Float, nullable=False),
    Column("elapsed_ms", Float, nullable=False),
    Column("payload_json", Text, nullable=False),
    UniqueConstraint("trace_id", "event_order"),
)
_migrations = Table(
    "sec_desktop_trace_migrations",
    _metadata,
    Column("source_path", Text, primary_key=True),
    Column("migrated_at", Float, nullable=False),
    Column("trace_count", Integer, nullable=False),
    Column("span_count", Integer, nullable=False),
    Column("event_count", Integer, nullable=False),
)
_TraceWrite = tuple[str, dict[str, Any]]


class DatabaseTraceStore:
    """Persist trace writes asynchronously while serving the Desktop UI from cache."""

    def __init__(self, database: Database, legacy_path: Path) -> None:
        self._database = database
        self._legacy_path = Path(legacy_path)
        self._traces: dict[str, dict[str, Any]] = {}
        self._spans: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[tuple[int, TraceEventRecord, str | None]]] = (
            defaultdict(list)
        )
        self._span_orders: dict[str, int] = defaultdict(int)
        self._event_orders: dict[str, int] = defaultdict(int)
        self._queue: asyncio.Queue[_TraceWrite | None] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._write_errors: list[Exception] = []

    async def initialize(self) -> None:
        await self._database.run_sync(_metadata.create_all)
        await self._migrate_legacy_file()
        await self._load_cache()
        self._queue = asyncio.Queue()
        self._writer = asyncio.create_task(self._write_loop())

    async def _migrate_legacy_file(self) -> None:
        path = self._legacy_path.expanduser().resolve()
        if not path.is_file():
            return
        snapshot = await asyncio.to_thread(self._read_legacy_file, path)
        source_path, trace_rows, span_rows, event_rows = snapshot
        async with self._database.transaction() as session:
            existing = await session.scalar(
                select(_migrations.c.source_path).where(
                    _migrations.c.source_path == source_path,
                )
            )
            if existing is not None:
                return

            existing_trace_ids = set(
                (
                    await session.scalars(
                        select(_traces.c.trace_id),
                    )
                ).all()
            )
            existing_span_ids = set(
                (
                    await session.scalars(
                        select(_spans.c.span_id),
                    )
                ).all()
            )
            span_maxima = await self._order_maxima(session, _spans, "span_order")
            event_maxima = await self._order_maxima(session, _events, "event_order")

            new_traces = [
                row for row in trace_rows
                if row["trace_id"] not in existing_trace_ids
            ]
            if new_traces:
                await session.execute(insert(_traces), new_traces)

            new_spans: list[dict[str, Any]] = []
            deferred_parents: list[tuple[str, str]] = []
            for source in span_rows:
                if source["span_id"] in existing_span_ids:
                    continue
                order = span_maxima.get(source["trace_id"], 0) + 1
                span_maxima[source["trace_id"]] = order
                parent_id = source["parent_id"]
                row = {**source, "span_order": order, "parent_id": None}
                new_spans.append(row)
                if parent_id is not None:
                    deferred_parents.append((source["span_id"], parent_id))
                existing_span_ids.add(source["span_id"])
            if new_spans:
                await session.execute(insert(_spans), new_spans)
                for span_id, parent_id in deferred_parents:
                    await session.execute(
                        update(_spans)
                        .where(_spans.c.span_id == span_id)
                        .values(parent_id=parent_id)
                    )

            new_events: list[dict[str, Any]] = []
            for source in event_rows:
                order = event_maxima.get(source["trace_id"], 0) + 1
                event_maxima[source["trace_id"]] = order
                source_event_id = f"{source_path}:{source['source_event_id']}"
                new_events.append({
                    "event_id": uuid5(NAMESPACE_URL, source_event_id).hex,
                    "trace_id": source["trace_id"],
                    "span_id": source["span_id"],
                    "event_order": order,
                    "event_type": source["event_type"],
                    "occurred_at": source["occurred_at"],
                    "elapsed_ms": source["elapsed_ms"],
                    "payload_json": _json_text(json.loads(source["payload_json"])),
                })
            if new_events:
                await session.execute(insert(_events), new_events)

            await session.execute(
                insert(_migrations).values(
                    source_path=source_path,
                    migrated_at=time(),
                    trace_count=len(new_traces),
                    span_count=len(new_spans),
                    event_count=len(new_events),
                )
            )

    @staticmethod
    async def _order_maxima(
        session: AsyncSession,
        table: Table,
        order_column: str,
    ) -> dict[str, int]:
        from sqlalchemy import func

        column = table.c[order_column]
        rows = await session.execute(
            select(table.c.trace_id, func.max(column)).group_by(table.c.trace_id)
        )
        return {trace_id: order or 0 for trace_id, order in rows}

    @staticmethod
    def _read_legacy_file(
        path: Path,
    ) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        uri = path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "traces" not in tables:
                raise RuntimeError(
                    f"legacy trace database has no traces table: {path}"
                )
            traces = [
                dict(row)
                for row in connection.execute(
                    "SELECT trace_id, session_id, started_at, finished_at, "
                    "elapsed_ms, status FROM traces"
                )
            ]
            spans: list[dict[str, Any]] = []
            if "trace_spans" in tables:
                for row in connection.execute(
                    "SELECT rowid AS source_order, span_id, trace_id, parent_id, "
                    "kind, label, status, started_at, ended_at, elapsed_ms, "
                    "approval_wait_ms, details_json FROM trace_spans "
                    "ORDER BY trace_id, source_order"
                ):
                    item = dict(row)
                    item.pop("source_order", None)
                    item["details_json"] = _json_text(
                        json.loads(item["details_json"])
                    )
                    spans.append(item)
            events: list[dict[str, Any]] = []
            if "trace_events" in tables:
                for row in connection.execute(
                    "SELECT event_id AS source_event_id, trace_id, span_id, "
                    "event_type, occurred_at, elapsed_ms, payload_json "
                    "FROM trace_events ORDER BY trace_id, event_id"
                ):
                    events.append(dict(row))
        finally:
            connection.close()
        return str(path), traces, spans, events

    async def _load_cache(self) -> None:
        async with self._database.session() as session:
            trace_rows = (
                await session.execute(select(_traces).order_by(_traces.c.started_at))
            ).mappings().all()
            span_rows = (
                await session.execute(
                    select(_spans).order_by(_spans.c.trace_id, _spans.c.span_order)
                )
            ).mappings().all()
            event_rows = (
                await session.execute(
                    select(_events).order_by(
                        _events.c.trace_id,
                        _events.c.event_order,
                    )
                )
            ).mappings().all()
        self._traces = {row["trace_id"]: dict(row) for row in trace_rows}
        self._spans = {row["span_id"]: dict(row) for row in span_rows}
        self._events = defaultdict(list)
        for row in event_rows:
            self._events[row["trace_id"]].append((
                row["event_order"],
                TraceEventRecord(
                    row["event_type"],
                    row["occurred_at"],
                    row["elapsed_ms"],
                    json.loads(row["payload_json"]),
                ),
                row["span_id"],
            ))
        self._span_orders = defaultdict(int)
        self._event_orders = defaultdict(int)
        for row in span_rows:
            self._span_orders[row["trace_id"]] = max(
                self._span_orders[row["trace_id"]],
                row["span_order"],
            )
        for row in event_rows:
            self._event_orders[row["trace_id"]] = max(
                self._event_orders[row["trace_id"]],
                row["event_order"],
            )

    def _enqueue(self, operation: str, row: dict[str, Any]) -> None:
        if self._queue is None:
            raise RuntimeError("database trace store is not initialized")
        self._queue.put_nowait((operation, row))

    def start_trace(self, trace_id: str, session_id: str, started_at: float) -> None:
        trace = {
            "trace_id": trace_id,
            "session_id": session_id,
            "started_at": started_at,
            "finished_at": None,
            "elapsed_ms": None,
            "status": "running",
        }
        self._traces[trace_id] = trace
        self._enqueue("insert_trace", trace.copy())
        self._span_orders[trace_id] = 1
        span = {
            "span_id": trace_id,
            "trace_id": trace_id,
            "parent_id": None,
            "span_order": 1,
            "kind": "trace",
            "label": f" Trace · {trace_id[:8]}",
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "elapsed_ms": None,
            "approval_wait_ms": None,
            "details_json": _json_text({"session_id": session_id}),
        }
        self._spans[trace_id] = span
        self._enqueue("insert_span", span.copy())

    def create_span(
        self,
        trace_id: str,
        parent_id: str,
        kind: str,
        label: str,
        started_at: float,
        *,
        status: str = "running",
        details: dict[str, Any] | None = None,
    ) -> str:
        span_id = uuid4().hex
        self._span_orders[trace_id] += 1
        span = {
            "span_id": span_id,
            "trace_id": trace_id,
            "parent_id": parent_id,
            "span_order": self._span_orders[trace_id],
            "kind": kind,
            "label": label,
            "status": status,
            "started_at": started_at,
            "ended_at": None,
            "elapsed_ms": None,
            "approval_wait_ms": None,
            "details_json": _json_text(details or {}),
        }
        self._spans[span_id] = span
        self._enqueue("insert_span", span.copy())
        return span_id

    def update_span(
        self,
        span_id: str,
        *,
        label: str | None = None,
        status: str | None = None,
        ended_at: float | None = None,
        elapsed_ms: float | None = None,
        approval_wait_ms: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        span = self._spans.get(span_id)
        if span is None:
            raise KeyError(f"Trace span not found: {span_id}")
        if details:
            current = json.loads(span["details_json"])
            current.update(details)
            span["details_json"] = _json_text(current)
        for name, value in (
            ("label", label),
            ("status", status),
            ("ended_at", ended_at),
            ("elapsed_ms", elapsed_ms),
            ("approval_wait_ms", approval_wait_ms),
        ):
            if value is not None:
                span[name] = value
        self._enqueue("update_span", span.copy())

    def finish_trace(
        self,
        trace_id: str,
        *,
        finished_at: float,
        elapsed_ms: float,
        status: str,
    ) -> None:
        trace = self._traces.get(trace_id)
        if trace is None:
            raise KeyError(f"Trace not found: {trace_id}")
        trace.update({
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "status": status,
        })
        self._enqueue("update_trace", trace.copy())
        self.update_span(
            trace_id,
            status=status,
            ended_at=finished_at,
            elapsed_ms=elapsed_ms,
        )

    def record_event(
        self,
        trace_id: str,
        span_id: str,
        event_type: str,
        occurred_at: float,
        elapsed_ms: float,
        payload: dict[str, Any],
    ) -> None:
        self._event_orders[trace_id] += 1
        row = {
            "event_id": uuid4().hex,
            "trace_id": trace_id,
            "span_id": span_id,
            "event_order": self._event_orders[trace_id],
            "event_type": event_type,
            "occurred_at": occurred_at,
            "elapsed_ms": elapsed_ms,
            "payload_json": _json_text(payload),
        }
        self._events[trace_id].append((
            row["event_order"],
            TraceEventRecord(event_type, occurred_at, elapsed_ms, json.loads(row["payload_json"])),
            span_id,
        ))
        self._enqueue("insert_event", row)

    def list_traces(
        self,
        limit: int = 200,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = (
            row for row in self._traces.values()
            if session_id is None or row["session_id"] == session_id
        )
        return [
            {
                key: row[key]
                for key in (
                    "trace_id", "session_id", "started_at",
                    "finished_at", "elapsed_ms", "status",
                )
            }
            for row in sorted(rows, key=lambda item: item["started_at"], reverse=True)[
                :max(1, min(limit, 1000))
            ]
        ]

    def get_tree(self, trace_id: str) -> list[dict[str, Any]]:
        spans = {
            row["span_id"]: {
                **row,
                "details": json.loads(row["details_json"]),
                "children": [],
            }
            for row in sorted(
                (
                    span for span in self._spans.values()
                    if span["trace_id"] == trace_id
                ),
                key=lambda span: (span["started_at"], span["span_order"]),
            )
        }
        for span in spans.values():
            span.pop("details_json", None)
        roots: list[dict[str, Any]] = []
        for span in spans.values():
            parent = spans.get(span["parent_id"])
            if parent is None:
                roots.append(span)
            else:
                parent["children"].append(span)
        return roots

    def get_span_events(self, trace_id: str, span_id: str) -> list[TraceEventRecord]:
        return [
            event
            for _order, event, event_span_id in sorted(
                self._events.get(trace_id, ()),
                key=lambda item: item[0],
            )
            if event_span_id == span_id
        ]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        trace = self._traces.get(trace_id)
        if trace is None:
            return None
        return {
            key: trace[key]
            for key in (
                "trace_id", "session_id", "started_at",
                "finished_at", "elapsed_ms", "status",
            )
        }

    async def _write_loop(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            first = await queue.get()
            if first is None:
                queue.task_done()
                return
            batch = [first]
            stop_after_batch = False
            while len(batch) < 100:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    stop_after_batch = True
                    break
                batch.append(item)
            try:
                async with self._database.transaction() as session:
                    for operation, row in batch:
                        await self._execute(session, operation, row)
            except Exception:
                for operation, row in batch:
                    try:
                        async with self._database.transaction() as session:
                            await self._execute(session, operation, row)
                    except Exception as error:
                        self._write_errors.append(error)
            finally:
                for _item in batch:
                    queue.task_done()
                if stop_after_batch:
                    queue.task_done()
            if stop_after_batch:
                return

    @staticmethod
    async def _execute(
        session: AsyncSession,
        operation: str,
        row: dict[str, Any],
    ) -> None:
        if operation == "insert_trace":
            await session.execute(insert(_traces).values(**row))
        elif operation == "insert_span":
            await session.execute(insert(_spans).values(**row))
        elif operation == "update_span":
            await session.execute(
                update(_spans)
                .where(_spans.c.span_id == row["span_id"])
                .values(**{
                    key: value for key, value in row.items()
                    if key not in {"span_id", "span_order"}
                })
            )
        elif operation == "update_trace":
            await session.execute(
                update(_traces)
                .where(_traces.c.trace_id == row["trace_id"])
                .values(**{
                    key: value for key, value in row.items()
                    if key != "trace_id"
                })
            )
        elif operation == "insert_event":
            await session.execute(insert(_events).values(**row))
        else:
            raise ValueError(f"unsupported trace database operation: {operation}")

    async def flush(self) -> None:
        queue = self._queue
        if queue is None:
            return
        await queue.join()
        if self._write_errors:
            errors, self._write_errors = self._write_errors, []
            raise ExceptionGroup("Desktop trace database writes failed", errors)

    async def aclose(self) -> None:
        queue = self._queue
        writer = self._writer
        if queue is None or writer is None:
            return
        flush_error: Exception | None = None
        try:
            await self.flush()
        except Exception as error:
            flush_error = error
        await queue.put(None)
        await writer
        self._queue = None
        self._writer = None
        if flush_error is not None:
            raise flush_error
