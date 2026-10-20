"""辅助推理组件共用的模型调用与 JSON 解析辅助函数。

各组件沿用 MemoryGate 的范式：向注入的 ModelProvider 发送一条 system+user
消息、请求纯文本响应，并用宽松的正则解析其中的 JSON。Provider 错误由调用方
fail-open 处理，本模块不吞掉可重试错误。
"""

from __future__ import annotations

import json
import re
from time import time
from typing import Any

from sec._types.content import TextContent
from sec._types.messages import SystemMessage, UserMessage
from sec._types.providers import ModelProvider, ModelRequestOptions

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


async def complete_text(provider: ModelProvider, system: str, user: str) -> str:
    """向 Provider 发送单条 system+user 消息并返回其纯文本响应。"""
    messages = [
        SystemMessage(content=[TextContent(text=system)], timestamp=time()),
        UserMessage(content=[TextContent(text=user)], timestamp=time()),
    ]
    response = await provider.complete(messages, (), ModelRequestOptions())
    return response.text


def parse_json_object(text: str) -> dict[str, Any] | None:
    """从文本中提取首个 JSON 对象；无合法对象时返回 None。"""
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_json_array(text: str) -> list[Any] | None:
    """从文本中提取首个 JSON 数组；无合法数组时返回 None。"""
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None
