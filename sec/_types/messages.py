"""定义按消息角色区分的协议数据类及消息联合类型。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, TypeAlias, Union

from .content import (
    FileContent,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    content_from_dict,
    content_to_dict,
)
from .json import JSONObject, JSONValue

StopReason: TypeAlias = Literal[
    "pending", "stop", "length", "tool_use", "error", "aborted", "deferred"
]


@dataclass
class UsageCost:
    """表示 Provider 报告的分项费用。"""

    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float


@dataclass
class Usage:
    """表示 Provider 报告的 token 用量与费用。"""

    input: float
    output: float
    cache_read: float
    cache_write: float
    total_tokens: float
    cost: UsageCost
    cache_write_1h: float | None = None
    reasoning: float | None = None


@dataclass
class DeferredHandle:
    """表示尚未完成的 Provider 响应及其轮询信息。"""

    provider: str
    model_id: str
    api: str
    id: str
    expires_at: float | None = None
    poll_after_ms: float | None = None
    data: JSONValue | None = None


@dataclass
class DiagnosticError:
    """表示消息处理期间产生的诊断错误。"""

    message: str
    name: str | None = None
    stack: str | None = None
    code: str | float | None = None


@dataclass
class AssistantMessageDiagnostic:
    """表示一次助手响应的诊断记录。"""

    type: str
    timestamp: float
    error: DiagnosticError | None = None
    details: JSONObject | None = None


@dataclass
class MessageToolDefinition:
    """描述消息协议中的工具定义。"""

    name: str
    description: str
    parameters: JSONObject
    constrained_sampling: Literal[False] | JSONObject | None = None


@dataclass
class ToolReference:
    """引用已登记的工具名称。"""

    name: str


@dataclass
class SystemMessage:
    """表示系统指令及其工具配置变更。"""

    role: Literal["system"] = field(default="system", init=False)
    content: str | list[TextContent]
    timestamp: float
    metadata: JSONObject = field(default_factory=dict)
    sections: dict[str, str | None] | None = None
    tools_added: list[MessageToolDefinition] | None = None
    tools_removed: list[ToolReference] | None = None

    @property
    def text(self) -> str:
        return _content_text(self.content)

    @property
    def reasoning(self) -> str:
        return ""

    @property
    def tool_uses(self) -> list[ToolUseContent]:
        return []

    def has_tool_use(self) -> bool:
        return False


@dataclass
class UserMessage:
    """表示用户提交的文本或文件内容。"""

    role: Literal["user"] = field(default="user", init=False)
    content: str | list[TextContent | FileContent]
    timestamp: float
    metadata: JSONObject = field(default_factory=dict)

    @property
    def text(self) -> str:
        return _content_text(self.content)

    @property
    def reasoning(self) -> str:
        return ""

    @property
    def tool_uses(self) -> list[ToolUseContent]:
        return []

    def has_tool_use(self) -> bool:
        return False


@dataclass
class AssistantMessage:
    """表示助手响应、生成内容及 Provider 元数据。"""

    role: Literal["assistant"] = field(default="assistant", init=False)
    content: list[TextContent | ThinkingContent | ToolUseContent | ToolResultContent]
    api: str
    provider: str
    model: str
    usage: Usage
    stop_reason: StopReason
    timestamp: float
    metadata: JSONObject = field(default_factory=dict)
    response_model: str | None = None
    response_id: str | None = None
    provider_thinking_level: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    deferred: DeferredHandle | None = None
    error_message: str | None = None
    raw_stop_reason: str | None = None
    end_turn: bool | None = None

    @property
    def text(self) -> str:
        return _content_text(self.content)

    @property
    def reasoning(self) -> str:
        return "".join(
            block.thinking
            for block in self.content
            if isinstance(block, ThinkingContent)
        )

    @property
    def tool_uses(self) -> list[ToolUseContent]:
        return [
            block for block in self.content if isinstance(block, ToolUseContent)
        ]

    def has_tool_use(self) -> bool:
        return bool(self.tool_uses)


@dataclass
class ToolResultMessage:
    """表示与某次工具调用对应的执行结果。"""

    role: Literal["tool_result"] = field(default="tool_result", init=False)
    tool_call_id: str
    tool_name: str
    content: list[TextContent | FileContent]
    is_error: bool
    timestamp: float
    metadata: JSONObject = field(default_factory=dict)
    details: JSONValue | None = None
    usage: Usage | None = None
    added_tool_names: list[str] | None = None

    @property
    def text(self) -> str:
        return _content_text(self.content)

    @property
    def reasoning(self) -> str:
        return ""

    @property
    def tool_uses(self) -> list[ToolUseContent]:
        return []

    def has_tool_use(self) -> bool:
        return False


@dataclass
class BashExecutionMessage:
    """表示 Bash 命令的执行结果。"""

    role: Literal["bash_execution"] = field(default="bash_execution", init=False)
    command: str
    output: str
    cancelled: bool
    truncated: bool
    timestamp: float
    exit_code: float | None = None
    full_output_path: str | None = None
    exclude_from_context: bool | None = None


@dataclass
class CustomMessage:
    """表示由扩展或应用定义的自定义消息。"""

    role: Literal["custom"] = field(default="custom", init=False)
    custom_type: str
    content: str | list[TextContent | FileContent]
    display: bool
    timestamp: float
    details: JSONValue | None = None


@dataclass
class BranchSummaryMessage:
    """表示会话分支的摘要记录。"""

    role: Literal["branch_summary"] = field(default="branch_summary", init=False)
    summary: str
    from_id: str | None
    timestamp: float


@dataclass
class CompactionSummaryMessage:
    """表示上下文压缩后的摘要记录。"""

    role: Literal["compaction_summary"] = field(default="compaction_summary", init=False)
    summary: str
    tokens_before: float
    timestamp: float


Message = Union[
    SystemMessage,
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
]


def _content_text(
    content: str | Sequence[object],
) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block.text
        for block in content
        if isinstance(block, TextContent)
    )


AgentMessage = Union[
    Message,
    BashExecutionMessage,
    CustomMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
]


def message_to_dict(message: Message) -> dict[str, Any]:
    """将规范消息序列化为可 JSON 编码的字典。"""
    return _to_data(message)


def message_from_dict(data: Mapping[str, Any]) -> Message:
    """按角色恢复规范消息；未知角色或内容类型显式报错。"""
    role = _required_str(data, "role")
    metadata = _required_mapping(data, "metadata")
    raw_content = data.get("content")

    if role == "system":
        if isinstance(raw_content, str):
            system_content: str | list[TextContent] = raw_content
        else:
            parsed_content = [
                content_from_dict(_mapping(item))
                for item in _required_list(data, "content")
            ]
            if any(not isinstance(item, TextContent) for item in parsed_content):
                raise ValueError("system message content must contain only text")
            system_content = [
                item for item in parsed_content if isinstance(item, TextContent)
            ]
        sections = data.get("sections")
        tools_added = data.get("tools_added")
        tools_removed = data.get("tools_removed")
        return SystemMessage(
            content=system_content,
            timestamp=_required_number(data, "timestamp"),
            metadata=dict(metadata),
            sections=dict(sections) if isinstance(sections, Mapping) else None,
            tools_added=[
                _tool_definition(_mapping(item)) for item in tools_added
            ]
            if isinstance(tools_added, list)
            else None,
            tools_removed=[
                ToolReference(name=_required_str(_mapping(item), "name"))
                for item in tools_removed
            ]
            if isinstance(tools_removed, list)
            else None,
        )

    if role == "user":
        if isinstance(raw_content, str):
            user_content: str | list[TextContent | FileContent] = raw_content
        else:
            parsed_content = [
                content_from_dict(_mapping(item))
                for item in _required_list(data, "content")
            ]
            if any(
                not isinstance(item, (TextContent, FileContent))
                for item in parsed_content
            ):
                raise ValueError("user message content has an unsupported type")
            user_content = [
                item
                for item in parsed_content
                if isinstance(item, (TextContent, FileContent))
            ]
        return UserMessage(
            content=user_content,
            timestamp=_required_number(data, "timestamp"),
            metadata=dict(metadata),
        )

    if role == "assistant":
        usage_data = _required_mapping(data, "usage")
        diagnostics_data = data.get("diagnostics")
        deferred_data = data.get("deferred")
        parsed_content = [
            content_from_dict(_mapping(item))
            for item in _required_list(data, "content")
        ]
        if any(
            not isinstance(
                item,
                (TextContent, ThinkingContent, ToolUseContent, ToolResultContent),
            )
            for item in parsed_content
        ):
            raise ValueError("assistant message content has an unsupported type")
        return AssistantMessage(
            content=[
                item
                for item in parsed_content
                if isinstance(
                    item,
                    (TextContent, ThinkingContent, ToolUseContent, ToolResultContent),
                )
            ],
            api=_required_str(data, "api"),
            provider=_required_str(data, "provider"),
            model=_required_str(data, "model"),
            usage=_usage(usage_data),
            stop_reason=_stop_reason(data.get("stop_reason")),
            timestamp=_required_number(data, "timestamp"),
            metadata=dict(metadata),
            response_model=_optional_str(data, "response_model"),
            response_id=_optional_str(data, "response_id"),
            provider_thinking_level=_optional_str(data, "provider_thinking_level"),
            diagnostics=[
                _diagnostic(_mapping(item)) for item in diagnostics_data
            ]
            if isinstance(diagnostics_data, list)
            else None,
            deferred=_deferred(_mapping(deferred_data))
            if isinstance(deferred_data, Mapping)
            else None,
            error_message=_optional_str(data, "error_message"),
            raw_stop_reason=_optional_str(data, "raw_stop_reason"),
            end_turn=data.get("end_turn")
            if isinstance(data.get("end_turn"), bool)
            else None,
        )

    if role == "tool_result":
        raw_tool_content = _required_list(data, "content")
        parsed_tool_content = [
            content_from_dict(_mapping(item)) for item in raw_tool_content
        ]
        if any(
            not isinstance(item, (TextContent, FileContent))
            for item in parsed_tool_content
        ):
            raise ValueError("tool-result message content has an unsupported type")
        usage_data = data.get("usage")
        added_names = data.get("added_tool_names")
        return ToolResultMessage(
            tool_call_id=_required_str(data, "tool_call_id"),
            tool_name=_required_str(data, "tool_name"),
            content=[
                item
                for item in parsed_tool_content
                if isinstance(item, (TextContent, FileContent))
            ],
            is_error=_required_bool(data, "is_error"),
            timestamp=_required_number(data, "timestamp"),
            metadata=dict(metadata),
            details=data.get("details"),
            usage=_usage(_mapping(usage_data))
            if isinstance(usage_data, Mapping)
            else None,
            added_tool_names=[_require_string(item) for item in added_names]
            if isinstance(added_names, list)
            else None,
        )

    raise ValueError(f"unsupported message role: {role!r}")


def merge_text(messages: Sequence[Message]) -> str:
    """拼接消息序列中的文本内容。"""
    return "".join(message.text for message in messages)


def _usage(data: Mapping[str, Any]) -> Usage:
    cost = _required_mapping(data, "cost")
    return Usage(
        input=_required_number(data, "input"),
        output=_required_number(data, "output"),
        cache_read=_required_number(data, "cache_read"),
        cache_write=_required_number(data, "cache_write"),
        total_tokens=_required_number(data, "total_tokens"),
        cost=UsageCost(
            input=_required_number(cost, "input"),
            output=_required_number(cost, "output"),
            cache_read=_required_number(cost, "cache_read"),
            cache_write=_required_number(cost, "cache_write"),
            total=_required_number(cost, "total"),
        ),
        cache_write_1h=_optional_number(data, "cache_write_1h"),
        reasoning=_optional_number(data, "reasoning"),
    )


def _stop_reason(value: Any) -> StopReason:
    if value not in (
        "pending",
        "stop",
        "length",
        "tool_use",
        "error",
        "aborted",
        "deferred",
    ):
        raise ValueError(f"unsupported assistant stop reason: {value!r}")
    return value


def _diagnostic(data: Mapping[str, Any]) -> AssistantMessageDiagnostic:
    error = data.get("error")
    return AssistantMessageDiagnostic(
        type=_required_str(data, "type"),
        timestamp=_required_number(data, "timestamp"),
        error=_diagnostic_error(_mapping(error))
        if isinstance(error, Mapping)
        else None,
        details=dict(data["details"])
        if isinstance(data.get("details"), Mapping)
        else None,
    )


def _diagnostic_error(data: Mapping[str, Any]) -> DiagnosticError:
    code = data.get("code")
    return DiagnosticError(
        message=_required_str(data, "message"),
        name=_optional_str(data, "name"),
        stack=_optional_str(data, "stack"),
        code=code if isinstance(code, (str, int, float)) else None,
    )


def _deferred(data: Mapping[str, Any]) -> DeferredHandle:
    return DeferredHandle(
        provider=_required_str(data, "provider"),
        model_id=_required_str(data, "model_id"),
        api=_required_str(data, "api"),
        id=_required_str(data, "id"),
        expires_at=_optional_number(data, "expires_at"),
        poll_after_ms=_optional_number(data, "poll_after_ms"),
        data=data.get("data"),
    )


def _tool_definition(data: Mapping[str, Any]) -> MessageToolDefinition:
    return MessageToolDefinition(
        name=_required_str(data, "name"),
        description=_required_str(data, "description"),
        parameters=dict(_required_mapping(data, "parameters")),
        constrained_sampling=data.get("constrained_sampling"),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("expected an object")
    return value


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _mapping(data.get(key))


def _required_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _required_str(data: Mapping[str, Any], key: str) -> str:
    return _require_string(data.get(key))


def _require_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _required_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_number(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _optional_number(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number or null")
    return float(value)


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _to_data(value: Any) -> Any:
    if isinstance(
        value,
        (TextContent, ThinkingContent, FileContent, ToolUseContent, ToolResultContent),
    ):
        return content_to_dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _to_data(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        return {_to_data(key): _to_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_data(item) for item in value]
    return value
