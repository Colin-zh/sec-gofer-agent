"""定义可复用的 sec Agent 配置。

Agent 保存模型指令、可用工具和单轮模型/工具调用上限；它描述任务能力，
不持有会话状态，也不直接执行模型请求或工具。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sec.tools.tool import Tool


@dataclass(frozen=True)
class Agent:
    """封装可复用的任务指令、工具集合及单轮调用限制。"""

    name: str
    instructions: str
    tools: tuple[Tool, ...] = field(default_factory=tuple)
    max_model_calls: int = 8
    max_tool_calls: int = 32

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent name must not be empty")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be at least 1")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")
        names = [registered.definition.name for registered in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("agent tool names must be unique")
