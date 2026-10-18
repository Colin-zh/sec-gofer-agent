"""封装单个 sec Run 的结果等待、事件订阅和运行控制接口。

Run 持有 Harness 创建的执行任务、Session、控制队列和事件流；调用方可等待
结果、读取有界事件，或提交取消、steering、follow-up 和工具审批响应。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sec._runtime.control import RunControl
from sec._runtime.event_stream import RunEventStream
from sec.agent.session import Session
from sec._types.events import SessionEvent
from sec._types.results import RunResult


class Run:
    """观察单次执行并提交运行控制请求的句柄。"""

    def __init__(
        self,
        task: asyncio.Task[RunResult],
        session: Session,
        control: RunControl,
        event_stream: RunEventStream,
    ) -> None:
        self._task = task
        self.session = session
        self._control = control
        self._event_stream = event_stream

    @property
    def id(self) -> str:
        """返回此 Run 的唯一标识。"""
        return self._event_stream.run_id

    def events(self) -> AsyncIterator[SessionEvent]:
        """订阅当前 Run 的事件流；每个订阅独立缓冲，过慢时显式溢出。"""
        return self._event_stream.subscribe()

    async def result(self) -> RunResult:
        """等待 Run 完成并返回结果；保留执行任务自身的异常。"""
        return await asyncio.shield(self._task)

    def cancel(self) -> bool:
        """请求协作式取消；返回是否接受了请求。"""
        return self._control.cancel()

    def steer(self, text: str) -> bool:
        """将指令送入当前 turn；Run 已关闭时返回 False。"""
        return self._control.steer(text)

    def follow_up(self, text: str) -> bool:
        """请求当前 turn 完成后启动一轮新任务。"""
        return self._control.follow_up(text)

    def approve_tool(self, tool_call_id: str) -> bool:
        """批准当前等待确认的工具调用；ID 不匹配或 Run 已结束时返回 False。"""
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise ValueError("tool call ID must not be empty")
        return self._control.approve_tool(tool_call_id)

    def deny_tool(
        self,
        tool_call_id: str,
        reason: str | None = None,
    ) -> bool:
        """拒绝当前等待确认的工具调用，并可提供发回模型的安全说明。"""
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise ValueError("tool call ID must not be empty")
        return self._control.deny_tool(tool_call_id, reason)

    @property
    def done(self) -> bool:
        """指示底层 Run 任务是否已经结束。"""
        return self._task.done()
