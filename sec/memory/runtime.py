"""打包长期记忆在运行时所需的组件，供 Harness 与 trace_loop 统一注入。"""

from __future__ import annotations

from dataclasses import dataclass

from sec._types.memory import EpisodicStoreProtocol
from sec.memory.consolidation import MemoryConsolidator
from sec.memory.gate import MemoryGate
from sec.memory.procedural_consolidation import ProceduralConsolidator
from sec.memory.retrieval import MemoryRetriever


@dataclass(frozen=True)
class MemoryRuntime:
    """组合 gate/retriever（读路径）与 episodic/consolidator（写路径）。"""

    gate: MemoryGate | None = None
    retriever: MemoryRetriever | None = None
    episodic: EpisodicStoreProtocol | None = None
    consolidator: MemoryConsolidator | None = None
    procedural_consolidator: ProceduralConsolidator | None = None

    @property
    def has_retrieval(self) -> bool:
        return self.gate is not None and self.retriever is not None

    @property
    def has_post_run(self) -> bool:
        return (
            self.episodic is not None
            or self.consolidator is not None
            or self.procedural_consolidator is not None
        )
