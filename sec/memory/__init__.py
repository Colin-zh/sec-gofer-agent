"""公开 sec 长期记忆（三层）与工作记忆的组件。

Session 持有原始对话历史；ContextBuilder 只构建请求视图并生成可持久化摘要，
不改写或删除原始历史。长期记忆由 procedural（USER.md）、semantic（事实抽取）、
episodic（消息索引）与 gate/retriever/consolidator 组成。
"""

from sec.memory.consolidation import MemoryConsolidator
from sec.memory.context_builder import (
    ContextBuilder,
    ContextCompactionPlan,
    PreparedContext,
)
from sec.memory.gate import MemoryGate
from sec.memory.procedural import SkillSource, UserProfile
from sec.memory.retrieval import MemoryRetriever
from sec.memory.runtime import MemoryRuntime

__all__ = [
    "ContextBuilder",
    "ContextCompactionPlan",
    "MemoryConsolidator",
    "MemoryGate",
    "MemoryRetriever",
    "MemoryRuntime",
    "PreparedContext",
    "SkillSource",
    "UserProfile",
]
