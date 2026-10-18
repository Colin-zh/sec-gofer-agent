"""管理供多个仓储复用的 SQLAlchemy 异步数据库资源。

Database 拥有 AsyncEngine、连接池和 AsyncSession 工厂，负责会话/事务作用域及
关闭；它不定义 Session、Memory 等领域表或仓储行为。关闭时等待已进入的操作结束，
并拒绝新的数据库操作。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy.engine import Connection, URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_T = TypeVar("_T")


class Database:
    """持有一个共享 AsyncEngine 并按操作提供独立 AsyncSession。"""

    def __init__(
        self,
        url: str | URL,
        *,
        echo: bool = False,
        connect_args: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(url, str) and not url.strip():
            raise ValueError("database URL must not be empty")
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            connect_args=connect_args or {},
        )
        self._sessions = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )
        self._condition = asyncio.Condition()
        self._close_lock = asyncio.Lock()
        self._active_operations = 0
        self._closing = False
        self._closed = False

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """提供独立的 AsyncSession；未提交的更改在退出时回滚。"""
        async with self._operation():
            async with self._sessions() as session:
                yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession, None]:
        """提供具有原子提交/回滚语义的 AsyncSession 事务。"""
        async with self.session() as session:
            async with session.begin():
                yield session

    async def run_sync(
        self,
        operation: Callable[[Connection], _T],
    ) -> _T:
        """在受管理的异步连接事务中运行 SQLAlchemy DDL/同步迁移回调。"""
        async with self._operation():
            async with self._engine.begin() as connection:
                return await connection.run_sync(operation)

    async def aclose(self) -> None:
        """阻止新操作，等待已进入的操作完成后释放 Engine 连接池。"""
        async with self._close_lock:
            if self._closed:
                return
            async with self._condition:
                self._closing = True
                await self._condition.wait_for(
                    lambda: self._active_operations == 0
                )
            await self._engine.dispose()
            async with self._condition:
                self._closed = True
                self._condition.notify_all()

    async def __aenter__(self) -> Database:
        async with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("database is closing or closed")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @asynccontextmanager
    async def _operation(self) -> AsyncGenerator[None, None]:
        async with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("database is closing or closed")
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_operations -= 1
                self._condition.notify_all()
