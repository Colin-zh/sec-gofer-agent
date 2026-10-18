"""Bridge Sec's asynchronous runtime to the desktop UI thread."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal

from sec import AsyncSecClient, PermissionModePolicy, SessionSummary
from sec.apps.cli import create_chat_commands
from sec.apps.cli.chat_commands import ChatCommandOutcome, ChatCommandRegistry
from sec.apps.cli.resources import CliResourceManager
from sec.agent import Run
from sec.tools import create_builtin_tools


class SecRuntime(QObject):
    """Own an AsyncSecClient on a background asyncio event loop."""

    ready = Signal(str)
    startup_failed = Signal(str)
    text_delta = Signal(str)
    status_changed = Signal(str)
    approval_requested = Signal(str, str, str)
    command_result = Signal(str, str)
    command_completions = Signal(object)
    session_changed = Signal(str)
    session_history = Signal(object)
    sessions_updated = Signal(object)
    assistant_started = Signal()
    exit_requested = Signal()
    run_finished = Signal()
    run_failed = Signal(str)

    def __init__(
        self,
        workspace: Path,
        *,
        enabled_extensions: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.workspace = workspace.expanduser().resolve()
        self.enabled_extensions = enabled_extensions
        self._thread = threading.Thread(
            target=self._thread_main,
            name="sec-desktop-runtime",
            daemon=True,
        )
        self._shutdown_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._client: AsyncSecClient | None = None
        self._commands: ChatCommandRegistry | None = None
        self._policy: PermissionModePolicy | None = None
        self._resources: CliResourceManager | None = None
        self._active_run: Run | None = None
        self._pending_approval_id: str | None = None
        self._approval_future: asyncio.Future[bool] | None = None

    def start(self) -> None:
        self._thread.start()

    def submit_input(self, text: str) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(self._handle_input(text), loop)

    def cancel_run(self) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(self._cancel_run)

    def resolve_approval(self, tool_call_id: str, approved: bool) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(
            self._resolve_approval,
            tool_call_id,
            approved,
        )

    def shutdown(self) -> None:
        self._shutdown_requested.set()
        loop = self._loop
        shutdown = self._shutdown
        if loop is None or not self._thread.is_alive():
            return
        if shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=8)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Sec Desktop runtime is not running")
        return loop

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as error:
            self.startup_failed.emit(str(error))
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None
            asyncio.set_event_loop(None)

    async def _serve(self) -> None:
        self._shutdown = asyncio.Event()
        self._policy = PermissionModePolicy(
            "normal",
            workspace_root=self.workspace,
        )
        self._resources = CliResourceManager(self.workspace)
        self._resources.reload()
        async with AsyncSecClient(
            "sec-desktop",
            "You are Sec, a helpful desktop assistant. Be concise and transparent.",
            tools=create_builtin_tools(self.workspace),
            policy=self._policy,
            enabled_extensions=self.enabled_extensions,
        ) as client:
            self._client = client
            self._commands = create_chat_commands(
                client,
                get_active_run=lambda: self._active_run,
                permission_policy=self._policy,
                on_session_change=self._on_session_changed,
                resource_manager=self._resources,
                refresh_completions=self.command_completions.emit,
            )
            self.command_completions.emit(self._commands.completion_items())
            self.session_changed.emit(client.session.id)
            self._publish_session_history()
            await self._refresh_sessions()
            self.ready.emit(str(self.workspace))
            if self._shutdown_requested.is_set():
                return
            await self._shutdown.wait()
            if self._active_run is not None and not self._active_run.done:
                self._active_run.cancel()
        self._client = None
        self._commands = None

    async def _handle_input(self, text: str) -> None:
        commands = self._commands
        if commands is not None and text.lstrip().startswith("/"):
            try:
                outcome = await commands.dispatch(text)
            except Exception as error:
                self.command_result.emit(f"Command failed: {error}", "error")
                return
            if outcome is not None:
                await self._handle_command_outcome(outcome)
                await self._refresh_sessions()
                return

        run = self._active_run
        if run is not None and not run.done:
            if run.follow_up(text):
                self.command_result.emit(
                    "Queued after the active assistant turn.",
                    "follow_up",
                )
            else:
                self.command_result.emit(
                    "The active Run no longer accepts follow-up input.",
                    "error",
                )
            return
        await self._run_prompt(text)

    async def _handle_command_outcome(
        self,
        outcome: ChatCommandOutcome,
    ) -> None:
        if outcome.message:
            self.command_result.emit(
                outcome.message,
                outcome.display_kind or "info",
            )
        if outcome.should_exit:
            self.exit_requested.emit()
        if outcome.run_prompt is not None:
            run = self._active_run
            if run is not None and not run.done:
                if not run.follow_up(outcome.run_prompt):
                    self.command_result.emit(
                        "The active Run no longer accepts follow-up input.",
                        "error",
                    )
            else:
                await self._run_prompt(outcome.run_prompt)

    async def _run_prompt(self, prompt: str) -> None:
        client = self._client
        if client is None:
            self.run_failed.emit("Sec Client is not ready.")
            return
        if self._active_run is not None and not self._active_run.done:
            self.run_failed.emit("A Sec Run is already active.")
            return

        run = client.run(prompt)
        self._active_run = run
        self.assistant_started.emit()
        self.status_changed.emit("正在运行")
        try:
            async for event in run.events():
                if event.type == "message_update":
                    message_event = event.assistant_message_event
                    if message_event.type == "text_delta":
                        self.text_delta.emit(message_event.delta)
                elif event.type == "tool_approval_requested":
                    await self._request_approval(
                        run,
                        event.tool_call_id,
                        event.tool_name,
                        event.args,
                        event.reason,
                    )
                elif event.type == "tool_execution_start":
                    self.status_changed.emit(f"工具运行中：{event.tool_name}")
                elif event.type == "agent_settled":
                    self.status_changed.emit(
                        {
                            "completed": "已完成",
                            "failed": "运行失败",
                            "limit_reached": "达到运行限制",
                            "cancelled": "已停止",
                        }[event.status]
                    )
            await run.result()
        except asyncio.CancelledError:
            self.status_changed.emit("已停止")
        except Exception as error:
            self.run_failed.emit(str(error))
            self.status_changed.emit("运行失败")
        finally:
            self._active_run = None
            self._pending_approval_id = None
            self._approval_future = None
            self._publish_session_history()
            await self._refresh_sessions()
            self.run_finished.emit()

    def _on_session_changed(self, session_id: str) -> None:
        self.session_changed.emit(session_id)
        self._publish_session_history()

    def _publish_session_history(self) -> None:
        client = self._client
        if client is None:
            return
        messages = tuple(
            (message.role, message.text)
            for message in client.session.messages
            if message.role in {"user", "assistant"} and message.text.strip()
        )
        self.session_history.emit(messages)

    async def _refresh_sessions(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            summaries = await client.list_sessions(limit=30)
        except Exception as error:
            self.command_result.emit(
                f"Could not load recent Sessions: {error}",
                "error",
            )
            return
        current = client.session
        items = [
            (summary.session_id, summary.name, summary.updated_at)
            for summary in summaries
        ]
        if current.id not in {item[0] for item in items} and current.messages:
            summary = SessionSummary.from_snapshot(current.snapshot)
            items.insert(0, (summary.session_id, summary.name, summary.updated_at))
        self.sessions_updated.emit(tuple(items))

    async def _request_approval(
        self,
        run: Run,
        tool_call_id: str,
        tool_name: str,
        args: object,
        reason: str | None,
    ) -> None:
        future = asyncio.get_running_loop().create_future()
        self._pending_approval_id = tool_call_id
        self._approval_future = future
        self.approval_requested.emit(
            tool_call_id,
            tool_name,
            json.dumps(args, ensure_ascii=False, indent=2),
        )
        if reason:
            self.status_changed.emit(f"等待工具审批：{reason}")
        approved = await future
        self._pending_approval_id = None
        self._approval_future = None
        if approved:
            run.approve_tool(tool_call_id)
        else:
            run.deny_tool(tool_call_id, "The desktop user denied this tool call.")

    def _cancel_run(self) -> None:
        run = self._active_run
        if run is not None:
            run.cancel()
        future = self._approval_future
        if future is not None and not future.done():
            future.set_result(False)

    def _resolve_approval(self, tool_call_id: str, approved: bool) -> None:
        future = self._approval_future
        if (
            tool_call_id != self._pending_approval_id
            or future is None
            or future.done()
        ):
            return
        future.set_result(approved)
