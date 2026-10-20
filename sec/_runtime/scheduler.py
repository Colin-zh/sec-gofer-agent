"""定时任务模型、JSON 持久化与异步触发驱动。

调度器与 Client 解耦：``fire`` 回调负责真正运行一次任务，调度器只负责
按最近触发点唤醒、记录运行结果并落盘，便于用假回调与假时钟单测。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import uuid4

from sec._runtime.cron import CronSchedule, CronScheduleError


def _isoformat(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid datetime {text!r}") from exc


@dataclass(frozen=True)
class ScheduledJob:
    """一条可持久化的定时任务；``next_run_at`` 为运行时派生值，不落盘。"""

    id: str
    name: str
    schedule: str
    prompt: str
    agent_id: str | None = None
    session_id: str | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    last_run_at: datetime | None = None
    last_status: str | None = None

    def next_run_at(self, now: datetime) -> datetime:
        return CronSchedule(self.schedule).next_after(now)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "schedule": self.schedule,
            "prompt": self.prompt,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "enabled": self.enabled,
            "created_at": _isoformat(self.created_at),
            "last_run_at": (
                None if self.last_run_at is None else _isoformat(self.last_run_at)
            ),
            "last_status": self.last_status,
        }

    @classmethod
    def from_dict(cls, data: object) -> ScheduledJob:
        if not isinstance(data, dict):
            raise ValueError("scheduled job must be an object")
        required = ("id", "name", "schedule", "prompt")
        for key in required:
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"scheduled job requires a non-empty string {key!r}")
        schedule = CronSchedule(data["schedule"])  # 加载即校验。
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("scheduled job 'enabled' must be a boolean")
        created_at = _parse_datetime(data["created_at"]) if "created_at" in data else datetime.now()
        last_run_at = (
            None if data.get("last_run_at") is None else _parse_datetime(data["last_run_at"])
        )
        last_status = data.get("last_status")
        if last_status is not None and not isinstance(last_status, str):
            raise ValueError("scheduled job 'last_status' must be a string")
        agent_id = data.get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise ValueError("scheduled job 'agent_id' must be a string")
        session_id = data.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("scheduled job 'session_id' must be a string")
        return cls(
            id=data["id"],
            name=data["name"],
            schedule=str(schedule),
            prompt=data["prompt"],
            agent_id=agent_id,
            session_id=session_id,
            enabled=enabled,
            created_at=created_at,
            last_run_at=last_run_at,
            last_status=last_status,
        )


class ScheduledJobStore:
    """同步、原子的 JSON 任务存储（工作区 ``.sec/scheduled-tasks.json``）。"""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace / ".sec" / "scheduled-tasks.json"

    def load(self) -> tuple[ScheduledJob, ...]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid scheduled tasks file: {self.path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError(f"invalid scheduled tasks file: {self.path}")
        jobs = tuple(ScheduledJob.from_dict(item) for item in data["jobs"])
        ids = [job.id for job in jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("scheduled job ids must be unique")
        return jobs

    def save(self, jobs: tuple[ScheduledJob, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "jobs": [job.to_dict() for job in jobs]}
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".scheduled-tasks-", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ScheduledRunResult:
    """一次定时任务运行的终态与回填信息。"""

    status: Literal["completed", "failed", "skipped"]
    session_id: str | None = None
    message: str | None = None


class CronScheduler:
    """按最近触发点唤醒并调用 ``fire`` 回调的异步驱动。"""

    def __init__(
        self,
        workspace: Path,
        *,
        fire: Callable[[ScheduledJob], Awaitable[ScheduledRunResult]],
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._store = ScheduledJobStore(workspace)
        self._fire = fire
        self._now = now
        self._jobs: dict[str, ScheduledJob] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._fire_tasks: set[asyncio.Task[None]] = set()
        self._running: set[str] = set()
        self._stopping = False

    async def start(self) -> None:
        self._jobs = {job.id: job for job in self._store.load()}
        self._stopping = False
        self._wake.set()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._fire_tasks:
            await asyncio.gather(*self._fire_tasks, return_exceptions=True)
            self._fire_tasks.clear()

    async def list_jobs(self) -> tuple[ScheduledJob, ...]:
        async with self._lock:
            jobs = tuple(self._jobs.values())
        return tuple(sorted(jobs, key=lambda job: (job.created_at, job.id)))

    async def add_job(
        self,
        name: str,
        schedule: str,
        prompt: str,
        agent_id: str | None = None,
    ) -> ScheduledJob:
        name = name.strip()
        prompt = prompt.strip()
        if not name:
            raise ValueError("scheduled task name must not be empty")
        if not prompt:
            raise ValueError("scheduled task prompt must not be empty")
        parsed = CronSchedule(schedule)
        now = self._now()
        job = ScheduledJob(
            id=uuid4().hex,
            name=name,
            schedule=str(parsed),
            prompt=prompt,
            agent_id=agent_id,
            created_at=now,
        )
        async with self._lock:
            self._jobs[job.id] = job
            self._store.save(tuple(self._jobs.values()))
            self._wake.set()
        return job

    async def remove_job(self, job_id: str) -> bool:
        async with self._lock:
            if self._jobs.pop(job_id, None) is None:
                return False
            self._store.save(tuple(self._jobs.values()))
            self._wake.set()
            return True

    async def set_enabled(self, job_id: str, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise TypeError("scheduled task enabled must be a boolean")
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.enabled == enabled:
                return job is not None
            self._jobs[job_id] = replace(job, enabled=enabled)
            self._store.save(tuple(self._jobs.values()))
            self._wake.set()
            return True

    async def _run(self) -> None:
        while not self._stopping:
            now = self._now()
            target, due_ids = self._next_due(now)
            timeout = None if target is None else max(0.0, (target - now).total_seconds())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                woken = True
            except asyncio.TimeoutError:
                woken = False
            finally:
                self._wake.clear()
            if self._stopping:
                break
            if woken:
                # 管理操作（增删改）或任务收尾触发的唤醒：重新计算下一次触发点。
                continue
            await self._fire_due(due_ids, self._now())

    def _next_due(self, now: datetime) -> tuple[datetime | None, tuple[str, ...]]:
        candidates = [
            (job.next_run_at(now), job.id)
            for job in self._jobs.values()
            if job.enabled and job.id not in self._running
        ]
        if not candidates:
            return None, ()
        target = min(candidate for candidate, _ in candidates)
        due_ids = tuple(
            job_id for candidate, job_id in candidates if candidate == target
        )
        return target, due_ids

    async def _fire_due(self, due_ids: tuple[str, ...], now: datetime) -> None:
        for job_id in due_ids:
            job = self._jobs.get(job_id)
            if job is None or not job.enabled or job_id in self._running:
                continue
            self._running.add(job_id)
            task = asyncio.create_task(self._fire_one(job, now))
            self._fire_tasks.add(task)
            task.add_done_callback(self._fire_tasks.discard)

    async def _fire_one(self, job: ScheduledJob, now: datetime) -> None:
        try:
            try:
                result = await self._fire(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = ScheduledRunResult(
                    "failed", message=f"{type(exc).__name__}: {exc}",
                )
        finally:
            self._running.discard(job.id)
        async with self._lock:
            current = self._jobs.get(job.id)
            if current is None:
                return
            updated = replace(
                current,
                last_run_at=now,
                last_status=result.status,
                session_id=result.session_id or current.session_id,
            )
            self._jobs[job.id] = updated
            self._store.save(tuple(self._jobs.values()))
            self._wake.set()


__all__ = [
    "CronScheduler",
    "ScheduledJob",
    "ScheduledJobStore",
    "ScheduledRunResult",
]
