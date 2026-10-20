"""为单个 Trace 提供有界事件历史及独立订阅者队列。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable

from sec._types.events import SessionEvent
from sec.exceptions import SecSubscriptionOverflow

DEFAULT_EVENT_BUFFER_SIZE = 128


class TraceEventSubscription(AsyncIterator[SessionEvent]):
    """读取某个 Trace 的独立事件队列；慢订阅者只会使自身显式溢出。"""

    def __init__(
        self,
        capacity: int,
        initial_events: tuple[SessionEvent, ...],
        unregister: Callable[[TraceEventSubscription], None],
    ) -> None:
        self._capacity = capacity
        self._events: deque[SessionEvent] = deque(initial_events)
        self._unregister = unregister
        self._ready = asyncio.Event()
        self._closed = False
        self._error: Exception | None = None
        self._reading = False
        if self._events:
            self._ready.set()

    def __aiter__(self) -> TraceEventSubscription:
        return self

    async def __anext__(self) -> SessionEvent:
        if self._reading:
            raise RuntimeError("Only one reader may iterate a Trace event subscription")
        self._reading = True
        try:
            while True:
                if self._error is not None:
                    error = self._error
                    self._error = None
                    self._events.clear()
                    self._ready.clear()
                    raise error
                if self._events:
                    event = self._events.popleft()
                    if event.type == "agent_settled":
                        self._finish()
                    return event
                if self._closed:
                    raise StopAsyncIteration
                self._ready.clear()
                if self._events or self._error is not None or self._closed:
                    continue
                await self._ready.wait()
        finally:
            self._reading = False

    async def aclose(self) -> None:
        """取消此订阅并释放其缓冲，不影响 Trace 或其他订阅者。"""
        self._events.clear()
        self._error = None
        self._finish()

    def _put(self, event: SessionEvent) -> None:
        if self._closed:
            return
        if len(self._events) >= self._capacity:
            self._fail(SecSubscriptionOverflow(
                "Trace event subscriber exceeded its configured buffer limit"
            ))
            return
        self._events.append(event)
        self._ready.set()
        if event.type == "agent_settled":
            self._finish()

    def _fail(self, error: Exception) -> None:
        self._events.clear()
        self._error = error
        self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unregister(self)
        self._ready.set()


class TraceEventStream:
    """保存有限事件历史，并向多个独立订阅者发布 Trace 事件。"""

    def __init__(
        self,
        *,
        trace_id: str,
        capacity: int = DEFAULT_EVENT_BUFFER_SIZE,
    ) -> None:
        if capacity < 1:
            raise ValueError("event buffer capacity must be at least 1")
        self.trace_id = trace_id
        self._capacity = capacity
        self._events: deque[SessionEvent] = deque(maxlen=capacity)
        self._subscriptions: set[TraceEventSubscription] = set()
        self._history_overflowed = False
        self._terminal_published = False
        self._closed = False

    def publish(self, event: SessionEvent) -> SessionEvent:
        """非阻塞发布事件；任一订阅者过慢只会结束该订阅者。"""
        if self._closed:
            raise RuntimeError("cannot publish to a closed Trace event stream")
        if self._terminal_published:
            raise RuntimeError("cannot publish after the terminal Trace event")
        if len(self._events) == self._capacity:
            self._history_overflowed = True
        self._events.append(event)
        self._terminal_published = event.type == "agent_settled"
        for subscription in tuple(self._subscriptions):
            subscription._put(event)
        return event

    def subscribe(self) -> TraceEventSubscription:
        """创建独立订阅，并重放完整历史；历史已截断则显式报错。"""
        subscription = TraceEventSubscription(
            self._capacity,
            () if self._history_overflowed else tuple(self._events),
            self._subscriptions.discard,
        )
        if self._history_overflowed:
            subscription._fail(
                SecSubscriptionOverflow(
                    "Trace event history exceeded its configured buffer limit"
                )
            )
            return subscription
        if not self._closed and not self._terminal_published:
            self._subscriptions.add(subscription)
        else:
            subscription._finish()
        return subscription

    def close(self) -> None:
        """关闭事件流并唤醒所有仍等待的订阅者。"""
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription._finish()
