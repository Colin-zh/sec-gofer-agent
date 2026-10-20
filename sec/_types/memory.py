"""定义长期记忆（Memory）的领域数据与可替换仓储/检索行为契约。

sec 的三层记忆由不同层负责：

- episodic（情景）：带时间和 SessionStore 来源引用的事件检索记录；原始消息仍由 SessionStore 保存；
- semantic（语义）：从用户明确陈述中抽取的稳定事实，见 MemoryEntry / SemanticStoreProtocol；
- procedural（程序性）：用户确认的长期偏好/规则；Skill 作为独立能力资源，不写入用户偏好。

检索/整理结果统一携带 status（ok/empty/failed），用于区分「没有命中」与
「检索失败」，避免把失败伪装成没有记忆。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from typing import Literal, Protocol, TypeAlias

MemoryKind: TypeAlias = Literal["semantic", "episodic"]
MemoryOrigin: TypeAlias = Literal["user", "assistant", "explicit"]
EpisodicKind: TypeAlias = Literal["message", "tool_call", "tool_result"]
MemoryRetrievalStatus: TypeAlias = Literal["ok", "empty", "failed"]
MemoryConsolidationStatus: TypeAlias = Literal["ok", "empty", "failed"]

DEFAULT_SCOPE = "default"


def memory_dedup_key(subject: str | None, content: str) -> str:
    """基于规范化 subject+content 计算幂等去重键。

    调用方写入前自行调用，或由仓储层在 add 时补算；同 scope、同 kind、同键
    视为同一条记忆，不重复写入。
    """
    subject_text = (subject or "").strip().lower()
    content_text = content.strip().lower()
    payload = f"{subject_text}\x00{content_text}"
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemorySource:
    """记录一条记忆的出处，可追溯到 Session 与具体消息节点。"""

    origin: MemoryOrigin
    session_id: str | None = None
    entry_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("session_id", self.session_id), ("entry_id", self.entry_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"memory source {name} must not be empty")
        if self.entry_id is not None and self.session_id is None:
            raise ValueError("memory source entry_id requires session_id")


@dataclass(frozen=True)
class MemoryEntry:
    """表示一条持久语义记忆（事实 / 决定；用户偏好归入程序记忆）。"""

    id: str
    content: str
    kind: MemoryKind
    source: MemorySource
    created_at: float
    updated_at: float
    dedup_key: str
    subject: str | None = None
    scope: str = DEFAULT_SCOPE

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("memory entry id must not be empty")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("memory entry content must not be empty")
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ValueError("memory entry scope must not be empty")
        if not isinstance(self.dedup_key, str) or not self.dedup_key.strip():
            raise ValueError("memory entry dedup_key must not be empty")
        if self.subject is not None and (not isinstance(self.subject, str) or not self.subject.strip()):
            raise ValueError("memory entry subject must not be empty")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"memory entry {name} must be a finite number")


@dataclass(frozen=True)
class EpisodicRecord:
    """表示进入情景检索索引的一条经历记录。

    kind 区分原始对话消息（message）、工具调用（tool_call）与工具执行结果
    （tool_result）；三者共用同一检索索引，text 为可检索文本。
    """

    id: str
    session_id: str
    entry_id: str
    text: str
    timestamp: float
    kind: EpisodicKind = "message"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("episodic record id must not be empty")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("episodic record session_id must not be empty")
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("episodic record entry_id must not be empty")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("episodic record text must not be empty")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, (int, float)) or not isfinite(self.timestamp):
            raise ValueError("episodic record timestamp must be a finite number")
        if self.kind not in ("message", "tool_call", "tool_result"):
            raise ValueError("episodic record kind must be message, tool_call, or tool_result")


class SemanticStoreProtocol(Protocol):
    """语义记忆（持久事实）的异步仓储契约。"""

    async def initialize(self) -> None:
        """确保底层 schema/索引就绪；重复调用应为幂等。"""

    async def add(self, entry: MemoryEntry) -> str:
        """写入一条事实并返回其 id。

        与既有 (scope, kind, dedup_key) 冲突时返回既有 id，不重复写入。
        """

    async def search(self, query: str, *, top_k: int, scope: str | None = None) -> tuple[MemoryEntry, ...]:
        """按相关度返回最多 top_k 条记忆；空结果返回 ()。"""

    async def list(self, *, limit: int, scope: str | None = None) -> tuple[MemoryEntry, ...]:
        """按最近更新顺序列出记忆。"""

    async def update(self, memory_id: str, *, content: str, subject: str | None = None) -> bool:
        """更新既有事实，返回是否命中；会刷新 dedup_key 与 updated_at。"""

    async def delete(self, memory_id: str) -> bool:
        """删除事实，返回是否命中。"""

    async def aclose(self) -> None:
        """释放底层资源；无资源时可不执行操作。"""


class EpisodicStoreProtocol(Protocol):
    """情景记忆（原始对话消息索引）的异步仓储契约。"""

    async def initialize(self) -> None:
        """确保底层 schema/索引就绪；重复调用应为幂等。"""

    async def index(self, records: Sequence[EpisodicRecord]) -> None:
        """写入一批消息索引；同 id 覆盖，其余追加。"""

    async def search(self, query: str, *, top_k: int) -> tuple[EpisodicRecord, ...]:
        """按相关度返回最多 top_k 条消息；空结果返回 ()。"""

    async def list(self, *, limit: int) -> tuple[EpisodicRecord, ...]:
        """按时间倒序返回最多 limit 条记录；空结果返回 ()。"""

    async def delete_session(self, session_id: str) -> None:
        """删除某 session 的全部消息索引。"""

    async def aclose(self) -> None:
        """释放底层资源；无资源时可不执行操作。"""


@dataclass(frozen=True)
class ProceduralBlock:
    """表示用户规则或独立 Skill 能力，不将两类来源混为一谈。"""

    name: str
    text: str
    category: Literal["user_rule", "skill"] = "user_rule"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("procedural block name must not be empty")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("procedural block text must not be empty")
        if self.category not in ("user_rule", "skill"):
            raise ValueError("procedural block category must be user_rule or skill")


class ProceduralSource(Protocol):
    """程序性记忆（「怎么做」）来源的异步契约。"""

    async def load(self, query: str) -> tuple[ProceduralBlock, ...]:
        """按当前 query 返回相关过程块；无内容返回 ()。"""


@dataclass(frozen=True)
class MemoryGateDecision:
    """MemGate 判断结果；error 非空表示 gate 模型调用失败。"""

    retrieve: bool
    query: str = ""
    error: str | None = None


@dataclass(frozen=True)
class MemoryRetrievalResult:
    """三层检索的拼接结果；status 区分 ok/empty/failed。"""

    status: MemoryRetrievalStatus
    memories: tuple[MemoryEntry, ...] = ()
    episodes: tuple[EpisodicRecord, ...] = ()
    procedures: tuple[ProceduralBlock, ...] = ()
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.memories and not self.episodes and not self.procedures


@dataclass(frozen=True)
class MemoryConsolidationResult:
    """整理结果；status 区分 ok/empty/failed。"""

    status: MemoryConsolidationStatus
    added_ids: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ProceduralConsolidationResult:
    """程序记忆（USER.md）整理结果；added 为本次新写入的规则文本。"""

    status: MemoryConsolidationStatus
    added: tuple[str, ...] = ()
    error: str | None = None


__all__ = [
    "DEFAULT_SCOPE",
    "EpisodicKind",
    "EpisodicRecord",
    "EpisodicStoreProtocol",
    "MemoryConsolidationResult",
    "MemoryConsolidationStatus",
    "MemoryEntry",
    "MemoryGateDecision",
    "MemoryKind",
    "MemoryOrigin",
    "MemoryRetrievalResult",
    "MemoryRetrievalStatus",
    "MemorySource",
    "ProceduralBlock",
    "ProceduralConsolidationResult",
    "ProceduralSource",
    "SemanticStoreProtocol",
    "memory_dedup_key",
]
