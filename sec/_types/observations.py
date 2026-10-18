"""Observed protocol events, session metadata, and usage summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from sec.exceptions import SecProtocolError
from sec._types.session import SessionEntry

RawRecord: TypeAlias = dict[str, Any]


@dataclass(frozen=True)
class Event:
    """未经转换的事件；raw 保留未知字段，且不出现在 repr 中。"""

    raw: RawRecord = field(repr=False)
    session_entry: SessionEntry | None = None

    @property
    def type(self) -> str:
        value = self.raw.get("type")
        return value if isinstance(value, str) else ""

    @property
    def text_delta(self) -> str | None:
        if self.type != "message_update":
            return None
        nested = self.raw.get("assistantMessageEvent")
        if isinstance(nested, dict) and nested.get("type") == "text_delta":
            delta = nested.get("delta")
            if not isinstance(delta, str):
                raise SecProtocolError("text_delta requires a string delta")
            return delta
        return None


@dataclass(frozen=True)
class SessionInfo:
    """会话身份快照；路径和名称不出现在 repr 中。"""

    session_id: str | None = None
    session_file: str | None = field(default=None, repr=False)
    session_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class UsageSummary:
    """观测到的助手用量，而非总账单；None 表示未知。

    推理 token 是 output_tokens 的子集，不得重复累加。
    只要任一观测到的助手消息缺少某项指标，该汇总字段就保持未知。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    assistant_messages: int = 0
