"""Local, redacted persistence for desktop traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4

from sec._types.events import (
    AgentEndEvent,
    AgentSettledEvent,
    AutoRetryEndEvent,
    BranchRetryAttemptEvent,
    CapabilitySnapshotEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    DelegationEvent,
    EmptySessionEvent,
    MessageBoundaryEvent,
    MessageUpdateEvent,
    ModelRequestEndEvent,
    ModelRequestStartEvent,
    QueueUpdateEvent,
    RetryStartEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolRetryEndEvent,
    ToolRetryStartEvent,
    TurnEndEvent,
)
from sec._types.messages import message_to_dict

_MAX_JSON_BYTES = 16_384
_MAX_TEXT_LENGTH = 4_096
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|"
    r"password|passwd|secret|credential|cookie|private[_-]?key|"
    r"environment|(?:^|[_-])env(?:ironment)?(?:$|[_-])|client[_-]?secret)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+[a-z0-9._~+/=-]{8,}|"
    r"(?:sk|rk|pk)-[a-z0-9_-]{16,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|secret)\s*[:=]\s*['\"]?[^\s,'\";]+)"
)


def redact_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded JSON-safe value with credentials removed."""
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth >= 8:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {
            str(child_key)[:128]: redact_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_value(item, depth=depth + 1)
            for item in value[:100]
        ]
    if isinstance(value, str):
        redacted = _SECRET_VALUE.sub("[REDACTED]", value)
        if len(redacted) > _MAX_TEXT_LENGTH:
            return redacted[:_MAX_TEXT_LENGTH] + "…[TRUNCATED]"
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[UNSUPPORTED {type(value).__name__}]"


def _json_text(value: Any, *, max_bytes: int = _MAX_JSON_BYTES) -> str:
    redacted = redact_value(value)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= max_bytes:
        return encoded
    preview = encoded.encode("utf-8")[: (max_bytes - 128) // 2].decode(
        "utf-8", errors="ignore",
    )
    return json.dumps(
        {"truncated": True, "preview": preview},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class TraceEventRecord:
    event_type: str
    occurred_at: float
    elapsed_ms: float
    payload: dict[str, Any] = field(default_factory=dict)


class TraceStore:
    """SQLite store scoped to a desktop workspace; no network access is used."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    elapsed_ms REAL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trace_spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                    parent_id TEXT REFERENCES trace_spans(span_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    elapsed_ms REAL,
                    approval_wait_ms REAL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trace_spans_order
                    ON trace_spans(trace_id, started_at, span_id);
                CREATE TABLE IF NOT EXISTS trace_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
                    span_id TEXT REFERENCES trace_spans(span_id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    elapsed_ms REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trace_events_order
                    ON trace_events(trace_id, event_id);
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start_trace(self, trace_id: str, session_id: str, started_at: float) -> None:
        details = {"session_id": session_id}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO traces(trace_id, session_id, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (trace_id, session_id, started_at),
            )
            connection.execute(
                """
                INSERT INTO trace_spans(
                    span_id, trace_id, parent_id, kind, label, status, started_at,
                    details_json
                ) VALUES (?, ?, NULL, 'trace', ?, 'running', ?, ?)
                """,
                (
                    trace_id,
                    trace_id,
                    f" Trace · {trace_id[:8]}",
                    started_at,
                    _json_text(details),
                ),
            )

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
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans(
                    span_id, trace_id, parent_id, kind, label, status, started_at,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    trace_id,
                    parent_id,
                    kind,
                    label,
                    status,
                    started_at,
                    _json_text(details or {}),
                ),
            )
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT details_json FROM trace_spans WHERE span_id = ?",
                (span_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Trace span not found: {span_id}")
            current = json.loads(row["details_json"])
            if details:
                current.update(details)
            connection.execute(
                """
                UPDATE trace_spans
                SET label = COALESCE(?, label),
                    status = COALESCE(?, status),
                    ended_at = COALESCE(?, ended_at),
                    elapsed_ms = COALESCE(?, elapsed_ms),
                    approval_wait_ms = COALESCE(?, approval_wait_ms),
                    details_json = ?
                WHERE span_id = ?
                """,
                (
                    label,
                    status,
                    ended_at,
                    elapsed_ms,
                    approval_wait_ms,
                    _json_text(current),
                    span_id,
                ),
            )

    def finish_trace(
        self,
        trace_id: str,
        *,
        finished_at: float,
        elapsed_ms: float,
        status: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE traces
                SET finished_at = ?, elapsed_ms = ?, status = ?
                WHERE trace_id = ?
                """,
                (finished_at, elapsed_ms, status, trace_id),
            )
            connection.execute(
                """
                UPDATE trace_spans
                SET status = ?, ended_at = ?, elapsed_ms = ?
                WHERE span_id = ?
                """,
                (status, finished_at, elapsed_ms, trace_id),
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
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_events(
                    trace_id, span_id, event_type, occurred_at, elapsed_ms, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    span_id,
                    event_type,
                    occurred_at,
                    elapsed_ms,
                    _json_text(payload),
                ),
            )

    def list_traces(
        self,
        limit: int = 200,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trace_id, session_id, started_at, finished_at, elapsed_ms, status
                FROM traces
                WHERE (? IS NULL OR session_id = ?)
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (session_id, session_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_tree(self, trace_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT span_id, trace_id, parent_id, kind, label, status, started_at,
                       ended_at, elapsed_ms, approval_wait_ms, details_json
                FROM trace_spans
                WHERE trace_id = ?
                ORDER BY started_at, rowid
                """,
                (trace_id,),
            ).fetchall()
        spans: dict[str, dict[str, Any]] = {}
        for row in rows:
            span = dict(row)
            span["details"] = json.loads(span.pop("details_json"))
            span["children"] = []
            spans[span["span_id"]] = span
        roots: list[dict[str, Any]] = []
        for span in spans.values():
            parent = spans.get(span["parent_id"])
            if parent is None:
                roots.append(span)
            else:
                parent["children"].append(span)
        return roots

    def get_span_events(self, trace_id: str, span_id: str) -> list[TraceEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_type, occurred_at, elapsed_ms, payload_json
                FROM trace_events
                WHERE trace_id = ? AND span_id = ?
                ORDER BY event_id
                """,
                (trace_id, span_id),
            ).fetchall()
        return [
            TraceEventRecord(
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                elapsed_ms=row["elapsed_ms"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trace_id, session_id, started_at, finished_at, elapsed_ms, status
                FROM traces WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        return None if row is None else dict(row)


def _usage_data(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    result: dict[str, Any] = {}
    for field_name in (
        "input",
        "output",
        "cache_read",
        "cache_write",
        "cache_write_1h",
        "reasoning",
        "total_tokens",
    ):
        value = getattr(usage, field_name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[field_name] = value
    cost = getattr(usage, "cost", None)
    total_cost = getattr(cost, "total", None)
    if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
        result["cost"] = total_cost
    return result or None


def _merge_trace_text_blocks(blocks: list[Any]) -> list[Any]:
    result: list[Any] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            result.append({"type": "text", "text": "".join(text)})
            text.clear()

    for block in blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block.keys() <= {"type", "text", "text_signature"}
        ):
            text.append(block["text"])
        else:
            flush()
            result.append(block)
    flush()
    return result


def _model_trace_value(value: Any, *, depth: int = 0) -> Any:
    """Exclude reasoning and binary data before applying the normal trace bounds."""
    if depth >= 8:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        if value.get("type") == "thinking":
            return {"type": "thinking", "omitted": True}
        if value.get("type") == "file":
            return {
                "type": "file",
                "data_type": value.get("data_type"),
                "omitted": True,
            }
        return {
            key: (
                {"omitted": True}
                if key == "attachments" and value.get("type") == "tool_result"
                else _model_trace_value(
                    _merge_trace_text_blocks(child)
                    if key == "content" and isinstance(child, list) else child,
                    depth=depth + 1,
                )
            )
            for key, child in list(value.items())[:100]
            if key not in {
                "thinking", "thinking_signature", "thought_signature", "text_signature",
            }
        }
    if isinstance(value, list):
        retained = value if len(value) <= 100 else value[:99]
        result = [_model_trace_value(child, depth=depth + 1) for child in retained]
        if len(retained) < len(value):
            result.append({
                "_trace_truncated": True, "omitted_items": len(value) - len(retained),
            })
        return result
    return value


def _json_size(value: Any) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


def _bounded_structure(
    value: Any, budget: int, *, preserve_recent: bool = False,
) -> Any:
    """Fit redacted JSON without serializing a partial document inside a string."""
    if _json_size(value) <= budget:
        return value
    if isinstance(value, str):
        suffix = "…[TRUNCATED]"
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if _json_size(value[:middle] + suffix) <= budget:
                low = middle
            else:
                high = middle - 1
        return value[:low] + suffix
    if isinstance(value, dict):
        result: dict[str, Any] = {"_trace_truncated": True}
        items = list(value.items())
        for index, (key, child) in enumerate(items):
            overhead = _json_size({key: None}) - 4
            remaining = budget - _json_size(result) - overhead - 1
            allowance = remaining // (len(items) - index)
            if allowance < 64:
                break
            result[key] = _bounded_structure(
                child, allowance, preserve_recent=key == "messages",
            )
        return result
    if isinstance(value, list):
        result_list: list[Any] = []
        count = min(len(value), max(1, budget // 512))
        selected = (
            [value[0], *value[-(count - 1):]]
            if preserve_recent and 1 < count < len(value) else value[:count]
        )
        marker = {"_trace_truncated": True, "omitted_items": len(value) - count}
        if budget - _json_size(marker) - 4 < count * 64:
            return [{"_trace_truncated": True, "omitted_items": len(value)}]
        for index, child in enumerate(selected):
            remaining = budget - _json_size(result_list) - _json_size(marker) - 2
            allowance = remaining // (count - index)
            result_list.append(_bounded_structure(child, allowance))
        if count < len(value):
            result_list.insert(1 if preserve_recent and count > 1 else count, marker)
        return result_list
    return {"_trace_truncated": True}


def _model_trace_payload(value: Any) -> Any:
    return _bounded_structure(
        redact_value(_model_trace_value(value)), _MAX_JSON_BYTES // 3,
    )


@dataclass
class TraceRecorder:
    """Translate observed Trace events into a bounded, non-chat trace tree."""

    store: TraceStore
    trace_id: str
    session_id: str
    started_at: float = field(default_factory=time.time)
    started_ns: int = field(default_factory=time.monotonic_ns)
    turn_spans: dict[str, str] = field(default_factory=dict)
    tool_spans: dict[str, str] = field(default_factory=dict)
    approval_spans: dict[str, str] = field(default_factory=dict)
    model_spans: dict[str, str] = field(default_factory=dict)
    model_attempt_spans: dict[tuple[str, int], str] = field(default_factory=dict)
    retry_spans: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    approval_started_ns: dict[str, int] = field(default_factory=dict)
    tool_started_ns: dict[str, int] = field(default_factory=dict)
    generic_retry_started_ns: dict[str, list[int]] = field(default_factory=dict)
    turn_started_ns: dict[str, int] = field(default_factory=dict)
    pending_turn_started_ns: int | None = None
    pending_turn_started_at: float | None = None
    current_turn_id: str | None = None
    turn_number: int = 0

    def __post_init__(self) -> None:
        self.store.start_trace(self.trace_id, self.session_id, self.started_at)

    def _clock(self) -> tuple[float, float]:
        return time.time(), (time.monotonic_ns() - self.started_ns) / 1_000_000

    def _turn(self, turn_id: str | None = None) -> str:
        key = turn_id or self.current_turn_id or f"observed-{self.turn_number or 1}"
        span_id = self.turn_spans.get(key)
        if span_id is None:
            self.turn_number += 1
            wall = self.pending_turn_started_at or time.time()
            started_ns = self.pending_turn_started_ns or time.monotonic_ns()
            span_id = self.store.create_span(
                self.trace_id,
                self.trace_id,
                "turn",
                f"轮次 {self.turn_number}",
                wall,
                details={"turn_id": key},
            )
            self.turn_spans[key] = span_id
            self.turn_started_ns[key] = started_ns
            self.current_turn_id = key
            self.pending_turn_started_at = None
            self.pending_turn_started_ns = None
        return span_id

    def _tool(self, call_id: str, name: str, wall: float) -> str:
        span_id = self.tool_spans.get(call_id)
        if span_id is None:
            span_id = self.store.create_span(
                self.trace_id,
                self._turn(),
                "tool",
                f"工具 · {name}",
                wall,
                details={"tool_call_id": call_id, "tool_name": name},
            )
            self.tool_spans[call_id] = span_id
        return span_id

    def _record(
        self,
        span_id: str,
        event_type: str,
        wall: float,
        elapsed: float,
        payload: dict[str, Any],
    ) -> None:
        self.store.record_event(
            self.trace_id, span_id, event_type, wall, elapsed, payload,
        )

    def record(self, event: object) -> None:
        wall, elapsed = self._clock()
        event_type = getattr(event, "type", type(event).__name__)
        span_id = self.trace_id
        payload: dict[str, Any] = {}

        if isinstance(event, EmptySessionEvent) and event.type == "turn_start":
            self.current_turn_id = None
            self.pending_turn_started_at = wall
            self.pending_turn_started_ns = time.monotonic_ns()
            payload = {"type": "turn_start"}
        elif isinstance(event, ModelRequestStartEvent):
            for retry_span, started_ns in self.retry_spans.pop("provider-retry", []):
                self.store.update_span(
                    retry_span,
                    status="completed",
                    ended_at=event.started_at,
                    elapsed_ms=(time.monotonic_ns() - started_ns) / 1_000_000,
                )
            key = (event.request_id, event.attempt)
            span_id = self.store.create_span(
                self.trace_id,
                self._turn(event.turn_id),
                "model",
                f"模型调用 · {'上下文压缩 · ' if event.purpose == 'compaction' else ''}"
                f"第 {event.attempt} 次",
                event.started_at,
                details={
                    "request_id": event.request_id,
                    "turn_id": event.turn_id,
                    "attempt": event.attempt,
                    "purpose": event.purpose,
                    "input": _model_trace_payload(event.input),
                },
            )
            self.model_attempt_spans[key] = span_id
            self.model_spans[f"{event.turn_id}:{event.request_id}:{event.attempt}"] = span_id
            payload = {
                "request_id": event.request_id,
                "attempt": event.attempt,
                "purpose": event.purpose,
            }
        elif isinstance(event, ModelRequestEndEvent):
            key = (event.request_id, event.attempt)
            span_id = self.model_attempt_spans[key]
            details: dict[str, Any] = {"error": event.error}
            label = None
            if event.response is not None:
                response = event.response
                output = message_to_dict(response)
                if response.metadata.get("usage_reported") is False:
                    output["usage"] = None
                details.update({
                    "provider": response.provider,
                    "model": response.model,
                    "api": response.api,
                    "response_model": response.response_model,
                    "response_id": response.response_id,
                    "stop_reason": response.stop_reason,
                    "usage": _usage_data(response.usage)
                    if response.metadata.get("usage_reported") is not False else None,
                    "output": _model_trace_payload(output),
                })
                label = "模型调用 · " + "/".join(
                    part for part in (response.provider, response.model) if part
                )
                if event.purpose == "compaction":
                    label += " · 上下文压缩"
                label += f" · 第 {event.attempt} 次"
            self.store.update_span(
                span_id,
                label=label,
                status=event.status,
                ended_at=event.ended_at,
                elapsed_ms=event.elapsed_ms,
                details=details,
            )
            payload = {
                "request_id": event.request_id,
                "attempt": event.attempt,
                "status": event.status,
                "error": event.error,
                "elapsed_ms": event.elapsed_ms,
            }
        elif isinstance(event, MessageBoundaryEvent):
            message = event.message
            role = message.role
            metadata = getattr(message, "metadata", {})
            raw_turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
            turn_id = raw_turn_id if isinstance(raw_turn_id, str) else None
            turn_span = self._turn(turn_id)
            span_id = turn_span
            payload = {"role": role, "turn_id": turn_id}
            if role == "assistant":
                observed_span = next(
                    (
                        candidate
                        for key, candidate in reversed(list(self.model_spans.items()))
                        if key.startswith(f"{turn_id or self.current_turn_id}:")
                        and candidate in self.model_attempt_spans.values()
                    ),
                    None,
                )
                if observed_span is not None:
                    self._record(observed_span, event_type, wall, elapsed, payload)
                    return
                model_key = f"{turn_id or self.current_turn_id}:{len(self.model_spans)}"
                if event.type == "message_start":
                    usage = _usage_data(getattr(message, "usage", None))
                    provider = getattr(message, "provider", None)
                    model = getattr(message, "model", None)
                    api = getattr(message, "api", None)
                    response_model = getattr(message, "response_model", None)
                    details = {
                        "provider": provider,
                        "model": model,
                        "api": api,
                        "response_model": response_model,
                        "input": "未记录：桌面 Trace 事件不暴露 Provider 请求输入。",
                        "output": "未记录：为避免复制聊天内容，Trace 不保存模型文本。",
                        "usage": usage,
                    }
                    label = "模型调用"
                    if provider or model:
                        label += " · " + "/".join(
                            str(part) for part in (provider, model) if part
                        )
                    model_span = self.store.create_span(
                        self.trace_id,
                        turn_span,
                        "model",
                        label,
                        wall,
                        status="completed",
                        details=details,
                    )
                    self.model_spans[model_key] = model_span
                    span_id = model_span
                else:
                    model_span = next(
                        (
                            candidate
                            for key, candidate in reversed(list(self.model_spans.items()))
                            if key.startswith(f"{turn_id or self.current_turn_id}:")
                        ),
                        turn_span,
                    )
                    if hasattr(message, "usage"):
                        self.store.update_span(
                            model_span,
                            details={"usage": _usage_data(message.usage)},
                        )
                    span_id = model_span
            self._record(span_id, event_type, wall, elapsed, payload)
            return
        elif isinstance(event, ToolExecutionStartEvent):
            span_id = self._tool(event.tool_call_id, event.tool_name, wall)
            self.tool_started_ns[event.tool_call_id] = time.monotonic_ns()
            payload = {
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "input": event.args,
            }
            self.store.update_span(span_id, details={"input": event.args})
        elif isinstance(event, ToolExecutionUpdateEvent):
            span_id = self._tool(event.tool_call_id, event.tool_name, wall)
            payload = {
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "partial_result": event.partial_result,
            }
            self.store.update_span(span_id, details={"partial_output": event.partial_result})
        elif isinstance(event, ToolExecutionEndEvent):
            span_id = self._tool(event.tool_call_id, event.tool_name, wall)
            payload = {
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "is_error": event.is_error,
                "output": event.result,
            }
            started = self.tool_started_ns.pop(event.tool_call_id, None)
            duration = (
                (time.monotonic_ns() - started) / 1_000_000
                if started is not None
                else None
            )
            self.store.update_span(
                span_id,
                status="failed" if event.is_error else "completed",
                ended_at=wall,
                elapsed_ms=duration,
                details={"output": event.result, "is_error": event.is_error},
            )
        elif isinstance(event, ToolApprovalRequestedEvent):
            tool_span = self._tool(event.tool_call_id, event.tool_name, wall)
            span_id = self.store.create_span(
                self.trace_id,
                tool_span,
                "approval",
                f"审批 · {event.tool_name}",
                wall,
                details={"tool_call_id": event.tool_call_id},
            )
            self.approval_spans[event.tool_call_id] = span_id
            self.approval_started_ns[event.tool_call_id] = time.monotonic_ns()
            payload = {
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "input": event.args,
                "reason": event.reason,
            }
        elif isinstance(event, ToolApprovalResolvedEvent):
            span_id = self.approval_spans.get(
                event.tool_call_id,
                self._tool(event.tool_call_id, event.tool_name, wall),
            )
            started = self.approval_started_ns.pop(event.tool_call_id, None)
            wait_ms = (
                (time.monotonic_ns() - started) / 1_000_000
                if started is not None
                else None
            )
            payload = {
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "outcome": event.outcome,
                "reason": event.reason,
            }
            self.store.update_span(
                span_id,
                status=event.outcome,
                ended_at=wall,
                elapsed_ms=wait_ms,
                approval_wait_ms=wait_ms,
                details={"outcome": event.outcome, "reason": event.reason},
            )
        elif isinstance(event, ToolRetryStartEvent):
            parent = self._tool(event.tool_call_id, event.tool_name, wall)
            span_id = self.store.create_span(
                self.trace_id,
                parent,
                "retry",
                f"工具重试 · 第 {event.attempt:g} 次",
                wall,
                details={
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "requested_delay_ms": event.delay_ms,
                    "error": event.error_message,
                },
            )
            self.retry_spans.setdefault(event.tool_call_id, []).append(
                (span_id, time.monotonic_ns()),
            )
            payload = {
                "tool_call_id": event.tool_call_id,
                "attempt": event.attempt,
                "max_attempts": event.max_attempts,
                "requested_delay_ms": event.delay_ms,
                "error": event.error_message,
            }
        elif isinstance(event, ToolRetryEndEvent):
            retries = self.retry_spans.get(event.tool_call_id, [])
            if retries:
                span_id, started = retries.pop()
                duration = (time.monotonic_ns() - started) / 1_000_000
                self.store.update_span(
                    span_id,
                    status="completed" if event.success else "failed",
                    ended_at=wall,
                    elapsed_ms=duration,
                    details={
                        "success": event.success,
                        "attempt": event.attempt,
                        "final_error": event.final_error,
                    },
                )
            else:
                span_id = self._tool(event.tool_call_id, event.tool_name, wall)
            payload = {
                "tool_call_id": event.tool_call_id,
                "success": event.success,
                "attempt": event.attempt,
                "final_error": event.final_error,
            }
        elif isinstance(event, RetryStartEvent):
            key = "provider-retry"
            span_id = self.store.create_span(
                self.trace_id,
                self._turn(),
                "retry",
                f"模型重试 · 第 {event.attempt:g} 次",
                wall,
                details={
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "requested_delay_ms": event.delay_ms,
                    "error": event.error_message,
                },
            )
            self.retry_spans.setdefault(key, []).append((span_id, time.monotonic_ns()))
            payload = {
                "attempt": event.attempt,
                "max_attempts": event.max_attempts,
                "requested_delay_ms": event.delay_ms,
                "error": event.error_message,
            }
        elif isinstance(event, AutoRetryEndEvent):
            retries = self.retry_spans.get("provider-retry", [])
            if retries:
                span_id, started = retries.pop()
                duration = (time.monotonic_ns() - started) / 1_000_000
                self.store.update_span(
                    span_id,
                    status=(
                        "completed" if event.success
                        else "cancelled" if event.final_error == "cancelled"
                        else "failed"
                    ),
                    ended_at=wall,
                    elapsed_ms=duration,
                    details={
                        "success": event.success,
                        "attempt": event.attempt,
                        "final_error": event.final_error,
                    },
                )
            payload = {
                "success": event.success,
                "attempt": event.attempt,
                "final_error": event.final_error,
            }
        elif isinstance(event, TurnEndEvent):
            span_id = self._turn()
            turn_key = self.current_turn_id or ""
            turn_started = self.turn_started_ns.get(turn_key)
            turn_duration = (
                (time.monotonic_ns() - turn_started) / 1_000_000
                if turn_started is not None
                else None
            )
            self.store.update_span(
                span_id,
                status="completed",
                ended_at=wall,
                elapsed_ms=turn_duration,
            )
            payload = {"status": "completed", "duration_ms": turn_duration}
        elif isinstance(event, CapabilitySnapshotEvent):
            payload = {
                "generation": event.generation, "agent_id": event.agent_id,
                "card_digest": event.card_digest,
                "loaded_skills": event.loaded_skills, "tool_names": event.tool_names,
            }
        elif isinstance(event, DelegationEvent):
            payload = {
                "parent_trace_id": event.parent_trace_id, "child_trace_id": event.child_trace_id,
                "child_session_id": event.child_session_id,
                "agent_id": event.agent_id, "status": event.status,
            }
        elif isinstance(event, AgentSettledEvent):
            payload = {"status": event.status}
        elif isinstance(event, AgentEndEvent):
            payload = {"will_retry": event.will_retry, "message_count": len(event.messages)}
        elif isinstance(event, QueueUpdateEvent):
            payload = {
                "steering_count": len(event.steering),
                "follow_up_count": len(event.follow_up),
            }
        elif isinstance(event, CompactionStartEvent):
            payload = {"reason": event.reason}
        elif isinstance(event, CompactionEndEvent):
            payload = {
                "reason": event.reason,
                "aborted": event.aborted,
                "will_retry": event.will_retry,
                "error": event.error_message,
            }
        elif isinstance(event, BranchRetryAttemptEvent):
            payload = {"source": event.source}
        elif isinstance(event, MessageUpdateEvent):
            inner = event.assistant_message_event
            if inner.type in {"done", "error"}:
                message = inner.message if inner.type == "done" else inner.error
                turn_id = getattr(message, "metadata", {}).get("turn_id")
                model_span = next(
                    (
                        candidate
                        for key, candidate in reversed(list(self.model_spans.items()))
                        if key.startswith(f"{turn_id or self.current_turn_id}:")
                    ),
                    None,
                )
                if model_span is not None:
                    span_id = model_span
                    if model_span not in self.model_attempt_spans.values():
                        self.store.update_span(
                            model_span,
                            status="failed" if inner.type == "error" else "completed",
                            details={"usage": _usage_data(event.usage)},
                        )
                payload = {
                    "assistant_event": inner.type,
                    "usage": _usage_data(event.usage),
                    "provider": getattr(message, "provider", None),
                    "model": getattr(message, "model", None),
                    "stop_reason": getattr(message, "stop_reason", None),
                }
            else:
                return
        else:
            return

        self._record(span_id, event_type, wall, elapsed, payload)

    def finish(self, status: str) -> None:
        finished_at = time.time()
        elapsed_ms = (time.monotonic_ns() - self.started_ns) / 1_000_000
        self.store.finish_trace(
            self.trace_id,
            finished_at=finished_at,
            elapsed_ms=elapsed_ms,
            status=status,
        )


def format_wall_time(value: float | None) -> str:
    if value is None:
        return "未记录"
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
