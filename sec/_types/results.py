"""定义单次 Trace 的结果和用量数据。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .messages import AssistantMessage, Message
from .trajectory import Trajectory, trajectory_from_messages


@dataclass(frozen=True)
class TraceUsage:
    """汇总单次 Trace 的用量，并记录无法确认的 Provider 计量。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reported_responses: int = 0
    unreported_responses: int = 0
    unreported_attempts: int = 0

    @property
    def total_tokens(self) -> int | None:
        """仅在输入和输出 token 均已知时返回总数。"""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def is_complete(self) -> bool:
        """指示所有响应和 attempt 是否均已报告用量。"""
        return self.unreported_responses == 0 and self.unreported_attempts == 0


@dataclass(frozen=True)
class TraceResult:
    """表示成功完成的 Trace；执行失败会作为异常抛给调用方。"""

    final_message: AssistantMessage
    messages: tuple[Message, ...]
    model_calls: int
    tool_calls: int
    model_attempts: int = 0
    retries: int = 0
    tool_attempts: int = 0
    tool_retries: int = 0
    usage: TraceUsage = field(default_factory=TraceUsage)

    @property
    def text(self) -> str:
        """返回最终助手消息的文本。"""
        return self.final_message.text

    @property
    def stop_reason(self) -> str | None:
        """返回 Provider 的停止原因；未提供时返回 None。"""
        if isinstance(self.final_message, AssistantMessage):
            return self.final_message.stop_reason
        return None

    @property
    def trajectory(self) -> Trajectory:
        """返回本次 Trace 新增消息拍平后的行为序列。"""
        return trajectory_from_messages(self.messages)
