"""定义流式模型 Provider 与 Runtime 之间交换的类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from .messages import AssistantMessage


@dataclass(frozen=True)
class ModelTextDelta:
    """表示 Provider 完成响应前产生的一段临时文本增量。"""

    text: str


@dataclass(frozen=True)
class ModelResponseCompleted:
    """表示已规范化、可提交到 Session 的完整助手消息。"""

    message: AssistantMessage


ModelStreamEvent: TypeAlias = ModelTextDelta | ModelResponseCompleted
