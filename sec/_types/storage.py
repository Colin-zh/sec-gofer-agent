"""定义会话历史与对象存储的可替换行为契约。

SessionStore 使用版本比较并追加消息；每次 append 必须原子提交整个消息批次。
存储事务不包含工具副作用，恢复未完成工具调用的规则由运行时另行负责。

ObjectStore 首版以 bytes 全量读写对象，适合当前附件规模但会将完整对象载入
内存；大对象流式 API 留待后续扩展。一个 ObjectStore 可注入多个领域仓储，
由应用统一关闭。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import Protocol

from sec._types.objects import StoredObject
from sec._types.session import SessionEntry, SessionSnapshot, SessionSummary


class ObjectStore(Protocol):
    """提供异步对象读写、删除及底层客户端关闭操作。"""

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        """写入对象；相同 key 覆盖旧对象，空 key 或非 bytes 数据应被拒绝。"""

    async def get(self, key: str) -> StoredObject:
        """读取完整对象及内容类型；不存在时抛出 ObjectNotFoundError。"""

    async def delete(self, key: str) -> None:
        """删除对象；对象不存在时视为成功，允许调用方安全重试。"""

    async def aclose(self) -> None:
        """释放客户端资源；关闭错误显式传播，关闭后操作抛出 ObjectStoreError。"""


class SessionStoreProtocol(Protocol):
    """读取会话树并以乐观并发控制原子追加条目批次。"""

    @abstractmethod
    async def load(self, session_id: str) -> SessionSnapshot:
        """读取快照；不存在的会话返回版本 0、空条目和空活动叶。

        返回的快照及消息必须与存储内部状态隔离，调用方修改读取结果不得
        修改已存储数据。
        """
        ...

    @abstractmethod
    async def list_sessions(self, limit: int = 20) -> tuple[SessionSummary, ...]:
        """返回最近更新的非空会话摘要，最多返回 limit 条。"""
        ...

    @abstractmethod
    async def append(
        self,
        session_id: str,
        expected_version: int,
        entries: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> int:
        """原子追加条目批次、切换活动叶并返回新版本号。

        新条目的父节点必须已存在或出现在批次前部，active_leaf_id 必须指向
        写入后无子节点的条目。version 从 0 开始，每次成功追加恰好递增 1。
        expected_version 不匹配时抛出 SessionVersionConflictError 且不写入。
        任意失败不得产生部分批次写入；存储错误必须明确抛出
        SessionStoreError。调用方不得盲目重试失败的 append，应先 load
        以核实当前版本及消息，避免不确定提交结果造成重复追加。存储不得保留
        调用方传入的可变消息对象引用。
        """
        ...


class DeletableSessionStoreProtocol(SessionStoreProtocol, Protocol):
    """可选删除能力；第三方旧 adapter 不必实现。"""

    async def delete(self, session_id: str, expected_version: int) -> None:
        """删除匹配版本的会话；版本冲突必须拒绝且不产生部分删除。"""
        ...
