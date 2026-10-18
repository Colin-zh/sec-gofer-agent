"""验证共享 SQLAlchemy Database 的事务和异步资源生命周期。"""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import Column, Integer, MetaData, Table, insert, select

from sec.storage import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    """覆盖事务提交/回滚、关闭等待和关闭后的拒绝行为。"""

    async def asyncSetUp(self) -> None:
        self.database = Database("sqlite+aiosqlite:///:memory:")
        self.metadata = MetaData()
        self.records = Table(
            "database_test_records",
            self.metadata,
            Column("id", Integer, primary_key=True),
            Column("value", Integer, nullable=False),
        )
        await self.database.run_sync(self.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.database.aclose()

    async def test_transaction_commits_and_rolls_back(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(insert(self.records).values(id=1, value=10))

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            async with self.database.transaction() as session:
                await session.execute(insert(self.records).values(id=2, value=20))
                raise RuntimeError("rollback")

        async with self.database.session() as session:
            rows = await session.execute(
                select(self.records.c.id, self.records.c.value)
            )
            self.assertEqual(rows.all(), [(1, 10)])

    async def test_close_waits_for_active_sessions_and_is_idempotent(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_session() -> None:
            async with self.database.session():
                entered.set()
                await release.wait()

        active = asyncio.create_task(hold_session())
        await entered.wait()
        closing = asyncio.create_task(self.database.aclose())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())

        release.set()
        await active
        await closing
        await self.database.aclose()

        with self.assertRaisesRegex(RuntimeError, "closed"):
            async with self.database.session():
                self.fail("closed Database must not create a session")

    async def test_cancelled_transaction_rolls_back_and_releases_session(self) -> None:
        transaction_started = asyncio.Event()
        wait_for_cancel = asyncio.Event()

        async def write_then_wait() -> None:
            async with self.database.transaction() as session:
                await session.execute(
                    insert(self.records).values(id=3, value=30)
                )
                transaction_started.set()
                await wait_for_cancel.wait()

        task = asyncio.create_task(write_then_wait())
        await transaction_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        async with self.database.session() as session:
            result = await session.execute(select(self.records.c.id))
            self.assertEqual(result.scalars().all(), [])

    async def test_empty_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "URL"):
            Database(" ")


if __name__ == "__main__":
    unittest.main()
