"""规范会话历史树、状态和聚合统计类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Literal, TypeAlias

from .content import (
    FileContent,
    TextContent,
    ThinkingContent,
    ToolUseContent,
)
from .json import JSONValue
from .messages import Message, SystemMessage, Usage
from .models import Model, ThinkingLevel

QueueMode: TypeAlias = Literal["all", "one-at-a-time"]
StreamingBehavior: TypeAlias = Literal["steer", "followUp"]


@dataclass(frozen=True)
class _SessionEntryBase:
    """所有会话历史节点共享的标识和父链字段。"""

    id: str
    parent_id: str | None
    timestamp: float

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("session entry ID must not be empty")
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id.strip()
        ):
            raise ValueError("session entry parent ID must not be empty")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or not isfinite(self.timestamp)
        ):
            raise ValueError("session entry timestamp must be finite")


@dataclass(frozen=True)
class MessageEntry(_SessionEntryBase):
    """保存一条可投影到 Agent 消息上下文的消息。"""

    message: Message
    type: Literal["message"] = field(default="message", init=False)

    def __post_init__(self) -> None:
        _SessionEntryBase.__post_init__(self)
        if not isinstance(self.message, Message):
            raise TypeError("message entry must contain a Message")


@dataclass(frozen=True)
class ThinkingLevelChangeEntry(_SessionEntryBase):
    """记录会话思考级别变更。"""

    thinking_level: ThinkingLevel
    type: Literal["thinking_level_change"] = field(
        default="thinking_level_change",
        init=False,
    )


@dataclass(frozen=True)
class ModelChangeEntry(_SessionEntryBase):
    """记录会话 Provider 或模型变更。"""

    provider: str
    model_id: str
    type: Literal["model_change"] = field(default="model_change", init=False)


@dataclass(frozen=True)
class UsageEntry(_SessionEntryBase):
    """记录会话历史中的模型用量观察节点。"""

    kind: str
    provider: str
    model: str
    usage: Usage
    note: str | None = None
    type: Literal["usage"] = field(default="usage", init=False)


@dataclass(frozen=True)
class CompactionEntry(_SessionEntryBase):
    """记录上下文压缩结果，不替换其父链中的原始历史。"""

    summary: str
    first_kept_entry_id: str
    tokens_before: float
    details: JSONValue | None = None
    usage: Usage | None = None
    from_hook: bool | None = None
    system_message: SystemMessage | None = None
    type: Literal["compaction"] = field(default="compaction", init=False)


@dataclass(frozen=True)
class BranchSummaryEntry(_SessionEntryBase):
    """记录从指定历史节点生成的分支摘要。"""

    from_id: str
    summary: str
    details: JSONValue | None = None
    usage: Usage | None = None
    from_hook: bool | None = None
    type: Literal["branch_summary"] = field(default="branch_summary", init=False)


@dataclass(frozen=True)
class CustomEntry(_SessionEntryBase):
    """记录由扩展定义的结构化会话节点。"""

    custom_type: str
    data: JSONValue | None = None
    type: Literal["custom"] = field(default="custom", init=False)


@dataclass(frozen=True)
class CustomMessageEntry(_SessionEntryBase):
    """记录由扩展提供、可展示但不自动进入 Agent 消息序列的内容。"""

    custom_type: str
    content: str | list[TextContent | FileContent]
    display: bool
    details: JSONValue | None = None
    type: Literal["custom_message"] = field(default="custom_message", init=False)


ContextEditableContent: TypeAlias = (
    str | list[TextContent | FileContent | ThinkingContent | ToolUseContent]
)


@dataclass(frozen=True)
class ContextEditReplacement:
    """表示上下文编辑对目标消息内容的替换。"""

    content: ContextEditableContent


@dataclass(frozen=True)
class ContextEditEntry(_SessionEntryBase):
    """记录会话中对指定消息的上下文编辑。"""

    target_id: str
    replacement: ContextEditReplacement | None
    type: Literal["context_edit"] = field(default="context_edit", init=False)


@dataclass(frozen=True)
class LabelEntry(_SessionEntryBase):
    """记录对指定历史节点的标签变更。"""

    target_id: str
    label: str | None = None
    type: Literal["label"] = field(default="label", init=False)


@dataclass(frozen=True)
class SessionInfoEntry(_SessionEntryBase):
    """记录会话名称或其他会话信息变更。"""

    name: str | None = None
    type: Literal["session_info"] = field(default="session_info", init=False)


SessionEntry: TypeAlias = (
    MessageEntry
    | ThinkingLevelChangeEntry
    | ModelChangeEntry
    | UsageEntry
    | CompactionEntry
    | BranchSummaryEntry
    | CustomEntry
    | CustomMessageEntry
    | ContextEditEntry
    | LabelEntry
    | SessionInfoEntry
)


@dataclass(frozen=True)
class SessionSnapshot:
    """表示会话在一个已提交版本上的完整条目树和活动叶节点。"""

    session_id: str
    version: int
    entries: tuple[SessionEntry, ...]
    active_leaf_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session ID must not be empty")
        if type(self.version) is not int or self.version < 0:
            raise ValueError("session version must be a non-negative integer")
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))
        entry_ids: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, SessionEntry):
                raise TypeError("session snapshot entries must be SessionEntry instances")
            if entry.id in entry_ids:
                raise ValueError(f"duplicate session entry ID: {entry.id!r}")
            if entry.parent_id is not None and entry.parent_id not in entry_ids:
                raise ValueError("session entries must be ordered after their parents")
            entry_ids.add(entry.id)
        if self.active_leaf_id is not None and self.active_leaf_id not in entry_ids:
            raise ValueError("active leaf must identify an entry in the session tree")
        if bool(self.entries) != (self.active_leaf_id is not None):
            raise ValueError("a session tree must have exactly one active leaf")
        if self.active_leaf_id is not None:
            parent_ids = {entry.parent_id for entry in self.entries}
            if self.active_leaf_id in parent_ids:
                raise ValueError("active leaf must not have children")

    @property
    def active_path(self) -> tuple[SessionEntry, ...]:
        """以根到叶顺序返回当前活动分支中的全部历史节点。"""
        if self.active_leaf_id is None:
            return ()
        by_id = {entry.id: entry for entry in self.entries}
        path: list[SessionEntry] = []
        current = by_id[self.active_leaf_id]
        while True:
            path.append(current)
            if current.parent_id is None:
                break
            current = by_id[current.parent_id]
        path.reverse()
        return tuple(path)

    @property
    def messages(self) -> tuple[Message, ...]:
        """仅投影活动路径中的 MessageEntry 为 Agent 消息序列。"""
        return tuple(
            entry.message
            for entry in self.active_path
            if isinstance(entry, MessageEntry)
        )


@dataclass(frozen=True)
class SessionSummary:
    """列举会话时使用的轻量摘要。"""

    session_id: str
    version: int
    active_leaf_id: str | None
    name: str | None
    updated_at: float | None

    @classmethod
    def from_snapshot(cls, snapshot: SessionSnapshot) -> SessionSummary:
        """从活动分支构建摘要；空会话不作为可恢复记录展示。"""
        name: str | None = None
        for entry in snapshot.active_path:
            if isinstance(entry, SessionInfoEntry):
                name = entry.name
        timestamps = [entry.timestamp for entry in snapshot.entries]
        return cls(
            session_id=snapshot.session_id,
            version=snapshot.version,
            active_leaf_id=snapshot.active_leaf_id,
            name=name,
            updated_at=max(timestamps) if timestamps else None,
        )


@dataclass(frozen=True)
class SessionState:
    """以 sec snake_case 表示的活动会话状态快照。"""

    thinking_level: ThinkingLevel
    is_streaming: bool
    is_compacting: bool
    steering_mode: QueueMode
    follow_up_mode: QueueMode
    session_id: str
    auto_compaction_enabled: bool
    message_count: float
    pending_message_count: float
    model: Model | None = None
    session_file: str | None = None
    session_name: str | None = None


@dataclass(frozen=True)
class ContextUsage:
    """表示当前活动会话路径的上下文占用估算。"""

    tokens: float | None
    context_window: float
    percent: float | None


@dataclass(frozen=True)
class SessionTokens:
    """表示整段会话累计的 Provider token 用量。"""

    input_tokens: float
    output_tokens: float
    cache_read_tokens: float
    cache_write_tokens: float
    total_tokens: float


@dataclass(frozen=True)
class SessionStats:
    """表示整段会话聚合统计，与单次 Run 的用量结果相互独立。"""

    session_id: str
    user_messages: float
    assistant_messages: float
    tool_calls: float
    tool_results: float
    total_messages: float
    tokens: SessionTokens
    cost: float
    session_file: str | None = None
    context_usage: ContextUsage | None = None


__all__ = [
    "BranchSummaryEntry",
    "CompactionEntry",
    "ContextEditableContent",
    "ContextEditEntry",
    "ContextEditReplacement",
    "ContextUsage",
    "CustomEntry",
    "CustomMessageEntry",
    "LabelEntry",
    "MessageEntry",
    "ModelChangeEntry",
    "QueueMode",
    "SessionEntry",
    "SessionInfoEntry",
    "SessionSnapshot",
    "SessionSummary",
    "SessionState",
    "SessionStats",
    "SessionTokens",
    "StreamingBehavior",
    "ThinkingLevelChangeEntry",
    "UsageEntry",
]
