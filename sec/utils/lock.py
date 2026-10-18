"""提供带写入者优先语义的异步读写锁。

多个读任务可并发持有读锁；写任务独占访问，且等待中的写者会阻止新读者插队。
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class AsyncRWLock:
    """支持异步上下文管理器的读写锁。"""

    def __init__(self) -> None:
        """创建未持有的读写锁。"""
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read_lock(self) -> AsyncGenerator[None, None]:
        """获取共享读锁，并在退出上下文时释放。"""
        async with self._cond:
            while self._writer or self._waiting_writers > 0:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @asynccontextmanager
    async def write_lock(self) -> AsyncGenerator[None, None]:
        """获取独占写锁，并在退出上下文时释放。"""
        async with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers > 0:
                await self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()
