"""定义消息内容块和工具调用的协议结构。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Union, Literal

from .json import JSONObject


@dataclass
class TextContent:
    """表示一段普通文本内容."""

    type: Literal["text"] = field(default="text", init=False)
    text: str
    text_signature: str | None = None


@dataclass
class ThinkingContent:
    """表示 Provider 返回的推理内容块。"""

    type: Literal["thinking"] = field(default="thinking", init=False)
    thinking: str
    thinking_signature: str | None = None
    redacted: bool | None = None


@dataclass
class FileContent:
    """表示用于 Provider 请求的多模态文件或图像内容。"""

    type: Literal["file"] = field(default="file", init=False)
    url: str  # 可使用 data URL 或 HTTPS 地址。
    data: str
    data_type: str


@dataclass
class ToolUseContent:
    """表示模型发起的一次工具调用。

    id 会在对应的 ToolResultContent 中回传，以便 Provider 匹配调用和结果；
    arguments 保存解析后的参数对象。
    """

    type: Literal["tool_use"] = field(default="tool_use", init=False)
    id: str
    name: str
    arguments: JSONObject
    thought_signature: str | None = None
    namespace: str | None = None


@dataclass
class ToolResultContent:
    """表示工具执行结果，后续作为消息内容返回给模型。"""

    type: Literal["tool_result"] = field(default="tool_result", init=False)
    tool_use_id: str
    content: str
    is_error: bool = False
    # 可附加图像或文档等多模态工具结果。
    attachments: List[Dict[str, Any]] = field(default_factory=list)


Content = Union[
    TextContent,
    ThinkingContent,
    FileContent,
    ToolUseContent,
    ToolResultContent,
]


def content_to_dict(content: Content) -> dict[str, Any]:
    """将内容 dataclass 序列化为包含 type 判别字段的字典。"""
    return _to_data(content)


def content_from_dict(data: Mapping[str, Any]) -> Content:
    """根据 type 判别字段恢复内容；未知类型或字段无效时显式报错。"""
    kind = _required_str(data, "type")
    if kind == "text":
        return TextContent(
            text=_required_str(data, "text"),
            text_signature=_optional_str(data, "text_signature"),
        )
    if kind == "thinking":
        redacted = data.get("redacted")
        if redacted is not None and not isinstance(redacted, bool):
            raise ValueError("redacted must be a boolean or null")
        return ThinkingContent(
            thinking=_required_str(data, "thinking"),
            thinking_signature=_optional_str(data, "thinking_signature"),
            redacted=redacted,
        )
    if kind == "file":
        return FileContent(
            url=_required_str(data, "url"),
            data=_required_str(data, "data"),
            data_type=_required_str(data, "data_type"),
        )
    if kind == "tool_use":
        arguments = data.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("tool-use arguments must be an object")
        return ToolUseContent(
            id=_required_str(data, "id"),
            name=_required_str(data, "name"),
            arguments=dict(arguments),
            thought_signature=_optional_str(data, "thought_signature"),
            namespace=_optional_str(data, "namespace"),
        )
    if kind == "tool_result":
        is_error = data.get("is_error", False)
        attachments = data.get("attachments", [])
        if not isinstance(is_error, bool):
            raise ValueError("tool-result error flag must be a boolean")
        if not isinstance(attachments, list) or any(
            not isinstance(attachment, Mapping) for attachment in attachments
        ):
            raise ValueError("tool-result attachments must be objects")
        return ToolResultContent(
            tool_use_id=_required_str(data, "tool_use_id"),
            content=_required_str(data, "content"),
            is_error=is_error,
            attachments=[dict(attachment) for attachment in attachments],
        )
    raise ValueError(f"unsupported content type: {kind!r}")


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _to_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _to_data(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {_to_data(key): _to_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_data(item) for item in value]
    return value
