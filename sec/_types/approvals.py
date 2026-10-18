"""定义策略审批请求和宿主决策的数据契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sec._types.json import JSONValue
from sec._types.policy import PolicyCapabilityType, PolicyRiskLevel

ApprovalOutcome = Literal["approved", "denied", "cancelled"]


@dataclass(frozen=True)
class ToolApprovalRequest:
    """描述一个经策略判定需要宿主确认的能力调用。"""

    tool_call_id: str
    tool_name: str
    capability_type: PolicyCapabilityType
    source: str
    operation: str
    mode_name: str | None
    risk_level: PolicyRiskLevel
    reason: str
    arguments: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        for field_name in (
            "tool_call_id",
            "tool_name",
            "source",
            "operation",
            "reason",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"approval {field_name} must not be empty")
        if self.capability_type not in ("tool", "mcp", "skill"):
            raise ValueError("unsupported approval capability type")
        if self.mode_name is not None and (
            not isinstance(self.mode_name, str) or not self.mode_name.strip()
        ):
            raise ValueError("approval mode name must not be empty")
        if self.risk_level not in ("read", "write", "execute", "network", "unknown"):
            raise ValueError("unsupported approval risk level")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("approval arguments must be a mapping")


@dataclass(frozen=True)
class ToolApprovalDecision:
    """记录宿主对指定工具调用的最终决定。"""

    tool_call_id: str
    outcome: ApprovalOutcome
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id.strip():
            raise ValueError("approval tool call ID must not be empty")
        if self.outcome not in ("approved", "denied", "cancelled"):
            raise ValueError("unsupported approval outcome")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("approval reason must be a string")


__all__ = [
    "ApprovalOutcome",
    "ToolApprovalDecision",
    "ToolApprovalRequest",
]
