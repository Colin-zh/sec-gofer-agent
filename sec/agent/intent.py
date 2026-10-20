"""意图分类：用小模型把用户消息归类为粗粒度意图标签。

分类失败（响应不可解析）返回 label="other" 作为安全默认；Provider 级错误
（RetryableProviderError 等）向上传播，由调用方 fail-open 到主模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from sec._types.providers import ModelProvider
from sec.agent._util import complete_text, parse_json_object

IntentLabel: TypeAlias = Literal[
    "read_file",
    "web_search",
    "info_collection",
    "reasoning",
    "coding",
    "security",
    "planning",
    "chitchat",
    "other",
]

_INTENT_LABELS = frozenset(
    {
        "read_file",
        "web_search",
        "info_collection",
        "reasoning",
        "coding",
        "security",
        "planning",
        "chitchat",
        "other",
    }
)

_INTENT_SYSTEM = (
    "Classify the user's message into exactly one intent label. Reply with ONLY "
    'a JSON object: {"label": "<label>", "confidence": 0.9, "reason": "<why>"}. '
    "Labels: read_file, web_search, info_collection, reasoning, coding, security, "
    "planning, chitchat, other. Use read_file/web_search/info_collection for "
    "simple single-step retrieval or fact-finding; reasoning/planning for "
    "multi-step thinking; coding for programming tasks; security for anything "
    "about vulnerabilities, exploits, authz, or threat analysis."
)


@dataclass(frozen=True)
class IntentClassification:
    """一次意图分类结果。"""

    label: IntentLabel
    confidence: float = 1.0
    reason: str = ""


class IntentClassifier:
    """用注入的 ModelProvider 对用户消息做意图分类。"""

    def __init__(self, provider: ModelProvider) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("intent provider must implement complete()")
        self._provider = provider

    async def classify(self, text: str) -> IntentClassification:
        raw = await complete_text(self._provider, _INTENT_SYSTEM, text)
        parsed = parse_json_object(raw)
        if parsed is None:
            return IntentClassification(
                label="other",
                confidence=0.0,
                reason="unparseable response",
            )
        label = parsed.get("label")
        if not isinstance(label, str) or label not in _INTENT_LABELS:
            return IntentClassification(
                label="other",
                confidence=0.0,
                reason="unknown label",
            )
        confidence = parsed.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 1.0
        else:
            confidence = max(0.0, min(1.0, float(confidence)))
        reason = parsed.get("reason")
        reason = reason if isinstance(reason, str) else ""
        return IntentClassification(label=label, confidence=confidence, reason=reason)
