"""管理单个 sec Run 的线程安全运行控制消息。

本模块将取消、当前 turn 的 steering 和后续 follow-up 投递到所属事件循环，
并保证 Session 结束运行后不再接收新控制请求。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from sec._types.approvals import ToolApprovalDecision, ToolApprovalRequest
from sec._types.messages import Message


@dataclass
class _PendingToolApproval:
    """保存待审批调用及其事件循环内的响应 Future。"""

    request: ToolApprovalRequest
    future: asyncio.Future[ToolApprovalDecision]
    resolved: bool = False


class RunControl:
    """管理单个 Run 的线程安全取消、steering、follow-up 和工具审批。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._closed = False
        self._stop_requested = False
        self._pending_callbacks = 0
        self._pending_approval: _PendingToolApproval | None = None
        self.stop_event = asyncio.Event()
        self.steering: asyncio.Queue[str] = asyncio.Queue()
        self.follow_ups: asyncio.Queue[str] = asyncio.Queue()

    @property
    def stop_requested(self) -> bool:
        """线程安全地读取是否已提交取消请求。"""
        with self._lock:
            return self._stop_requested

    def raise_if_stopped(self) -> None:
        """Run 已收到取消请求时抛出协作式取消异常。"""
        if self.stop_requested:
            raise asyncio.CancelledError

    def cancel(self) -> bool:
        """提交一次协作式取消请求，并通知运行事件循环。"""
        with self._lock:
            if self._closed or self._stop_requested:
                return False
            self._stop_requested = True
            same_loop = self._is_running_loop()
            if same_loop:
                self.stop_event.set()
            else:
                self._loop.call_soon_threadsafe(self.stop_event.set)
        return True

    def steer(self, text: str) -> bool:
        """将 steering 消息投递到当前 turn 的队列。"""
        return self._submit(self.steering, text)

    def follow_up(self, text: str) -> bool:
        """将 follow-up 消息投递到后续 turn 的队列。"""
        return self._submit(self.follow_ups, text)

    def approve_tool(self, tool_call_id: str) -> bool:
        """批准匹配 ID 的活动工具调用。"""
        return self._resolve_tool_approval(
            tool_call_id,
            ToolApprovalDecision(tool_call_id, "approved"),
        )

    def deny_tool(self, tool_call_id: str, reason: str | None = None) -> bool:
        """拒绝匹配 ID 的活动工具调用。"""
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("approval denial reason must not be empty")
        return self._resolve_tool_approval(
            tool_call_id,
            ToolApprovalDecision(tool_call_id, "denied", reason),
        )

    def begin_tool_approval(self, request: ToolApprovalRequest) -> bool:
        """登记待审批调用；已停止或关闭的 Run 拒绝新审批。"""
        if not isinstance(request, ToolApprovalRequest):
            raise TypeError("approval request must be a ToolApprovalRequest")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("tool approval must begin on its owning event loop")
        with self._lock:
            if self._closed or self._stop_requested:
                return False
            if self._pending_approval is not None:
                raise RuntimeError("a Run can only wait for one tool approval at a time")
            self._pending_approval = _PendingToolApproval(
                request=request,
                future=self._loop.create_future(),
            )
            return True

    async def wait_for_tool_approval(
        self,
        tool_call_id: str,
    ) -> ToolApprovalDecision | None:
        """等待审批响应；Run 被停止时返回 None。"""
        with self._lock:
            pending = self._pending_approval
            if pending is None or pending.request.tool_call_id != tool_call_id:
                raise RuntimeError("tool approval request is not active")

        stop_task = asyncio.create_task(self.wait_for_stop())
        try:
            done, _ = await asyncio.wait(
                {pending.future, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done or self.stop_requested:
                return None
            return pending.future.result()
        finally:
            if not stop_task.done():
                stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            with self._lock:
                if self._pending_approval is pending:
                    self._pending_approval = None

    def _submit(self, queue: asyncio.Queue[str], text: str) -> bool:
        if not isinstance(text, str):
            raise TypeError("control message must be a string")
        if not text.strip():
            raise ValueError("control message must not be empty")
        with self._lock:
            if self._closed or self._stop_requested:
                return False
            if self._is_running_loop():
                queue.put_nowait(text)
            else:
                self._pending_callbacks += 1
                try:
                    self._loop.call_soon_threadsafe(self._deliver, queue, text)
                except RuntimeError:
                    self._pending_callbacks -= 1
                    raise
            return True

    def _is_running_loop(self) -> bool:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return running_loop is self._loop

    def _deliver(self, queue: asyncio.Queue[str], text: str) -> None:
        with self._lock:
            self._pending_callbacks -= 1
            if not self._closed:
                queue.put_nowait(text)

    def _resolve_tool_approval(
        self,
        tool_call_id: str,
        response: ToolApprovalDecision,
    ) -> bool:
        if not isinstance(tool_call_id, str):
            raise TypeError("tool call ID must be a string")
        with self._lock:
            pending = self._pending_approval
            if (
                pending is None
                or pending.request.tool_call_id != tool_call_id
                or response.tool_call_id != tool_call_id
                or pending.resolved
                or self._closed
                or self._stop_requested
            ):
                return False
            pending.resolved = True
            if self._is_running_loop():
                pending.future.set_result(response)
                return True

            self._pending_callbacks += 1
            try:
                self._loop.call_soon_threadsafe(
                    self._deliver_tool_approval,
                    pending,
                    response,
                )
            except RuntimeError:
                self._pending_callbacks -= 1
                pending.resolved = False
                raise
            return True

    def _deliver_tool_approval(
        self,
        pending: _PendingToolApproval,
        response: ToolApprovalDecision,
    ) -> None:
        with self._lock:
            self._pending_callbacks -= 1
            if (
                not self._closed
                and not self._stop_requested
                and self._pending_approval is pending
                and not pending.future.done()
            ):
                pending.future.set_result(response)

    def take_steering_nowait(self) -> str | None:
        try:
            return self.steering.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def take_follow_up_nowait(self) -> str | None:
        try:
            return self.follow_ups.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def wait_for_steering(self) -> str:
        return await self.steering.get()

    async def wait_for_stop(self) -> None:
        await self.stop_event.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = self._pending_approval
            self._pending_approval = None
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    def close_if_idle(self) -> bool:
        with self._lock:
            if (
                self._pending_callbacks
                or self._pending_approval is not None
                or not self.steering.empty()
                or not self.follow_ups.empty()
            ):
                return False
            self._closed = True
            return True
