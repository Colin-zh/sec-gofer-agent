"""打包辅助推理组件，供 Harness / trace_loop 统一注入。

辅助步骤（意图分类、查询改写、LLM-as-Judge、trace 摘要）共用一个小模型
provider，各自独立开关；每轮用 AuxiliaryBudget 限制调用次数，不占用
Agent 的 max_model_calls 预算。EntityExtractor 当前仅作为可复用组件提供，
未接入主循环。
"""

from __future__ import annotations

from dataclasses import dataclass

from sec.agent.entity import EntityExtractor
from sec.agent.intent import IntentClassifier
from sec.agent.judge import OutputJudge
from sec.agent.rewrite import QueryRewriter
from sec.agent.router import ContentRouter
from sec.agent.summary import TraceSummarizer
from sec.config import AuxiliarySettings


@dataclass
class AuxiliaryBudget:
    """单轮辅助步骤的调用预算。"""

    remaining: int

    def consume(self) -> bool:
        """尝试消费一次调用额度；额度耗尽返回 False。"""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass
class AuxiliaryRuntime:
    """组合可选的辅助推理组件及其开关配置。"""

    settings: AuxiliarySettings
    intent: IntentClassifier | None = None
    router: ContentRouter | None = None
    rewriter: QueryRewriter | None = None
    judge: OutputJudge | None = None
    summarizer: TraceSummarizer | None = None
    extractor: EntityExtractor | None = None

    @property
    def active(self) -> bool:
        """总开关是否启用。"""
        return self.settings.enabled

    def new_budget(self) -> AuxiliaryBudget:
        """开启新的一轮辅助调用预算。"""
        return AuxiliaryBudget(self.settings.max_calls_per_turn)
