"""运行时有界事件订阅：仅接收订阅后的事件，响应路由不等待消费者。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from types import TracebackType
from typing import Generic, Self, TypeVar

from sec._types import Event
from sec._types.limits import Limits
from sec.exceptions import SecSubscriptionOverflow

T = TypeVar("T")


class _Subscription(Generic[T]):
    """通过上下文管理订阅生命周期，不保存订阅前的历史事件。

    应在提交任务前进入上下文；慢消费者超出缓冲限制时显式收到
    SecSubscriptionOverflow，而非静默丢失事件。退出上下文会立即注销并释放缓冲。
    """

    def __init__(
        self,
        limits: Limits,
        register: Callable[[], None],
        unregister: Callable[[], None],
    ) -> None:
        self._limits = limits
        self._register = register
        self._unregister = unregister
        self._records: deque[tuple[T, int]] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()
        self._entered = False
        self._closed = False
        self._error: Exception | None = None
        self._reading = False

    async def __aenter__(self) -> Self:
        return self._enter()

    def _enter(self) -> Self:
        if self._entered or self._closed:
            raise RuntimeError("Event subscriptions are single-use")
        self._register()
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """注销订阅并释放排队的载荷；可安全重复调用。"""
        self._discard()

    def _discard(self, *, buffered: bool = False) -> None:
        """结束订阅并清空缓冲，也供同步门面在关闭订阅后调用。"""
        self._finish()
        self._records.clear()
        self._bytes = 0
        self._error = None

    def _finish(self, error: Exception | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._error = error
        self._unregister()
        if isinstance(error, SecSubscriptionOverflow):
            # 溢出意味着事件丢失，应立即报错；普通终止则保留完整前缀供消费者读完。
            self._records.clear()
            self._bytes = 0
        self._ready.set()

    def _put(self, event: T, size: int) -> Exception | None:
        if self._closed:
            return self._error
        if (
            len(self._records) >= self._limits.event_queue_size
            or self._bytes + size > self._limits.event_queue_bytes
        ):
            error = SecSubscriptionOverflow("Event consumer exceeded its configured buffer limit")
            self._finish(error)
            return error
        self._records.append((event, size))
        self._bytes += size
        self._ready.set()
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> T:
        if not self._entered:
            raise RuntimeError("Enter the event subscription context before iterating")
        if self._reading:
            raise RuntimeError("Only one reader may iterate an event subscription")
        self._reading = True
        try:
            while True:
                event = self._next_nowait()
                if event is not None:
                    return event
                self._ready.clear()
                await self._ready.wait()
        finally:
            self._reading = False

    async def _next_batch(self) -> list[T]:
        records = [await self.__anext__()]
        # 只批量转移已缓冲的记录，不等待凑满批次，减少同步桥接造成的积压。
        # 下方不挂起；包括刚取出的首条记录在内，批次仍受原队列数量和字节预算约束。
        while self._records:
            record = self._next_nowait()
            assert record is not None
            records.append(record)
        return records

    def _next_nowait(self) -> T | None:
        """仅在所属事件循环中读取，或在该循环完全停止后读取。"""
        if self._records:
            event, size = self._records.popleft()
            self._bytes -= size
            return event
        if self._error is not None:
            raise self._error
        if self._closed:
            raise StopAsyncIteration
        return None


class EventSubscription(_Subscription[Event]):
    """有界的后续事件订阅；如需接收启动事件，应在启动前进入上下文。"""

    def __init__(
        self,
        limits: Limits,
        register: Callable[[EventSubscription], None],
        unregister: Callable[[EventSubscription], None],
    ) -> None:
        super().__init__(limits, lambda: register(self), lambda: unregister(self))
