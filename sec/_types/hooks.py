"""定义变换型 hook 的契约、聚合上下文与阻塞 UI 原语。

与旧的观察型 hook（BeforeTraceHook/AfterTraceHook/TraceEventHook）不同，本模块
定义的 hook 可以改写流经 Trace 的数据（消息、payload、工具参数、工具结果），
并具备 block / decline 等干预语义。聚合策略由 sec._runtime.hooks 负责。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from .events import CompactionReason
from .json import JSONObject, JSONValue
from .messages import AgentMessage, AssistantMessage, Message, ToolResultMessage
from .providers import ModelRequestOptions

HookName: TypeAlias = Literal[
    "before_trace",
    "before_drive",
    "before_trace_end",
    "transform_context",
    "before_request",
    "before_payload",
    "after_response",
    "before_tool",
    "after_tool",
    "before_compaction",
    "before_navigation",
]

HOOK_NAMES: tuple[HookName, ...] = (
    "before_trace",
    "before_drive",
    "before_trace_end",
    "transform_context",
    "before_request",
    "before_payload",
    "after_response",
    "before_tool",
    "after_tool",
    "before_compaction",
    "before_navigation",
)


# --- 阻塞 UI（extension → host） -------------------------------------------------


class BlockingUI(Protocol):
    """扩展在 hook 内可阻塞等待宿主渲染并回传的 UI 原语。"""

    async def select(
        self,
        title: str,
        options: list[str],
        *,
        timeout: float | None = None,
    ) -> str | None: ...

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool | None: ...

    async def input(
        self,
        title: str,
        *,
        placeholder: str | None = None,
        timeout: float | None = None,
    ) -> str | None: ...

    async def editor(
        self,
        title: str,
        *,
        prefill: str | None = None,
    ) -> str | None: ...

    async def notify(
        self,
        message: str,
        *,
        notify_type: str | None = None,
    ) -> None: ...


@dataclass
class HookContext:
    """传递给每个 hook handler 的稳定上下文及阻塞 UI。"""

    trace_id: str
    session_id: str
    agent_name: str
    ui: BlockingUI | None = None


# --- 各 hook 的 event / result --------------------------------------------------


@dataclass(frozen=True)
class BeforeTraceEvent:
    prompt: str


@dataclass(frozen=True)
class BeforeTraceResult:
    messages: tuple[Message, ...] | None = None


@dataclass(frozen=True)
class BeforeDriveEvent:
    operation: Literal["trace", "compaction"]


@dataclass(frozen=True)
class BeforeTraceEndEvent:
    messages: tuple[AgentMessage, ...]


@dataclass(frozen=True)
class BeforeTraceEndResult:
    follow_up: str | None = None


@dataclass(frozen=True)
class TransformContextEvent:
    messages: tuple[Message, ...]
    system_prompt: str


@dataclass(frozen=True)
class TransformContextResult:
    messages: tuple[Message, ...] | None = None
    system_prompt: str | None = None


@dataclass(frozen=True)
class BeforeRequestEvent:
    options: ModelRequestOptions


@dataclass(frozen=True)
class BeforeRequestResult:
    options: ModelRequestOptions | None = None


@dataclass(frozen=True)
class BeforePayloadEvent:
    payload: JSONObject


@dataclass(frozen=True)
class BeforePayloadResult:
    payload: JSONObject | None = None


@dataclass(frozen=True)
class AfterResponseEvent:
    message: AssistantMessage


@dataclass(frozen=True)
class AfterResponseResult:
    message: AssistantMessage | None = None


@dataclass(frozen=True)
class BlockResult:
    reason: str
    terminate: bool = False


@dataclass(frozen=True)
class BeforeToolEvent:
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue]


@dataclass(frozen=True)
class BeforeToolResult:
    args: dict[str, JSONValue] | None = None
    block: BlockResult | None = None


@dataclass(frozen=True)
class AfterToolEvent:
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue]
    result: ToolResultMessage


@dataclass(frozen=True)
class AfterToolResult:
    result: ToolResultMessage | None = None
    terminate: bool = False


@dataclass(frozen=True)
class BeforeCompactionEvent:
    reason: CompactionReason


@dataclass(frozen=True)
class BeforeCompactionResult:
    decline: bool = False
    summary: str | None = None


@dataclass(frozen=True)
class BeforeNavigationEvent:
    target_id: str


@dataclass(frozen=True)
class BeforeNavigationResult:
    decline: bool = False
    summary: str | None = None


HookHandler: TypeAlias = Callable[[Any, HookContext], Any | Awaitable[Any]]


__all__ = [
    "AfterResponseEvent",
    "AfterResponseResult",
    "AfterToolEvent",
    "AfterToolResult",
    "BeforeCompactionEvent",
    "BeforeCompactionResult",
    "BeforeDriveEvent",
    "BeforeNavigationEvent",
    "BeforeNavigationResult",
    "BeforePayloadEvent",
    "BeforePayloadResult",
    "BeforeRequestEvent",
    "BeforeRequestResult",
    "BeforeTraceEndEvent",
    "BeforeTraceEndResult",
    "BeforeTraceEvent",
    "BeforeTraceResult",
    "BeforeToolEvent",
    "BeforeToolResult",
    "BlockResult",
    "BlockingUI",
    "HOOK_NAMES",
    "HookContext",
    "HookHandler",
    "HookName",
    "TransformContextEvent",
    "TransformContextResult",
]
