"""公开 sec 工作记忆的上下文构建能力。

Session 持有原始对话历史；ContextBuilder 只构建请求视图并生成可持久化摘要，
不改写或删除原始历史。
"""

from sec.memory.context_builder import (
    ContextBuilder,
    ContextCompactionPlan,
    PreparedContext,
)

__all__ = ["ContextBuilder", "ContextCompactionPlan", "PreparedContext"]