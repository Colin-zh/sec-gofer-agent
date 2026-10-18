"""在规范 SessionEntry 与版本化 JSON 载荷之间安全编解码。

载荷包含显式 schema version 和已知内容块；不反序列化任意 Python 对象。JSON
不能表示的 metadata、损坏数据或未知载荷版本均转换为 SessionStoreError，供
不同 SessionStore 后端和可移植 Session 文件共用。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from sec._types.content import (
    FileContent,
    TextContent,
    ThinkingContent,
    ToolUseContent,
    content_from_dict,
    content_to_dict,
)
from sec._types.messages import (
    Message,
    SystemMessage,
    Usage,
    UsageCost,
    message_from_dict,
    message_to_dict,
)
from sec._types.session import (
    BranchSummaryEntry,
    CompactionEntry,
    ContextEditEntry,
    ContextEditableContent,
    ContextEditReplacement,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    ThinkingLevelChangeEntry,
    UsageEntry,
)
from sec.exceptions import SessionStoreError

_SCHEMA_VERSION = 4


def encode_session_entry(entry: SessionEntry) -> str:
    """将规范会话条目编码为带版本号的 snake_case JSON。"""
    try:
        data: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "type": entry.type,
            "id": entry.id,
            "parent_id": entry.parent_id,
            "timestamp": entry.timestamp,
        }
        if isinstance(entry, MessageEntry):
            data["message"] = message_to_dict(entry.message)
        elif isinstance(entry, ThinkingLevelChangeEntry):
            data["thinking_level"] = entry.thinking_level
        elif isinstance(entry, ModelChangeEntry):
            data.update(provider=entry.provider, model_id=entry.model_id)
        elif isinstance(entry, UsageEntry):
            data.update(
                kind=entry.kind,
                provider=entry.provider,
                model=entry.model,
                usage=_usage_to_data(entry.usage),
                note=entry.note,
            )
        elif isinstance(entry, CompactionEntry):
            data.update(
                summary=entry.summary,
                first_kept_entry_id=entry.first_kept_entry_id,
                tokens_before=entry.tokens_before,
                details=entry.details,
                usage=_usage_to_data(entry.usage) if entry.usage is not None else None,
                from_hook=entry.from_hook,
                system_message=(
                    message_to_dict(entry.system_message)
                    if entry.system_message is not None
                    else None
                ),
            )
        elif isinstance(entry, BranchSummaryEntry):
            data.update(
                from_id=entry.from_id,
                summary=entry.summary,
                details=entry.details,
                usage=_usage_to_data(entry.usage) if entry.usage is not None else None,
                from_hook=entry.from_hook,
            )
        elif isinstance(entry, CustomEntry):
            data.update(custom_type=entry.custom_type, data=entry.data)
        elif isinstance(entry, CustomMessageEntry):
            data.update(
                custom_type=entry.custom_type,
                content=_encode_content(entry.content),
                display=entry.display,
                details=entry.details,
            )
        elif isinstance(entry, ContextEditEntry):
            data.update(
                target_id=entry.target_id,
                replacement=(
                    {"content": _encode_content(entry.replacement.content)}
                    if entry.replacement is not None
                    else None
                ),
            )
        elif isinstance(entry, LabelEntry):
            data.update(target_id=entry.target_id, label=entry.label)
        elif isinstance(entry, SessionInfoEntry):
            data["name"] = entry.name
        else:
            raise TypeError(f"unsupported SessionEntry variant: {type(entry).__name__}")
        return json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise SessionStoreError(
            "Session entry contains data that cannot be serialized as JSON"
        ) from exc


def decode_session_entry(payload: str) -> SessionEntry:
    """校验并解码一条持久化的规范会话条目。"""
    try:
        envelope = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise SessionStoreError("stored Session entry is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise SessionStoreError("stored Session entry must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION:
        raise SessionStoreError("stored Session entry schema version is unsupported")
    try:
        common = {
            "id": envelope["id"],
            "parent_id": envelope.get("parent_id"),
            "timestamp": envelope["timestamp"],
        }
        entry_type = envelope.get("type")
        if entry_type == "message":
            return MessageEntry(**common, message=_decode_message(envelope["message"]))
        if entry_type == "thinking_level_change":
            return ThinkingLevelChangeEntry(
                **common,
                thinking_level=envelope["thinking_level"],
            )
        if entry_type == "model_change":
            return ModelChangeEntry(
                **common,
                provider=envelope["provider"],
                model_id=envelope["model_id"],
            )
        if entry_type == "usage":
            return UsageEntry(
                **common,
                kind=envelope["kind"],
                provider=envelope["provider"],
                model=envelope["model"],
                usage=_usage_from_data(envelope["usage"]),
                note=envelope.get("note"),
            )
        if entry_type == "compaction":
            raw_system_message = envelope.get("system_message")
            system_message = (
                _decode_message(raw_system_message)
                if raw_system_message is not None
                else None
            )
            if system_message is not None and not isinstance(system_message, SystemMessage):
                raise ValueError("compaction system_message must be a SystemMessage")
            raw_usage = envelope.get("usage")
            return CompactionEntry(
                **common,
                summary=envelope["summary"],
                first_kept_entry_id=envelope["first_kept_entry_id"],
                tokens_before=envelope["tokens_before"],
                details=envelope.get("details"),
                usage=_usage_from_data(raw_usage) if raw_usage is not None else None,
                from_hook=envelope.get("from_hook"),
                system_message=system_message,
            )
        if entry_type == "branch_summary":
            raw_usage = envelope.get("usage")
            return BranchSummaryEntry(
                **common,
                from_id=envelope["from_id"],
                summary=envelope["summary"],
                details=envelope.get("details"),
                usage=_usage_from_data(raw_usage) if raw_usage is not None else None,
                from_hook=envelope.get("from_hook"),
            )
        if entry_type == "custom":
            return CustomEntry(
                **common,
                custom_type=envelope["custom_type"],
                data=envelope.get("data"),
            )
        if entry_type == "custom_message":
            content = _decode_content(envelope["content"])
            if isinstance(content, list) and any(
                not isinstance(item, (TextContent, FileContent)) for item in content
            ):
                raise ValueError("custom message content has unsupported blocks")
            return CustomMessageEntry(
                **common,
                custom_type=envelope["custom_type"],
                content=(
                    content
                    if isinstance(content, str)
                    else cast(list[TextContent | FileContent], content)
                ),
                display=envelope["display"],
                details=envelope.get("details"),
            )
        if entry_type == "context_edit":
            raw_replacement = envelope.get("replacement")
            if raw_replacement is not None:
                if not isinstance(raw_replacement, Mapping):
                    raise ValueError("context edit replacement must be an object")
                replacement_content = _decode_content(raw_replacement["content"])
                replacement = ContextEditReplacement(
                    content=cast(ContextEditableContent, replacement_content)
                )
            else:
                replacement = None
            return ContextEditEntry(
                **common,
                target_id=envelope["target_id"],
                replacement=replacement,
            )
        if entry_type == "label":
            return LabelEntry(
                **common,
                target_id=envelope["target_id"],
                label=envelope.get("label"),
            )
        if entry_type == "session_info":
            return SessionInfoEntry(**common, name=envelope.get("name"))
        raise ValueError(f"unknown session entry type: {entry_type!r}")
    except (TypeError, ValueError, KeyError) as exc:
        raise SessionStoreError("stored Session entry has invalid fields") from exc


def _decode_message(raw: object) -> Message:
    if not isinstance(raw, Mapping):
        raise ValueError("stored Session entry message must be an object")
    return message_from_dict(raw)


def _usage_to_data(usage: Usage) -> dict[str, object]:
    return {
        "input": usage.input,
        "output": usage.output,
        "cache_read": usage.cache_read,
        "cache_write": usage.cache_write,
        "total_tokens": usage.total_tokens,
        "cost": {
            "input": usage.cost.input,
            "output": usage.cost.output,
            "cache_read": usage.cost.cache_read,
            "cache_write": usage.cost.cache_write,
            "total": usage.cost.total,
        },
        "cache_write_1h": usage.cache_write_1h,
        "reasoning": usage.reasoning,
    }


def _usage_from_data(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        raise ValueError("stored usage must be an object")
    cost = raw.get("cost")
    if not isinstance(cost, Mapping):
        raise ValueError("stored usage cost must be an object")
    return Usage(
        input=raw["input"],
        output=raw["output"],
        cache_read=raw["cache_read"],
        cache_write=raw["cache_write"],
        total_tokens=raw["total_tokens"],
        cost=UsageCost(
            input=cost["input"],
            output=cost["output"],
            cache_read=cost["cache_read"],
            cache_write=cost["cache_write"],
            total=cost["total"],
        ),
        cache_write_1h=raw.get("cache_write_1h"),
        reasoning=raw.get("reasoning"),
    )


def _encode_content(content: object) -> object:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [content_to_dict(item) for item in content]
    raise TypeError("session entry content must be a string or content list")


def _decode_content(
    raw: object,
) -> str | list[TextContent | FileContent | ThinkingContent | ToolUseContent]:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        raise ValueError("session entry content must be a string or list")
    result = [content_from_dict(item) for item in raw if isinstance(item, Mapping)]
    if len(result) != len(raw):
        raise ValueError("session entry content list must contain objects")
    allowed = (
        TextContent,
        FileContent,
        ThinkingContent,
        ToolUseContent,
    )
    if any(not isinstance(item, allowed) for item in result):
        raise ValueError("session entry contains unsupported content")
    return result
