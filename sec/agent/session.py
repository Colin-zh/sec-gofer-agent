"""保存跨 Run 的会话条目树，并管理同一 Session 的活动 Run 控制权。

Session 负责活动分支消息投影、并发占用检查和可选持久化；每次 Run 结束时释放控制权。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Sequence
from uuid import uuid4

from sec._runtime.control import RunControl
from sec._runtime.recovery import build_interrupted_tool_result_message
from sec._types.messages import Message
from sec._types.session import (
    MessageEntry,
    SessionEntry,
    SessionSnapshot,
)
from sec._types.storage import SessionStoreProtocol
from sec.exceptions import SessionBusyError, SessionStoreError


class Session:
    """持有跨 Run 的会话树，并保证同一时刻至多一个活动 Run。

    可选的 SessionStore 会在消息加入内存历史前先持久化该消息。
    """

    def __init__(
        self,
        *,
        id: str | None = None,
        messages: Iterable[Message] = (),
        store: SessionStoreProtocol | None = None,
    ) -> None:
        self.id = id or uuid4().hex
        initial_messages = tuple(messages)
        if store is not None and initial_messages:
            raise ValueError("use Session.restore() to load persisted messages")
        if store is not None and (
            not callable(getattr(store, "load", None))
            or not callable(getattr(store, "append", None))
        ):
            raise TypeError("store must implement load() and append()")
        self._store = store
        self._version = 0
        self._entries: list[SessionEntry] = []
        self._active_leaf_id: str | None = None
        for message in initial_messages:
            self._entries.append(
                MessageEntry(
                    id=uuid4().hex,
                    parent_id=self._active_leaf_id,
                    timestamp=message.timestamp,
                    message=message,
                )
            )
            self._active_leaf_id = self._entries[-1].id
        self._needs_restore = False
        self._lock = threading.Lock()
        self._active_control: RunControl | None = None

    @classmethod
    async def restore(cls, store: SessionStoreProtocol, session_id: str) -> Session:
        """从存储恢复历史，并封闭中断 Run 留下的未配对工具调用。

        未配对调用的结果无法确定，因此恢复时不重放工具，而是追加错误结果，
        让后续模型请求看到明确的中断状态。
        """
        snapshot = await store.load(session_id)
        if not isinstance(snapshot, SessionSnapshot):
            raise SessionStoreError("SessionStore.load() must return SessionSnapshot")
        if snapshot.session_id != session_id:
            raise SessionStoreError("SessionStore.load() returned a different session")

        session = cls(id=session_id, store=store)
        session._entries.extend(snapshot.entries)
        session._active_leaf_id = snapshot.active_leaf_id
        session._version = snapshot.version

        recovery_messages = build_interrupted_tool_result_message(snapshot.messages)
        if recovery_messages:
            await session._append_messages(recovery_messages)
        return session

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回活动根到叶路径投影出的消息序列。"""
        return self._snapshot().messages

    @property
    def entries(self) -> tuple[SessionEntry, ...]:
        """以只读元组形式返回整个条目树。"""
        return tuple(self._entries)

    @property
    def snapshot(self) -> SessionSnapshot:
        """返回当前完整条目树及活动分支快照。"""
        return self._snapshot()

    @property
    def active_leaf_id(self) -> str | None:
        """返回当前活动分支的叶条目 ID。"""
        return self._active_leaf_id

    async def append_entry(
        self,
        entry: SessionEntry,
        *,
        active_leaf_id: str | None = None,
    ) -> None:
        """追加任意会话历史节点，可指定其父节点和写入后的活动叶。

        节点的 ``parent_id`` 可指向当前叶或任一既有节点以创建分支；
        默认选择新节点作为活动叶。若要在新增旁支时保留另一个叶，可传入
        ``active_leaf_id`` 指向该叶。
        """
        selected_leaf = entry.id if active_leaf_id is None else active_leaf_id
        await self._append_entries((entry,), selected_leaf)

    @property
    def version(self) -> int:
        """返回最近一次成功加载或追加的存储版本。"""
        return self._version

    @property
    def is_busy(self) -> bool:
        """指示当前是否有 Run 占用此 Session。"""
        with self._lock:
            return self._active_control is not None

    def steer(self, text: str) -> bool:
        """向当前活动 Run 提交 steering 指令。"""
        control = self._current_control()
        return control.steer(text) if control is not None else False

    def follow_up(self, text: str) -> bool:
        """向当前活动 Run 提交下一轮 follow-up。"""
        control = self._current_control()
        return control.follow_up(text) if control is not None else False

    def cancel(self) -> bool:
        """请求取消当前活动 Run。"""
        control = self._current_control()
        return control.cancel() if control is not None else False

    def _begin_run(self) -> RunControl:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._needs_restore:
                raise SessionStoreError(
                    "Session storage state is uncertain; restore it before reuse"
                )
            if self._active_control is not None:
                raise SessionBusyError(
                    f"session {self.id!r} already has an active Run"
                )
            control = RunControl(loop)
            self._active_control = control
            return control

    def _finish_run(self, control: RunControl) -> None:
        with self._lock:
            if self._active_control is control:
                self._active_control = None
                control.close()

    async def _append_message(self, message: Message) -> None:
        await self._append_messages((message,))

    async def _append_messages(self, messages: Sequence[Message]) -> None:
        if not messages:
            raise ValueError("cannot append an empty message batch")
        parent_id = self._active_leaf_id
        entries = tuple(
            MessageEntry(
                id=uuid4().hex,
                parent_id=parent_id,
                timestamp=message.timestamp,
                message=message,
            )
            for message in messages
        )
        entries = _link_entries(entries, parent_id)
        await self._append_entries(entries, entries[-1].id)

    async def _append_entries(
        self,
        entries: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> None:
        if not entries:
            raise ValueError("cannot append an empty entry batch")
        candidate_entries = self._entries + list(entries)
        candidate = SessionSnapshot(
            self.id,
            self._version + (1 if self._store is not None else 0),
            tuple(candidate_entries),
            active_leaf_id,
        )
        if self._store is not None:
            try:
                next_version = await self._store.append(
                    self.id,
                    self._version,
                    entries,
                    active_leaf_id,
                )
            except BaseException:
                self._needs_restore = True
                raise
            if type(next_version) is not int or next_version != self._version + 1:
                self._needs_restore = True
                raise SessionStoreError(
                    "SessionStore.append() must return the next version"
                )
            self._version = next_version
        self._entries = list(candidate.entries)
        self._active_leaf_id = candidate.active_leaf_id

    def _snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            self.id,
            self._version,
            tuple(self._entries),
            self._active_leaf_id,
        )

    def _current_control(self) -> RunControl | None:
        with self._lock:
            return self._active_control


def _link_entries(
    entries: Sequence[SessionEntry],
    first_parent_id: str | None,
) -> tuple[SessionEntry, ...]:
    """为新消息节点形成顺序父子链。"""
    linked: list[SessionEntry] = []
    parent_id = first_parent_id
    for entry in entries:
        node = MessageEntry(
            id=entry.id,
            parent_id=parent_id,
            timestamp=entry.timestamp,
            message=entry.message,
        )
        linked.append(node)
        parent_id = node.id
    return tuple(linked)
