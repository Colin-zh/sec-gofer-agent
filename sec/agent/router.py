"""内容路由：依据意图把本轮请求路由到 main 或 small 模型 tier。

纯规则映射，不发起额外模型调用：简单单步意图（读文件、搜索、基础信息、
闲聊、其他）落到 small；推理、编码、安全、规划等复杂意图恒走 main。
"""

from __future__ import annotations

from dataclasses import dataclass

from sec.agent.intent import IntentClassification
from sec.providers.router import ModelTier

_SMALL_INTENTS = frozenset(
    {"read_file", "web_search", "info_collection", "chitchat", "other"}
)


@dataclass(frozen=True)
class RoutingDecision:
    """本轮请求应使用的模型 tier 及原因。"""

    tier: ModelTier
    reason: str = ""


class ContentRouter:
    """把意图分类结果映射到模型 tier。"""

    def decide(self, intent: IntentClassification) -> RoutingDecision:
        if intent.label in _SMALL_INTENTS:
            return RoutingDecision(
                tier="small",
                reason=f"simple intent: {intent.label}",
            )
        return RoutingDecision(
            tier="main",
            reason=f"complex intent: {intent.label}",
        )
