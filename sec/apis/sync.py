"""提供基于持久事件循环线程的同步 Client 与 Trace 流适配器。

该模块只桥接 sec 已有的异步 Client/Trace API；模型、工具、预算和 Session
编排仍由 sec 异步运行时执行。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from typing import Any, Coroutine, TypeVar

from sec.agent import Trace
from sec.apis.client import _SecClientCore
from sec.apps.extension_ui import ExtensionUIRequest, ExtensionUIResponse
from sec._types.events import SessionEvent
from sec._types.results import TraceResult
from sec._runtime.capabilities import ReloadReport
from collections.abc import Iterable

_T = TypeVar("_T")


class _LoopThread:
    """在线程中持有一个长期运行的 asyncio 事件循环。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve,
            name="sec-client-event-loop",
            daemon=True,
        )

    @property
    def thread(self) -> threading.Thread:
        """返回承载事件循环的线程。"""
        return self._thread

    def start(self) -> None:
        """启动事件循环线程并等待其进入 run_forever。"""
        try:
            self._thread.start()
            if not self._ready.wait(timeout=10):
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()
                raise TimeoutError("sec Client event loop did not start in time")
        except BaseException:
            if not self._thread.is_alive() and not self._loop.is_closed():
                self._loop.close()
            raise

    def submit(self, coroutine: Coroutine[Any, Any, _T]) -> Future[_T]:
        """将协程提交到唯一的运行时事件循环。"""
        if not self._thread.is_alive() or self._loop.is_closed():
            coroutine.close()
            raise RuntimeError("sec Client event loop is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def stop(self) -> None:
        """停止事件循环，取消遗留任务并释放循环资源。"""
        if not self._thread.is_alive():
            return
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot synchronously stop the sec event loop thread")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()
            asyncio.set_event_loop(None)


class SecClient(_SecClientCore):
    """同步对话 Client；每个实例在专属事件循环线程上运行异步核心。"""

    @property
    def _loop_thread(self) -> _LoopThread | None:
        return getattr(self, "_sync_loop_thread", None)

    @_loop_thread.setter
    def _loop_thread(self, worker: _LoopThread | None) -> None:
        self._sync_loop_thread = worker

    def __enter__(self) -> SecClient:
        """启动运行时并同步初始化 Provider、Harness 与 Session。"""
        if self._closed:
            raise RuntimeError("SecClient is closed")
        if self._loop_thread is not None:
            raise RuntimeError("SecClient is already entered")
        worker = _LoopThread()
        self._loop_thread = worker
        try:
            worker.start()
            worker.submit(self._initialize()).result()
        except BaseException:
            self._loop_thread = None
            worker.stop()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """同步关闭 Client 持有的异步资源和事件循环线程。"""
        self.close()

    def run(self, prompt: str) -> TraceResult:
        """运行一轮对话，阻塞至结果完成并返回结果对象。"""
        async def run_and_wait() -> TraceResult:
            return await self._start_trace(prompt).result()

        return self._blocking_call(run_and_wait())

    def handle_ui_request(
        self,
        handler_name: str,
        request: ExtensionUIRequest,
    ) -> ExtensionUIResponse:
        """按名称同步调用扩展 UI handler，包括异步 handler。"""
        return self._blocking_call(
            self._handle_ui_request(handler_name, request)
        )

    def reload_resources(self) -> ReloadReport:
        async def reload() -> ReloadReport:
            async with self._resource_mutation_lock:
                return await self._reload_resources()
        return self._blocking_call(reload())

    def select_agent(self, agent_id: str) -> None:
        self._blocking_call(self._select_agent(agent_id))

    def set_resource_enabled(self, kind: str, name: str, enabled: bool) -> ReloadReport:
        return self._blocking_call(self._set_resource_enabled(kind, name, enabled))

    def set_worker_extensions(self, names: Iterable[str]) -> ReloadReport:
        return self._blocking_call(self._set_worker_extensions(names))

    def set_enabled_extensions(self, names: Iterable[str]) -> None:
        async def set_enabled() -> None:
            async with self._resource_mutation_lock:
                await self._set_enabled_extensions(names)
        self._blocking_call(set_enabled())

    def set_mcp_enabled(self, server_id: str, enabled: bool) -> ReloadReport:
        return self._blocking_call(self._set_mcp_enabled(server_id, enabled))

    def revoke_capability(self, name: str) -> None:
        self._blocking_call(self._revoke_capability(name))

    def stream(self, prompt: str) -> SyncTraceStream:
        """创建同步事件流上下文；实际启动 Trace 发生在进入上下文时。"""
        self._require_worker()
        return SyncTraceStream(self, prompt)

    def close(self) -> None:
        """关闭 Client；重复调用安全，注入资源仍由调用方拥有。"""
        worker = self._loop_thread
        if worker is None:
            if self._closed:
                return
            raise RuntimeError("SecClient must be entered before it can be closed")
        if threading.current_thread() is worker.thread:
            raise RuntimeError("SecClient cannot close from its event loop thread")
        try:
            if not self._closed:
                worker.submit(self._close()).result()
        finally:
            self._loop_thread = None
            worker.stop()

    def _blocking_call(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        worker = self._require_worker()
        if threading.current_thread() is worker.thread:
            coroutine.close()
            raise RuntimeError(
                "blocking SecClient methods cannot run on its event loop thread"
            )
        return worker.submit(coroutine).result()

    def _require_worker(self) -> _LoopThread:
        worker = self._loop_thread
        if worker is None or self._closed:
            raise RuntimeError("SecClient must be entered before use")
        return worker

    async def _begin_stream(
        self,
        prompt: str,
    ) -> tuple[Trace, AsyncIterator[SessionEvent]]:
        run = self._start_trace(prompt)
        return run, run.events()


class SyncTraceStream(Iterator[SessionEvent]):
    """把 Trace 异步事件迭代器包装为同步上下文管理器和迭代器。"""

    def __init__(self, client: SecClient, prompt: str) -> None:
        self._client = client
        self._prompt = prompt
        self._run: Trace | None = None
        self._events: AsyncIterator[SessionEvent] | None = None
        self._entered = False
        self._exhausted = False
        self._result_requested = False
        self._result: TraceResult | None = None

    def __enter__(self) -> SyncTraceStream:
        """启动 Trace 并立即订阅其事件流。"""
        if self._entered:
            raise RuntimeError("a SyncTraceStream can only be entered once")
        worker = self._client._require_worker()
        self._run, self._events = worker.submit(
            self._client._begin_stream(self._prompt)
        ).result()
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """在提前退出时取消 Trace；正常结束时传播其最终异常。"""
        if self._run is None or self._events is None:
            return
        worker = self._client._require_worker()
        worker.submit(
            _finish_stream(
                self._run,
                self._events,
                cancel=exc_type is not None or not self._exhausted,
                result_requested=self._result_requested,
            )
        ).result()

    def __iter__(self) -> SyncTraceStream:
        return self

    def __next__(self) -> SessionEvent:
        """按需从异步事件流取出下一条事件。"""
        if not self._entered or self._events is None:
            raise RuntimeError("SyncTraceStream must be entered before iteration")
        event = self._client._blocking_call(_next_event(self._events))
        if event is None:
            self._exhausted = True
            raise StopIteration
        return event

    def result(self) -> TraceResult:
        """等待当前 Trace 的最终结果；失败异常与异步 API 一致地传播。"""
        if not self._entered or self._run is None:
            raise RuntimeError("SyncTraceStream must be entered before reading result")
        self._result_requested = True
        if self._result is None:
            self._result = self._client._blocking_call(self._run.result())
        return self._result


async def _next_event(
    events: AsyncIterator[SessionEvent],
) -> SessionEvent | None:
    try:
        return await anext(events)
    except StopAsyncIteration:
        return None


async def _finish_stream(
    run: Trace,
    events: AsyncIterator[SessionEvent],
    *,
    cancel: bool,
    result_requested: bool,
) -> None:
    if cancel and not run.done:
        run.cancel()
    try:
        if not result_requested:
            try:
                await run.result()
            except asyncio.CancelledError:
                if not cancel:
                    raise
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
