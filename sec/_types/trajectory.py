"""Trajectory：跨多个 Trace 拍扁后得到的行为序列。

Trajectory 与 Trace 是两种不同的视角：

- **Trace** 是保留全部技术细节的原始调用树（由 Span 组成），回答「调用链上
  发生了什么、耗时多少、用了哪个模型、温度如何」。
- **Trajectory** 是把一个或多个 Trace 的对话消息与工具活动按时间顺序拍平、
  过滤后得到的行为摘要，只保留「谁说了什么、Agent 调用了什么工具、结果如何」，
  与技术细节无关，也不是 Trace 的子集。

因此 Trajectory 与 Trace 是并列而非包含关系；它与 `EpisodicRecord` 也不同：
后者是带 embedding 的检索索引条目，而 Trajectory 是可读的行为序列本身。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import json
from typing import Literal

from .messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)

TrajectoryRole = Literal["user", "assistant", "tool_call", "tool_result"]


@dataclass(frozen=True)
class TrajectoryStep:
    """Trajectory 中的一个原子行为步骤。"""

    role: TrajectoryRole
    text: str
    timestamp: float | None = None
    tool_name: str | None = None


@dataclass(frozen=True)
class Trajectory:
    """跨 Trace 拍平后的行为序列，携带其来源的会话与 Trace 标识。"""

    steps: tuple[TrajectoryStep, ...] = field(default_factory=tuple)
    session_id: str | None = None
    trace_ids: tuple[str, ...] = field(default_factory=tuple)

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)


def trajectory_from_messages(
    messages: Sequence[Message],
    *,
    session_id: str | None = None,
    trace_ids: Sequence[str] = (),
) -> Trajectory:
    """把消息序列投影为 Trajectory；跳过 system 等非行为节点。

    - `UserMessage` → ``role="user"``；
    - `AssistantMessage` → 文本为 ``role="assistant"``，其每个 ``tool_use``
      另生成 ``role="tool_call"``；
    - `ToolResultMessage` → ``role="tool_result"``，错误结果在文本中保留标记。
    """
    steps: list[TrajectoryStep] = []
    for message in messages:
        if isinstance(message, UserMessage):
            text = message.text.strip()
            if text:
                steps.append(TrajectoryStep("user", text, message.timestamp))
        elif isinstance(message, AssistantMessage):
            text = message.text.strip()
            if text:
                steps.append(TrajectoryStep("assistant", text, message.timestamp))
            for tool_use in message.tool_uses:
                steps.append(TrajectoryStep(
                    "tool_call",
                    _tool_call_text(tool_use.name, tool_use.arguments),
                    message.timestamp,
                    tool_use.name,
                ))
        elif isinstance(message, ToolResultMessage):
            steps.append(TrajectoryStep(
                "tool_result",
                _tool_result_text(message.tool_name, message.text, message.is_error),
                message.timestamp,
                message.tool_name,
            ))
    return Trajectory(
        steps=tuple(steps),
        session_id=session_id,
        trace_ids=tuple(trace_ids),
    )


def _tool_call_text(name: str, arguments: object) -> str:
    try:
        args_text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        args_text = str(arguments)
    return f"{name} {args_text}"


def _tool_result_text(name: str, text: str, is_error: bool) -> str:
    body = text.strip()
    result = name if not body else f"{name}: {body}"
    return result + (" [error]" if is_error else "")
