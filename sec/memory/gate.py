"""基于模型判断本轮是否需要检索长期记忆（MemGate）。

Gate 失败按 fail-open 处理：检索继续（用原始用户文本作 query），但通过
MemoryGateDecision.error 保留失败原因，使调用方能发布可观测的失败事件。
"""

from __future__ import annotations

import json
import re
from time import time

from sec._types.content import TextContent
from sec._types.memory import MemoryGateDecision
from sec._types.messages import SystemMessage, UserMessage
from sec._types.providers import ModelProvider, ModelRequestOptions
from sec.exceptions import ProviderProtocolError, RetryableProviderError

_GATE_SYSTEM = (
    "You decide whether long-term memory retrieval would help answer the user's "
    "current message. Reply with ONLY a JSON object: "
    '{"retrieve": true|false, "query": "short keyword search phrase"}. '
    "Set retrieve to true only when the user references earlier facts, "
    "preferences, or prior conversation; otherwise false. The query must be a "
    "short search phrase, not a restatement of the message."
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class MemoryGate:
    """用注入的 ModelProvider 判断是否需要检索并提炼 query。"""

    def __init__(self, provider: ModelProvider, *, enabled: bool = True) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("gate provider must implement complete()")
        self._provider = provider
        self._enabled = enabled

    async def decide(self, user_text: str) -> MemoryGateDecision:
        text = user_text.strip()
        if not self._enabled:
            return MemoryGateDecision(retrieve=True, query=text)
        messages = [
            SystemMessage(content=[TextContent(text=_GATE_SYSTEM)], timestamp=time()),
            UserMessage(content=[TextContent(text=text)], timestamp=time()),
        ]
        try:
            response = await self._provider.complete(
                messages, (), ModelRequestOptions()
            )
        except (RetryableProviderError, ProviderProtocolError) as exc:
            return MemoryGateDecision(
                retrieve=True,
                query=text,
                error=f"{type(exc).__name__}: {exc}",
            )
        parsed = _parse_gate_response(response.text)
        if parsed is None:
            return MemoryGateDecision(
                retrieve=True,
                query=text,
                error="gate returned an unparseable response",
            )
        retrieve, query = parsed
        return MemoryGateDecision(retrieve=retrieve, query=query or text)


def _parse_gate_response(text: str) -> tuple[bool, str] | None:
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    retrieve = data.get("retrieve")
    if not isinstance(retrieve, bool):
        return None
    query = data.get("query")
    if not isinstance(query, str):
        query = ""
    return retrieve, query.strip()
