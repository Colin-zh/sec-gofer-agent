"""定义 sec 工具调用的结构化策略决策及基础授权规则。

策略只决定允许、询问或拒绝，不执行工具；审批请求由 Run 与宿主应用协调。
"""

from __future__ import annotations

from collections.abc import Iterable

from sec._types.policy import PolicyContext, PolicyDecision


class DenyAllPolicy:
    """未显式授权时拒绝所有工具调用。"""

    def decide(self, context: PolicyContext) -> PolicyDecision:
        """拒绝给定上下文中的能力调用。"""
        return PolicyDecision.deny("Tool call denied by policy.")


class AllowListPolicy:
    """仅允许名称出现在白名单中的工具。"""

    def __init__(self, allowed_tools: Iterable[str]) -> None:
        """创建工具名称白名单；其他类型的能力始终拒绝。"""
        self._allowed_tools = frozenset(allowed_tools)

    def decide(self, context: PolicyContext) -> PolicyDecision:
        """仅允许白名单中的工具名称。"""
        if (
            context.capability_type == "tool"
            and context.capability_name in self._allowed_tools
        ):
            return PolicyDecision.allow()
        return PolicyDecision.deny("Tool call denied by policy.")
