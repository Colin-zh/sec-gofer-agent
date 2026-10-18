"""定义策略引擎与 Runtime 之间交换的数据类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .json import JSONValue

PolicyOutcome = Literal["allow", "ask", "deny"]
PolicyCapabilityType = Literal["tool", "mcp", "skill"]
PolicyRiskLevel = Literal["read", "write", "execute", "network", "unknown"]


@dataclass(frozen=True)
class PolicyContext:
    """提供策略评估所需的 Agent、Mode、能力来源和操作信息。"""

    agent_name: str
    mode_name: str | None
    capability_type: PolicyCapabilityType
    capability_name: str
    source: str | None = None
    operation: str | None = None
    arguments: Mapping[str, JSONValue] = field(default_factory=dict)
    risk_level: PolicyRiskLevel = "unknown"
    workspace_scoped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.agent_name, str) or not self.agent_name.strip():
            raise ValueError("policy context agent name must not be empty")
        if self.mode_name is not None and (
            not isinstance(self.mode_name, str) or not self.mode_name.strip()
        ):
            raise ValueError("policy context mode name must not be empty")
        if self.capability_type not in ("tool", "mcp", "skill"):
            raise ValueError("unsupported policy capability type")
        if self.risk_level not in ("read", "write", "execute", "network", "unknown"):
            raise ValueError("unsupported policy risk level")
        if (
            not isinstance(self.capability_name, str)
            or not self.capability_name.strip()
        ):
            raise ValueError("policy context capability name must not be empty")
        if self.source is not None and (
            not isinstance(self.source, str) or not self.source.strip()
        ):
            raise ValueError("policy context source must not be empty")
        if self.operation is not None and (
            not isinstance(self.operation, str) or not self.operation.strip()
        ):
            raise ValueError("policy context operation must not be empty")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("policy context arguments must be a mapping")
        if not isinstance(self.workspace_scoped, bool):
            raise TypeError("workspace_scoped must be a boolean")


@dataclass(frozen=True)
class PolicyDecision:
    """An allow, ask, or deny decision and its optional explanation."""

    outcome: PolicyOutcome
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("allow", "ask", "deny"):
            raise ValueError("policy outcome must be 'allow', 'ask', or 'deny'")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("policy reason must be a string")
        if self.outcome != "allow" and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("ask and deny policy decisions require a reason")

    @classmethod
    def allow(cls, reason: str | None = None) -> PolicyDecision:
        """创建允许执行的决策。"""
        return cls("allow", reason)

    @classmethod
    def ask(cls, reason: str) -> PolicyDecision:
        """创建需要宿主审批的决策。"""
        return cls("ask", reason)

    @classmethod
    def deny(cls, reason: str) -> PolicyDecision:
        """创建拒绝执行的决策。"""
        return cls("deny", reason)


class PolicyEngine(Protocol):
    """基于 Agent、Mode 和能力上下文返回结构化策略结论。"""

    def decide(self, context: PolicyContext) -> PolicyDecision:
        """评估适用策略；不得执行能力或等待宿主审批。"""
