"""编排三层记忆检索并产出拼接结果。

检索结果统一携带 status，用于区分「没有命中」（empty）与「检索失败」
（failed）；部分层失败仍返回已取得的内容，并保留失败原因。程序性记忆由多个
`ProceduralSource` 提供（如 USER.md 用户偏好、已发现的 Skill），逐源加载后
与语义/情景结果一并按预算截断。
"""

from __future__ import annotations

from collections.abc import Sequence
from time import time

from sec._types.content import TextContent
from sec._types.context import TokenEstimator
from sec._types.memory import (
    EpisodicRecord,
    EpisodicStoreProtocol,
    MemoryEntry,
    MemoryRetrievalResult,
    ProceduralBlock,
    ProceduralSource,
    SemanticStoreProtocol,
)
from sec._types.messages import UserMessage
from sec.exceptions import MemoryStoreError


class MemoryRetriever:
    """调用 semantic/episodic 仓储并加载各程序性来源，按预算截断。"""

    def __init__(
        self,
        semantic: SemanticStoreProtocol,
        episodic: EpisodicStoreProtocol,
        *,
        procedural_sources: Sequence[ProceduralSource] = (),
        top_k: int = 5,
        token_budget: int | None = None,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if token_budget is not None and token_budget < 1:
            raise ValueError("token_budget must be at least 1")
        if token_budget is not None and token_estimator is None:
            raise ValueError("token_budget requires a token_estimator")
        self._semantic = semantic
        self._episodic = episodic
        self._procedural_sources = tuple(procedural_sources)
        self._top_k = top_k
        self._token_budget = token_budget
        self._token_estimator = token_estimator

    async def retrieve(self, query: str) -> MemoryRetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("retrieval query must not be empty")

        memories: tuple[MemoryEntry, ...] = ()
        episodes: tuple[EpisodicRecord, ...] = ()
        procedures: tuple[ProceduralBlock, ...] = ()
        errors: list[str] = []
        try:
            memories = await self._semantic.search(query, top_k=self._top_k)
        except MemoryStoreError as exc:
            errors.append(f"semantic: {type(exc).__name__}: {exc}")
        try:
            episodes = await self._episodic.search(query, top_k=self._top_k)
        except MemoryStoreError as exc:
            errors.append(f"episodic: {type(exc).__name__}: {exc}")
        for source in self._procedural_sources:
            try:
                procedures = (*procedures, *await source.load(query))
            except Exception as exc:
                errors.append(f"procedural: {type(exc).__name__}: {exc}")

        memories, episodes, procedures = self._truncate(
            memories, episodes, procedures
        )

        if errors:
            return MemoryRetrievalResult(
                status="failed",
                memories=memories,
                episodes=episodes,
                procedures=procedures,
                error="; ".join(errors),
            )
        if not memories and not episodes and not procedures:
            return MemoryRetrievalResult(status="empty")
        return MemoryRetrievalResult(
            status="ok",
            memories=memories,
            episodes=episodes,
            procedures=procedures,
        )

    def _truncate(
        self,
        memories: tuple[MemoryEntry, ...],
        episodes: tuple[EpisodicRecord, ...],
        procedures: tuple[ProceduralBlock, ...],
    ) -> tuple[
        tuple[MemoryEntry, ...],
        tuple[EpisodicRecord, ...],
        tuple[ProceduralBlock, ...],
    ]:
        if self._token_estimator is None or self._token_budget is None:
            return memories, episodes, procedures

        blocks: list[tuple[MemoryEntry | EpisodicRecord | ProceduralBlock, str]] = [
            (entry, entry.content) for entry in memories
        ] + [
            (record, record.text) for record in episodes
        ] + [
            (block, block.text) for block in procedures
        ]

        def estimate() -> int:
            text = "\n\n".join(block[1] for block in blocks)
            message = UserMessage(content=[TextContent(text=text)], timestamp=time())
            return self._token_estimator.estimate_tokens((message,), ())

        while blocks and estimate() > self._token_budget:
            blocks.pop()

        kept_memories = tuple(
            obj for obj, _ in blocks if isinstance(obj, MemoryEntry)
        )
        kept_episodes = tuple(
            obj for obj, _ in blocks if isinstance(obj, EpisodicRecord)
        )
        kept_procedures = tuple(
            obj for obj, _ in blocks if isinstance(obj, ProceduralBlock)
        )
        return kept_memories, kept_episodes, kept_procedures
