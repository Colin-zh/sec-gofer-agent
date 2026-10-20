"""约束单个 sec Trace 的生命周期状态及合法迁移。

TraceState 只保存当前生命周期，不存事件记录、控制队列、审批 Future 或用量；
状态迁移由 Runtime 协调器映射为独立事件。
"""

from __future__ import annotations

from typing import Literal

from sec._types.events import TraceTerminalStatus

TraceStatus = Literal[
    "created",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "limit_reached",
    "cancelled",
]

_TERMINAL_STATUSES = frozenset(
    ("completed", "failed", "limit_reached", "cancelled")
)


class TraceState:
    """维护单个 Trace 的当前状态，并拒绝非法生命周期迁移。"""

    def __init__(self) -> None:
        self._status: TraceStatus = "created"

    @property
    def status(self) -> TraceStatus:
        """返回当前 Trace 生命周期状态。"""
        return self._status

    def start(self) -> None:
        """将新建 Trace 转入运行状态。"""
        self._transition("created", "running")

    def wait_for_approval(self) -> None:
        """将运行中的 Trace 转入等待审批状态。"""
        self._transition("running", "waiting_approval")

    def resume_after_approval(self) -> None:
        """在审批已处理后恢复 Trace。"""
        self._transition("waiting_approval", "running")

    def finish(self, status: TraceTerminalStatus) -> None:
        """将非终态 Trace 结束为完成、失败、超限或取消。"""
        if status not in _TERMINAL_STATUSES:
            raise ValueError("Trace terminal status is invalid")
        if self._status in _TERMINAL_STATUSES:
            raise RuntimeError("Trace is already in a terminal state")
        self._status = status

    def _transition(self, expected: TraceStatus, target: TraceStatus) -> None:
        if self._status != expected:
            raise RuntimeError(
                f"invalid Trace state transition: {self._status} -> {target}"
            )
        self._status = target
