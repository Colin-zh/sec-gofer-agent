"""Dataclass event types for assistant streams, sessions, and tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from .content import ToolUseContent
from .json import JSONObject, JSONValue
from .messages import AgentMessage, AssistantMessage, ToolResultMessage, Usage
from .models import ThinkingLevel
from .session import SessionEntry

CompactionReason: TypeAlias = Literal["manual", "threshold", "overflow"]
RunTerminalStatus: TypeAlias = Literal[
    "completed",
    "failed",
    "limit_reached",
    "cancelled",
]


@dataclass(frozen=True)
class CompactionResult:
    summary: str
    first_kept_entry_id: str
    tokens_before: float
    estimated_tokens_after: float | None = None
    usage: Usage | None = None
    details: JSONValue | None = None


@dataclass(frozen=True)
class QueueState:
    steering: list[str]
    follow_up: list[str]


@dataclass(frozen=True)
class AssistantStartEvent:
    type: Literal["start"] = field(default="start", init=False)


@dataclass(frozen=True)
class ContentStartEvent:
    type: Literal["text_start", "thinking_start"]
    content_index: float


@dataclass(frozen=True)
class ContentDeltaEvent:
    type: Literal["text_delta", "thinking_delta", "toolcall_delta"]
    content_index: float
    delta: str


@dataclass(frozen=True)
class ContentEndEvent:
    type: Literal["text_end", "thinking_end"]
    content_index: float
    content: str


@dataclass(frozen=True)
class ToolCallStartEvent:
    type: Literal["toolcall_start"] = field(default="toolcall_start", init=False)
    content_index: float
    id: str
    tool_name: str


@dataclass(frozen=True)
class ToolCallEndEvent:
    type: Literal["toolcall_end"] = field(default="toolcall_end", init=False)
    content_index: float
    tool_call: ToolUseContent


@dataclass(frozen=True)
class AssistantDoneEvent:
    type: Literal["done"] = field(default="done", init=False)
    reason: Literal["stop", "length", "tool_use", "deferred"]
    message: AssistantMessage


@dataclass(frozen=True)
class AssistantErrorEvent:
    type: Literal["error"] = field(default="error", init=False)
    reason: Literal["aborted", "error"]
    error: AssistantMessage


AssistantMessageEvent: TypeAlias = (
    AssistantStartEvent
    | ContentStartEvent
    | ContentDeltaEvent
    | ContentEndEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | AssistantDoneEvent
    | AssistantErrorEvent
)


@dataclass(frozen=True)
class EmptySessionEvent:
    type: Literal["agent_start", "turn_start", "summarization_retry_finished"]


@dataclass(frozen=True)
class AgentSettledEvent:
    type: Literal["agent_settled"] = field(default="agent_settled", init=False)
    status: RunTerminalStatus


@dataclass(frozen=True)
class AgentEndEvent:
    type: Literal["agent_end"] = field(default="agent_end", init=False)
    messages: list[AgentMessage]
    will_retry: bool


@dataclass(frozen=True)
class TurnEndEvent:
    type: Literal["turn_end"] = field(default="turn_end", init=False)
    message: AgentMessage
    tool_results: list[ToolResultMessage]


@dataclass(frozen=True)
class MessageBoundaryEvent:
    type: Literal["message_start", "message_end"]
    message: AgentMessage


@dataclass(frozen=True)
class MessageUpdateEvent:
    """A lightweight assistant update without accumulated message snapshots."""

    type: Literal["message_update"] = field(default="message_update", init=False)
    assistant_message_event: AssistantMessageEvent
    usage: Usage | None = None


@dataclass(frozen=True)
class ModelRequestStartEvent:
    """Snapshot of one actual Provider attempt, before dispatch (not HTTP wire data)."""

    type: Literal["model_request_start"] = field(default="model_request_start", init=False)
    request_id: str
    turn_id: str
    attempt: int
    purpose: Literal["response", "compaction"]
    started_at: float
    input: JSONObject


@dataclass(frozen=True)
class ModelRequestEndEvent:
    """Terminal outcome of an attempt; duration excludes retry backoff."""

    type: Literal["model_request_end"] = field(default="model_request_end", init=False)
    request_id: str
    turn_id: str
    attempt: int
    purpose: Literal["response", "compaction"]
    ended_at: float
    elapsed_ms: float
    status: Literal["completed", "failed", "cancelled"]
    response: AssistantMessage | None = None
    error: str | None = None


@dataclass(frozen=True)
class ToolExecutionStartEvent:
    type: Literal["tool_execution_start"] = field(
        default="tool_execution_start",
        init=False,
    )
    tool_call_id: str
    tool_name: str
    args: JSONValue


@dataclass(frozen=True)
class ToolExecutionUpdateEvent:
    type: Literal["tool_execution_update"] = field(
        default="tool_execution_update",
        init=False,
    )
    tool_call_id: str
    tool_name: str
    args: JSONValue
    partial_result: JSONValue


@dataclass(frozen=True)
class ToolExecutionEndEvent:
    type: Literal["tool_execution_end"] = field(
        default="tool_execution_end",
        init=False,
    )
    tool_call_id: str
    tool_name: str
    result: JSONValue
    is_error: bool


@dataclass(frozen=True)
class ToolApprovalRequestedEvent:
    type: Literal["tool_approval_requested"] = field(
        default="tool_approval_requested",
        init=False,
    )
    tool_call_id: str
    tool_name: str
    args: JSONValue
    reason: str | None = None


@dataclass(frozen=True)
class ToolApprovalResolvedEvent:
    type: Literal["tool_approval_resolved"] = field(
        default="tool_approval_resolved",
        init=False,
    )
    tool_call_id: str
    tool_name: str
    outcome: Literal["approved", "denied", "cancelled"]
    reason: str | None = None


@dataclass(frozen=True)
class ToolRetryStartEvent:
    type: Literal["tool_retry_start"] = field(default="tool_retry_start", init=False)
    tool_call_id: str
    tool_name: str
    attempt: float
    max_attempts: float
    delay_ms: float
    error_message: str


@dataclass(frozen=True)
class ToolRetryEndEvent:
    type: Literal["tool_retry_end"] = field(default="tool_retry_end", init=False)
    tool_call_id: str
    tool_name: str
    success: bool
    attempt: float
    final_error: str | None = None


@dataclass(frozen=True)
class QueueUpdateEvent(QueueState):
    type: Literal["queue_update"] = field(default="queue_update", init=False)


@dataclass(frozen=True)
class CompactionStartEvent:
    type: Literal["compaction_start"] = field(default="compaction_start", init=False)
    reason: CompactionReason


@dataclass(frozen=True)
class CompactionEndEvent:
    type: Literal["compaction_end"] = field(default="compaction_end", init=False)
    reason: CompactionReason
    aborted: bool
    will_retry: bool
    result: CompactionResult | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class EntryAppendedEvent:
    type: Literal["entry_appended"] = field(default="entry_appended", init=False)
    entry: SessionEntry


@dataclass(frozen=True)
class SessionInfoChangedEvent:
    type: Literal["session_info_changed"] = field(
        default="session_info_changed",
        init=False,
    )
    name: str | None = None


@dataclass(frozen=True)
class ThinkingLevelChangedEvent:
    type: Literal["thinking_level_changed"] = field(
        default="thinking_level_changed",
        init=False,
    )
    level: ThinkingLevel


@dataclass(frozen=True)
class RetryStartEvent:
    type: Literal["auto_retry_start", "summarization_retry_scheduled"]
    attempt: float
    max_attempts: float
    delay_ms: float
    error_message: str


@dataclass(frozen=True)
class AutoRetryEndEvent:
    type: Literal["auto_retry_end"] = field(default="auto_retry_end", init=False)
    success: bool
    attempt: float
    final_error: str | None = None


@dataclass(frozen=True)
class CompactionRetryAttemptEvent:
    type: Literal["summarization_retry_attempt_start"] = field(
        default="summarization_retry_attempt_start",
        init=False,
    )
    source: Literal["compaction"] = field(default="compaction", init=False)
    reason: CompactionReason


@dataclass(frozen=True)
class BranchRetryAttemptEvent:
    type: Literal["summarization_retry_attempt_start"] = field(
        default="summarization_retry_attempt_start",
        init=False,
    )
    source: Literal["branch_summary"] = field(default="branch_summary", init=False)


@dataclass(frozen=True)
class BashExecutionUpdateEvent:
    type: Literal["bash_execution_update"] = field(
        default="bash_execution_update",
        init=False,
    )
    delta: str
    id: str | None = None


SessionEvent: TypeAlias = (
    EmptySessionEvent
    | AgentSettledEvent
    | AgentEndEvent
    | TurnEndEvent
    | MessageBoundaryEvent
    | MessageUpdateEvent
    | ModelRequestStartEvent
    | ModelRequestEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | ToolApprovalRequestedEvent
    | ToolApprovalResolvedEvent
    | ToolRetryStartEvent
    | ToolRetryEndEvent
    | QueueUpdateEvent
    | CompactionStartEvent
    | CompactionEndEvent
    | EntryAppendedEvent
    | SessionInfoChangedEvent
    | ThinkingLevelChangedEvent
    | RetryStartEvent
    | AutoRetryEndEvent
    | CompactionRetryAttemptEvent
    | BranchRetryAttemptEvent
    | BashExecutionUpdateEvent
)
