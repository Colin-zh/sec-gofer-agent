"""LLM-as-Judge：用小模型判断助手输出是否满足用户请求。

judge 仅作观测，不阻断回复：失败（响应不可解析或 Provider 错误）返回
status="unknown"，由调用方发布事件而不改变回复内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from sec._types.providers import ModelProvider
from sec.agent._util import complete_text, parse_json_object

JudgeStatus: TypeAlias = Literal["pass", "fail", "unknown"]

_JUDGE_SYSTEM = (
    "You judge whether an assistant's response adequately satisfies the user's "
    "request. Consider correctness, completeness, and safety. Reply with ONLY a "
    'JSON object: {"status": "pass"|"fail"|"unknown", "score": 0.0, '
    '"reason": "<short why>"}. score is from 0.0 to 1.0. Use "unknown" when '
    "you cannot determine the answer."
)


@dataclass(frozen=True)
class JudgeVerdict:
    """一次输出判断的结果。"""

    status: JudgeStatus
    score: float = 0.0
    reason: str = ""


class OutputJudge:
    """用注入的 ModelProvider 判断助手输出质量。"""

    def __init__(self, provider: ModelProvider) -> None:
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("judge provider must implement complete()")
        self._provider = provider

    async def judge(self, prompt: str, response_text: str) -> JudgeVerdict:
        user_input = f"User request:\n{prompt}\n\nAssistant response:\n{response_text}"
        raw = await complete_text(self._provider, _JUDGE_SYSTEM, user_input)
        parsed = parse_json_object(raw)
        if parsed is None:
            return JudgeVerdict(status="unknown", score=0.0, reason="unparseable response")
        status = parsed.get("status")
        if status not in ("pass", "fail", "unknown"):
            status = "unknown"
        score = parsed.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = 0.0
        else:
            score = max(0.0, min(1.0, float(score)))
        reason = parsed.get("reason")
        reason = reason if isinstance(reason, str) else ""
        return JudgeVerdict(status=status, score=score, reason=reason)
