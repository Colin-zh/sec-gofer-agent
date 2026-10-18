"""验证运行时事件订阅的生命周期、缓冲限制和消费语义。"""

import asyncio
import unittest

from sec._runtime.subscriptions import EventSubscription
from sec._types import Event, Limits
from sec.exceptions import SecSubscriptionOverflow


class EventSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    """通过订阅接口及运行时投递入口验证迁移后的行为。"""

    def make_subscription(self, **limits: int) -> EventSubscription:
        self.registered: list[EventSubscription] = []
        self.unregistered: list[EventSubscription] = []
        return EventSubscription(
            Limits(**limits), self.registered.append, self.unregistered.append
        )

    async def test_lifecycle_and_batch(self) -> None:
        subscription = self.make_subscription()
        first = Event({"type": "agent_start"})
        second = Event({"type": "agent_end"})
        async with subscription:
            self.assertEqual(self.registered, [subscription])
            self.assertIsNone(subscription._put(first, 2))
            self.assertIsNone(subscription._put(second, 3))
            self.assertEqual(await subscription._next_batch(), [first, second])
            self.assertEqual(subscription._bytes, 0)
            subscription._put(first, 2)
        self.assertEqual(self.unregistered, [subscription])
        self.assertEqual(len(subscription._records), 0)
        await subscription.aclose()
        self.assertEqual(len(self.unregistered), 1)
        with self.assertRaises(StopAsyncIteration):
            await anext(subscription)
        with self.assertRaises(RuntimeError):
            await subscription.__aenter__()

    async def test_count_and_byte_overflow(self) -> None:
        for limits in (
            {"event_queue_size": 1},
            {"event_queue_bytes": 2},
        ):
            with self.subTest(limits=limits):
                subscription = self.make_subscription(**limits)
                async with subscription:
                    event = Event({"type": "agent_start"})
                    self.assertIsNone(subscription._put(event, 2))
                    error = subscription._put(event, 1)
                    self.assertIsInstance(error, SecSubscriptionOverflow)
                    self.assertEqual(len(subscription._records), 0)
                    self.assertEqual(subscription._bytes, 0)
                    with self.assertRaises(SecSubscriptionOverflow) as caught:
                        await anext(subscription)
                    self.assertIs(caught.exception, error)
                self.assertEqual(self.unregistered, [subscription])

    async def test_failure_drains_buffer_before_raising(self) -> None:
        subscription = self.make_subscription()
        async with subscription:
            event = Event({"type": "agent_end"})
            subscription._put(event, 1)
            error = RuntimeError("process stopped")
            subscription._finish(error)
            self.assertIs(await anext(subscription), event)
            with self.assertRaises(RuntimeError) as caught:
                await anext(subscription)
            self.assertIs(caught.exception, error)

    async def test_close_wakes_waiting_reader(self) -> None:
        subscription = self.make_subscription()
        async with subscription:
            reader = asyncio.create_task(anext(subscription))
            await asyncio.sleep(0)
            await subscription.aclose()
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(reader, 1)

    async def test_entry_and_single_reader_required(self) -> None:
        subscription = self.make_subscription()
        with self.assertRaises(RuntimeError):
            await anext(subscription)
        async with subscription:
            reader = asyncio.create_task(anext(subscription))
            try:
                await asyncio.sleep(0)
                with self.assertRaises(RuntimeError):
                    await anext(subscription)
                event = Event({"type": "agent_start"})
                subscription._put(event, 1)
                self.assertIs(await asyncio.wait_for(reader, 1), event)
            finally:
                if not reader.done():
                    reader.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await reader
