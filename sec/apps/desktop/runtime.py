"""Bridge Sec's asynchronous runtime to the desktop UI thread."""

from __future__ import annotations

import asyncio
from importlib.metadata import entry_points
import json
import shlex
from pathlib import Path
import threading
from typing import Literal
from time import time
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from sec import AsyncSecClient, PermissionModePolicy, SessionSummary
from sec.apps.cli import create_chat_commands
from sec.apps.cli.app import _resource_command_specs
from sec.apps.cli.chat_commands import (
    ChatCommandOutcome,
    ChatCommandRegistry,
    format_reload_report,
)
from sec.apps.cli.resources import CliResourceManager
from sec.apps.cli.session_io import write_session_file
from sec.agent import Trace
from sec.tools import Tool, create_builtin_tools
from sec.apps.desktop.settings import ExtensionSettings
from sec.apps.desktop.tool_page import ToolEntry, ToolInventory
from sec._types.session import CustomMessageEntry, MessageEntry
from sec._types.json import JSONObject
from sec._types.events import AgentSettledEvent
from sec.apps.desktop.trace_store import TraceRecorder, TraceStore


def _status_value(status: object, *names: str, default: object = "") -> object:
    if isinstance(status, dict):
        return next((status[name] for name in names if name in status), default)
    return next(
        (getattr(status, name) for name in names if hasattr(status, name)),
        default,
    )


def _classify_tool(tool: Tool) -> tuple[bool, str | None, str | None, str]:
    """按工具来源分类：内置只读，MCP/扩展可启停其来源。

    返回 (builtin, origin_kind, origin_name, source_label)。
    """
    source = tool.source
    if source == "sec.builtin.workspace":
        return True, None, None, "内置工具"
    if source.startswith("mcp/"):
        server = source.split("/")[1]
        return False, "mcp", server, f"MCP 服务 {server}"
    if source.startswith("worker/"):
        extension = source.split("/")[1]
        return False, "worker", extension, f"插件 {extension}（worker）"
    if source.startswith("extension/"):
        extension = source.split("/")[1]
        return False, "extension", extension, f"插件 {extension}"
    return True, None, None, "由 Agent / 技能 提供"


class SecRuntime(QObject):
    """Own an AsyncSecClient on a background asyncio event loop."""

    ready = Signal(str)
    startup_failed = Signal(str)
    text_delta = Signal(str)
    status_changed = Signal(str)
    approval_requested = Signal(str, str, str)
    extension_ui_requested = Signal(str, object)
    command_result = Signal(str, str)
    command_completions = Signal(object)
    session_changed = Signal(str)
    session_history = Signal(object)
    session_timing = Signal(object)
    sessions_updated = Signal(object)
    resources_updated = Signal(object)
    configuration_result = Signal(str, str)
    assistant_started = Signal()
    exit_requested = Signal()
    run_finished = Signal()
    run_failed = Signal(str)
    traces_updated = Signal(object, object, object)
    trace_updated = Signal(str)

    def __init__(
        self,
        workspace: Path,
        *,
        enabled_extensions: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.workspace = workspace.expanduser().resolve()
        self._trace_store = TraceStore(
            self.workspace / ".sec" / "desktop-traces.sqlite3",
        )
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
        self._extension_settings: ExtensionSettings | None = None
        self._configuring = False
        self._active_trace: Trace | None = None
        self._pending_approval_id: str | None = None
        self._approval_future: asyncio.Future[bool] | None = None
        self._pending_approval_name: str | None = None
        self._approval_writing = False
        self._approval_cancelled = False
        self._pending_ui_id: str | None = None
        self._ui_future: asyncio.Future[dict] | None = None
        self._ui_cancelled = False
        self._queued_resource_inputs: list[tuple[str, str, frozenset[str]]] = []
        self._prompt_task: asyncio.Task[object] | None = None
        self._send_lock: asyncio.Lock | None = None

    @property
    def trace_store(self) -> TraceStore:
        return self._trace_store

    def _publish_traces(self, preferred_trace_id: str | None = None) -> None:
        try:
            session_id = self._client.session.id if self._client is not None else None
            runs = (
                self._trace_store.list_traces(session_id=session_id)
                if session_id is not None
                else []
            )
            self.traces_updated.emit(
                runs,
                preferred_trace_id,
                session_id,
            )
        except Exception as error:
            self.command_result.emit(
                f"Trace 列表读取失败（{type(error).__name__}）。",
                "error",
            )

    def submit_message(self, text: str, mode: str = "replace") -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(
            self._submit_message(text, mode), loop,
        )

    async def _submit_message(self, text: str, mode: str) -> None:
        try:
            if mode not in {"replace", "steer", "followup"}:
                raise ValueError("未知发送模式")
            if mode != "replace":
                await self._handle_input(f"/{mode} {shlex.quote(text)}")
                return
            if self._send_lock is None:
                self._send_lock = asyncio.Lock()
            async with self._send_lock:
                task = self._prompt_task
                if task is not None and not task.done():
                    self._cancel_trace()
                    await asyncio.shield(task)
                next_task = asyncio.create_task(self._handle_input(text))
                # Let the new Trace acquire its Session before another send can stop it.
                await asyncio.sleep(0)
            await next_task
        except Exception as error:
            self.command_result.emit(f"发送失败：{error}", "error")

    def start(self) -> None:
        self._thread.start()

    def submit_input(self, text: str) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(self._handle_input(text), loop)

    def cancel_run(self) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(self._cancel_trace)

    def configure_resource(self, kind: str, name: str, enabled: bool) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(
            self._configure_resource(kind, name, enabled), loop,
        )

    def reload_resources(self) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(
            self._reload_resources(), loop,
        )

    def select_agent(self, agent_id: str) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(
            self._select_agent(agent_id), loop,
        )

    def configure_permission(self, mode: str) -> None:
        loop = self._require_loop()
        asyncio.run_coroutine_threadsafe(
            self._configure_permission(mode), loop,
        )

    def _require_configuration_idle(self) -> tuple[AsyncSecClient, ChatCommandRegistry]:
        client, commands = self._client, self._commands
        if commands is None or client is None:
            raise RuntimeError("Sec Client is not ready")
        if self._configuring:
            raise RuntimeError("配置正在更新，请稍后重试。")
        if self._active_trace is not None and not self._active_trace.done:
            raise RuntimeError("请先停止或完成当前任务，再修改配置。")
        return client, commands

    async def _configure_permission(self, mode: str) -> None:
        try:
            _client, commands = self._require_configuration_idle()
            if mode not in {"normal", "plan", "auto"}:
                raise ValueError("未知权限模式")
            outcome = await commands.dispatch(f"/mode {mode}")
            if outcome is None:
                raise RuntimeError("权限命令不可用")
            self.configuration_result.emit(outcome.message or "", outcome.display_kind or "info")
        except Exception as error:
            self.configuration_result.emit(f"权限配置失败：{error}", "error")
        finally:
            self._publish_resources()

    async def _configure_resource(self, kind: str, name: str, enabled: bool) -> None:
        owns_update = False
        try:
            client, commands = self._client, self._commands
            if client is None or commands is None:
                raise RuntimeError("Sec Client is not ready")
            if self._configuring:
                raise RuntimeError("配置正在更新，请稍后重试。")
            if kind in {"skill", "prompt", "extension", "mcp", "worker"}:
                if kind == "extension" or getattr(client, "capability_generation", 0) == 0:
                    self._require_configuration_idle()
                self._configuring = True
                owns_update = True
            if kind in {"skill", "prompt"}:
                setter = getattr(client, "set_resource_enabled", None)
                if callable(setter):
                    await setter(kind, name, enabled)
                    manager = client.resources
                else:
                    manager = self._resources
                    if manager is None:
                        raise RuntimeError("资源尚未就绪")
                    resource_kind: Literal["skill", "prompt"] = (
                        "skill" if kind == "skill" else "prompt"
                    )
                    manager.set_enabled(resource_kind, name, enabled)
                self._resources = manager
                commands.replace_resource_commands(_resource_command_specs(manager))
                self.command_completions.emit(commands.completion_items())
            elif kind == "mcp":
                setter = getattr(client, "set_mcp_enabled", None)
                if not callable(setter):
                    raise RuntimeError("MCP 配置不可用")
                report = await setter(name, enabled)
                self.configuration_result.emit(
                    f"MCP 服务 {name} 已{'启用' if enabled else '停用'}。\n"
                    f"{format_reload_report(report)}",
                    "info",
                )
            elif kind == "worker":
                names = set(client.worker_extensions)
                if enabled:
                    names.add(name)
                else:
                    names.discard(name)
                await client.set_worker_extensions(tuple(sorted(names)))
            elif kind == "extension":
                previous_extensions = client.enabled_extensions
                action = "enable" if enabled else "disable"
                outcome = await commands.dispatch(f"/reload {action} {shlex.quote(name)}")
                if outcome is None or outcome.display_kind == "error":
                    raise RuntimeError(outcome.message if outcome else "扩展命令不可用")
                try:
                    if self._extension_settings is None:
                        raise RuntimeError("扩展配置存储尚未就绪")
                    self._extension_settings.save(name, enabled)
                except Exception as save_error:
                    rollback_action = "enable" if name in previous_extensions else "disable"
                    rollback = await commands.dispatch(
                        f"/reload {rollback_action} {shlex.quote(name)}"
                    )
                    if rollback is None or rollback.display_kind == "error":
                        raise RuntimeError(
                            f"保存失败：{save_error}；恢复扩展失败："
                            f"{rollback.message if rollback else '命令不可用'}"
                        ) from save_error
                    raise
            else:
                raise ValueError(f"unsupported resource kind: {kind!r}")
            if kind != "mcp":
                self.configuration_result.emit(
                    f"{name} 已{'启用' if enabled else '停用'}，配置已保存。", "info",
                )
        except Exception as error:
            self.configuration_result.emit(f"资源配置失败：{error}", "error")
        finally:
            if owns_update:
                self._configuring = False
            self._publish_resources()

    async def _reload_resources(self) -> None:
        try:
            client, commands = self._client, self._commands
            if client is None or commands is None:
                raise RuntimeError("Sec Client is not ready")
            report = await client.reload_resources()
            self._resources = client.resources
            commands.replace_resource_commands(_resource_command_specs(self._resources))
            self.command_completions.emit(commands.completion_items())
            self.configuration_result.emit(format_reload_report(report), "info")
        except Exception as error:
            self.configuration_result.emit(f"资源重新加载失败：{error}", "error")
        finally:
            self._publish_resources()

    async def _select_agent(self, agent_id: str) -> None:
        try:
            client = self._client
            if client is None:
                raise RuntimeError("Sec Client is not ready")
            await client.select_agent(agent_id)
            pending = client.pending_agent_id
            message = (
                f"Agent {agent_id} queued for the next Trace."
                if pending == agent_id
                else f"Agent {agent_id} selected."
            )
            self.configuration_result.emit(message, "info")
        except Exception as error:
            self.configuration_result.emit(f"Agent selection failed: {error}", "error")
        finally:
            self._publish_resources()

    def rename_session(self, session_id: str, name: str) -> None:
        asyncio.run_coroutine_threadsafe(
            self._rename_session(session_id, name), self._require_loop()
        )

    def submit_control(self, command: str, text: str) -> None:
        if command not in {"steer", "followup"}:
            raise ValueError("unsupported Trace control command")
        self.submit_input(f"/{command} {shlex.quote(text)}")

    def refresh_sessions(self) -> None:
        asyncio.run_coroutine_threadsafe(self._refresh_sessions(), self._require_loop())

    def session_action(
        self, action: str, session_id: str, path: str | None = None,
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            self._session_action(action, session_id, path), self._require_loop()
        )

    async def _session_action(
        self, action: str, session_id: str, path: str | None = None,
    ) -> None:
        try:
            client = self._client
            if client is None:
                raise RuntimeError("Sec Client is not ready")
            if action == "open":
                await client.switch_session(session_id)
            elif action == "fork":
                await client.fork_session(session_id)
            elif action == "clone":
                await client.clone_session(session_id)
            elif action == "delete":
                await client.delete_session(session_id)
            elif action == "export":
                if not path:
                    raise ValueError("export path must not be empty")
                snapshot = await client.get_session_snapshot(session_id)
                written = await asyncio.to_thread(write_session_file, snapshot, Path(path))
                self.command_result.emit(f"会话已导出：{written}", "info")
            else:
                raise ValueError(f"unsupported Session action: {action}")
            if action != "export":
                self._on_session_changed(client.session.id)
            await self._refresh_sessions()
        except Exception as error:
            self.command_result.emit(f"会话操作失败：{error}", "error")

    async def _rename_session(self, session_id: str, name: str) -> None:
        try:
            if self._client is None:
                raise RuntimeError("Sec Client is not ready")
            await self._client.rename_session(session_id, name)
        except Exception as error:
            self.command_result.emit(f"重命名会话失败：{error}", "error")
            return
        await self._refresh_sessions()

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
        self._extension_settings = ExtensionSettings(
            self.workspace / ".sec" / "desktop-extensions.json",
        )
        async with AsyncSecClient(
            "sec-desktop",
            "You are Sec, a helpful desktop assistant. Be concise and transparent.",
            tools=create_builtin_tools(self.workspace),
            policy=self._policy,
            workspace=self.workspace,
            enabled_extensions=self._extension_settings.resolve(self.enabled_extensions),
        ) as client:
            self._client = client
            self._resources = client.resources
            self._commands = create_chat_commands(
                client,
                get_active_trace=lambda: self._active_trace,
                permission_policy=self._policy,
                on_session_change=self._on_session_changed,
                resource_manager=self._resources,
                refresh_completions=self.command_completions.emit,
            )
            self.command_completions.emit(self._commands.completion_items())
            self._publish_resources()
            self.session_changed.emit(client.session.id)
            self._publish_session_history()
            await self._refresh_sessions()
            self._publish_traces()
            self.ready.emit(str(self.workspace))
            if self._shutdown_requested.is_set():
                return
            await self._shutdown.wait()
            if self._active_trace is not None and not self._active_trace.done:
                self._active_trace.cancel()
        self._client = None
        self._commands = None

    async def _handle_input(self, text: str) -> None:
        if self._configuring:
            self.command_result.emit("配置正在更新，请完成后重新发送。", "error")
            return
        commands = self._commands
        if commands is not None and text.lstrip().startswith("/"):
            previous_session_id = self._client.session.id if self._client is not None else None
            try:
                outcome = await commands.dispatch(text)
            except Exception as error:
                self.command_result.emit(f"Command failed: {error}", "error")
                return
            if outcome is not None:
                await self._handle_command_outcome(outcome)
                if (
                    self._client is not None
                    and self._client.session.id != previous_session_id
                    and not self._client.session.messages
                    and outcome.display_kind != "error"
                ):
                    self._publish_session_history()
                self._publish_resources()
                await self._refresh_sessions()
                return

        run = self._active_trace
        if run is not None and not run.done:
            if run.follow_up(text):
                self.command_result.emit(
                    "Queued after the active assistant turn.",
                    "follow_up",
                )
            else:
                self.command_result.emit(
                    "The active Trace no longer accepts follow-up input.",
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
            run = self._active_trace
            if run is not None and not run.done:
                if not run.follow_up(outcome.run_prompt):
                    self.command_result.emit(
                        "The active Trace no longer accepts follow-up input.",
                        "error",
                    )
                elif outcome.display_prompt is not None and self._client is not None:
                    self._queued_resource_inputs.append((
                        outcome.run_prompt, outcome.display_prompt,
                        frozenset(entry.id for entry in self._client.session.entries),
                    ))
            else:
                if outcome.display_prompt is not None:
                    try:
                        await self._record_display_prompt(outcome.run_prompt, outcome.display_prompt)
                    except Exception as error:
                        self.command_result.emit(f"保存资源输入记录失败，未启动任务：{error}", "error")
                        return
                await self._run_prompt(outcome.run_prompt)

    async def _record_display_prompt(self, prompt: str, display: str) -> None:
        if self._client is None:
            raise RuntimeError("Sec Client is not ready")
        session = self._client.session
        await session.append_entry(CustomMessageEntry(
            id=uuid4().hex, parent_id=session.active_leaf_id, timestamp=time(),
            custom_type="sec.desktop.resource_input", content=display, display=False,
            details={"prompt": prompt},
        ))

    async def _run_prompt(self, prompt: str) -> None:
        client = self._client
        if client is None:
            self.run_failed.emit("Sec Client is not ready.")
            return
        if self._active_trace is not None and not self._active_trace.done:
            self.run_failed.emit("A Sec Trace is already active.")
            return

        run = client.run(prompt)
        self._active_trace = run
        self._prompt_task = asyncio.current_task()
        recorder: TraceRecorder | None = None
        trace_status = "failed"
        try:
            recorder = TraceRecorder(self._trace_store, run.id, client.session.id)
        except Exception as error:
            self.command_result.emit(
                f"Trace 未能开始记录（{type(error).__name__}）；Trace 将继续。",
                "error",
            )
        self.trace_updated.emit(run.id)
        self._publish_traces(run.id)
        self.status_changed.emit("正在运行")
        try:
            async for event in run.events():
                if recorder is not None:
                    try:
                        recorder.record(event)
                        self.trace_updated.emit(run.id)
                    except Exception as error:
                        recorder = None
                        self.command_result.emit(
                            f"Trace 写入失败（{type(error).__name__}）；后续事件未记录，Trace 将继续。",
                            "error",
                        )
                if isinstance(event, AgentSettledEvent):
                    trace_status = event.status
                if event.type == "message_update":
                    message_event = event.assistant_message_event
                    if message_event.type == "start":
                        self._publish_session_history()
                        self.assistant_started.emit()
                    elif message_event.type == "text_delta":
                        self.text_delta.emit(message_event.delta)
                elif event.type == "message_end":
                    self._publish_session_history()
                elif event.type == "tool_approval_requested":
                    await self._request_approval(
                        run,
                        event.tool_call_id,
                        event.tool_name,
                        event.args,
                        event.reason,
                    )
                elif event.type == "extension_ui_requested":
                    await self._request_extension_ui(
                        run,
                        event.request_id,
                        event.extension_name,
                        event.request,
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
            trace_status = "cancelled"
            self.status_changed.emit("已停止")
        except Exception as error:
            trace_status = "failed"
            self.run_failed.emit(str(error))
            self.status_changed.emit("运行失败")
        finally:
            if not run.done:
                run.cancel()
            try:
                await run.result()
            except asyncio.CancelledError:
                pass
            except Exception as error:
                self.command_result.emit(f"任务结束：{error}", "error")
            if self._pending_approval_id is not None:
                try:
                    await self._record_approval(
                        self._pending_approval_id, self._pending_approval_name or "",
                        "cancelled", "审批已取消，未授权执行。",
                    )
                except Exception as error:
                    self.command_result.emit(f"保存审批取消记录失败：{error}", "error")
            try:
                for target_id, display in self._queued_resource_displays().items():
                    session = client.session
                    await session.append_entry(CustomMessageEntry(
                        id=uuid4().hex, parent_id=session.active_leaf_id, timestamp=time(),
                        custom_type="sec.desktop.resource_input", content=display, display=False,
                        details={"target_id": target_id},
                    ))
            except Exception as error:
                self.command_result.emit(f"保存资源输入显示记录失败：{error}", "error")
            if recorder is not None:
                try:
                    recorder.finish(trace_status)
                except Exception as error:
                    self.command_result.emit(
                        f"Trace 完成状态写入失败（{type(error).__name__}）。",
                        "error",
                    )
            self._queued_resource_inputs.clear()
            self._active_trace = None
            self._prompt_task = None
            self._pending_approval_id = None
            self._pending_approval_name = None
            self._approval_future = None
            self._approval_cancelled = False
            self._publish_session_history()
            await self._refresh_sessions()
            self.trace_updated.emit(run.id)
            self._publish_traces()
            self.run_finished.emit()

    def _on_session_changed(self, session_id: str) -> None:
        self.session_changed.emit(session_id)
        self._publish_session_history()
        self._publish_traces()

    def _publish_session_history(self) -> None:
        client = self._client
        if client is None:
            return
        messages: list[tuple[str, str]] = []
        timestamps: list[float] = []

        def append_record(role: str, text: str, timestamp: float) -> None:
            messages.append((role, text))
            timestamps.append(timestamp)
        display_prompt: tuple[str, str] | None = None
        resource_displays = self._queued_resource_displays()
        for entry in client.session.snapshot.active_path:
            if (
                isinstance(entry, CustomMessageEntry)
                and entry.custom_type == "sec.desktop.resource_input"
                and isinstance(entry.content, str)
                and isinstance(entry.details, dict)
                and isinstance(entry.details.get("target_id"), str)
            ):
                resource_displays[entry.details["target_id"]] = entry.content
        completed_calls = {
            entry.message.tool_call_id
            for entry in client.session.snapshot.active_path
            if isinstance(entry, MessageEntry) and entry.message.role == "tool_result"
        }
        for entry in client.session.snapshot.active_path:
            if isinstance(entry, MessageEntry):
                message = entry.message
                if message.role in {"user", "assistant"} and message.text.strip():
                    text = message.text
                    if message.role == "user":
                        if entry.id in resource_displays:
                            text = resource_displays[entry.id]
                        elif display_prompt is not None and text == display_prompt[0]:
                            text = display_prompt[1]
                        else:
                            text = self._legacy_resource_display(text)
                        display_prompt = None
                    append_record(message.role, text, entry.timestamp)
                if message.role == "assistant":
                    for tool in message.tool_uses:
                        append_record(
                            "activity" if tool.id in completed_calls else "process",
                            f"调用工具 · {tool.name}\n"
                            + json.dumps(tool.arguments, ensure_ascii=False, indent=2),
                            entry.timestamp,
                        )
                elif message.role == "tool_result":
                    append_record(
                        "activity",
                        f"工具{'失败' if message.is_error else '完成'} · {message.tool_name}\n{message.text}",
                        entry.timestamp,
                    )
            elif (
                isinstance(entry, CustomMessageEntry)
                and entry.custom_type == "sec.desktop.resource_input"
                and isinstance(entry.content, str)
                and isinstance(entry.details, dict)
                and isinstance(entry.details.get("prompt"), str)
            ):
                display_prompt = (entry.details["prompt"], entry.content)
            elif (
                isinstance(entry, CustomMessageEntry)
                and entry.custom_type == "sec.desktop.approval"
                and isinstance(entry.content, str)
            ):
                details = entry.details if isinstance(entry.details, dict) else {}
                if details.get("outcome") == "requested":
                    if details.get("tool_call_id") == self._pending_approval_id:
                        continue
                    append_record("activity", entry.content + "\n历史请求，仅供回看，不可重新授权。", entry.timestamp)
                else:
                    append_record("activity", entry.content, entry.timestamp)
        self.session_timing.emit(tuple(timestamps))
        self.session_history.emit(tuple(messages))

    def _queued_resource_displays(self) -> dict[str, str]:
        client = self._client
        result: dict[str, str] = {}
        if client is None:
            return result
        for prompt, display, previous in self._queued_resource_inputs:
            for entry in client.session.snapshot.active_path:
                if (
                    isinstance(entry, MessageEntry) and entry.message.role == "user"
                    and entry.id not in previous and entry.id not in result
                    and entry.message.text == prompt
                ):
                    result[entry.id] = display
                    break
        return result

    def _legacy_resource_display(self, text: str) -> str:
        manager = self._resources
        if manager is None:
            return text
        snapshot = manager.available_snapshot
        for resource in (*snapshot.skills, *snapshot.prompts):
            prefix = resource.content + "\n\nUser request:\n"
            command = f"/{resource.kind}:{resource.name}"
            if text.startswith(prefix):
                return command + " " + text[len(prefix):]
            if text == resource.content or (
                resource.kind == "skill"
                and text == resource.content + "\n\nApply this Skill to the current project."
            ):
                return command
        return text

    async def _record_approval(
        self, call_id: str, name: str, outcome: str, content: str,
    ) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("Sec Client is not ready")
        details: JSONObject = {"tool_call_id": call_id, "tool_name": name, "outcome": outcome}
        await client.session.append_entry(CustomMessageEntry(
            id=uuid4().hex, parent_id=client.session.active_leaf_id, timestamp=time(),
            custom_type="sec.desktop.approval", content=content, display=True, details=details,
        ))

    def _publish_resources(self) -> None:
        client = self._client
        workspace_capabilities = (
            client is not None and getattr(client, "capability_generation", 0) > 0
        )
        if workspace_capabilities:
            self._resources = client.resources
        if self._resources is None:
            return
        snapshot = self._resources.available_snapshot
        entries = [
            ToolEntry(
                item.kind, item.name, item.description,
                f"来源：{item.path}\n\n{item.content}",
                self._resources.is_enabled(item.kind, item.name),
            )
            for item in (*snapshot.prompts, *snapshot.skills)
        ]
        if workspace_capabilities:
            selected_agent = client.selected_agent_id
            pending_agent = client.pending_agent_id
            for card in client.agent_cards:
                state = (
                    "Queued for the next Trace."
                    if card.id == pending_agent
                    else "Currently selected."
                    if card.id == selected_agent
                    else ""
                )
                description = card.description
                if card.id == pending_agent:
                    description += " · 已排队，将在下一次 Trace 生效"
                entries.append(ToolEntry(
                    "agent",
                    card.id,
                    description,
                    f"{card.name}\n{card.description}\n\n{state}".strip(),
                    card.id == selected_agent,
                ))

            for status in client.mcp_statuses:
                server_id = str(_status_value(status, "id", "server_id", "name", default="unknown"))
                name = str(_status_value(status, "name", "label", default=server_id))
                state = str(_status_value(status, "status", "state", default="unknown"))
                enabled_state = _status_value(status, "enabled", default=False)
                error = _status_value(status, "error", "message", default="")
                details = [f"Service: {name}", f"Status: {state}"]
                if error:
                    details.append(f"Details: {error}")
                entries.append(ToolEntry(
                    "mcp",
                    server_id,
                    f"{name} · {state}",
                    "\n".join(details),
                    bool(enabled_state),
                ))

            for tool in client.available_tools:
                builtin, origin_kind, origin_name, source_label = _classify_tool(tool)
                description = tool.definition.description or ""
                content = f"{source_label} · 风险等级 {tool.risk_level}"
                if description:
                    content += f"\n\n{description}"
                entries.append(ToolEntry(
                    "tool",
                    tool.definition.name,
                    description,
                    content,
                    True,
                    builtin=builtin,
                    origin_kind=origin_kind,
                    origin_name=origin_name,
                ))

        if client is not None:
            enabled = client.enabled_extensions
            workers = client.worker_extensions if workspace_capabilities else ()
            available = client.discover_extensions()
            installed = {
                point.name: point for point in entry_points(group="sec.extensions")
            }
            for name in sorted(set(available) | set(enabled) | set(workers)):
                point = installed.get(name)
                distribution = point.dist if point is not None else None
                summary = (
                    distribution.metadata.get("Summary", "SEC 扩展")
                    if distribution is not None else "SEC 扩展"
                )
                source = (
                    f"安装入口：{point.value}\n" if point is not None
                    else "安装入口未找到\n"
                )
                if distribution is not None:
                    source += (
                        f"发行包：{distribution.metadata['Name']}\n"
                        f"版本：{distribution.version}\n"
                    )
                entries.append(ToolEntry(
                    "extension", name, summary,
                    source + "\n"
                    "worker 模式只接受工具，可重新加载 Python 代码；"
                    "进程内模式支持工具、命令等完整贡献，但代码更新需要重启宿主。\n"
                    "切换执行方式前须先停用当前插件。\n"
                    "预览不会导入未启用的扩展。工具执行仍受全局权限策略控制。",
                    name in enabled or name in workers,
                    "worker" if name in workers or (
                        workspace_capabilities and name not in enabled
                    ) else "in_process",
                ))
        self.resources_updated.emit(ToolInventory(
            tuple(entries),
            self._policy.mode_name if self._policy is not None else "normal",
            self._configuring or (self._active_trace is not None and not self._active_trace.done),
        ))

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
        run: Trace,
        tool_call_id: str,
        tool_name: str,
        args: object,
        reason: str | None,
    ) -> None:
        future = asyncio.get_running_loop().create_future()
        self._pending_approval_id = tool_call_id
        self._pending_approval_name = tool_name
        self._approval_future = future
        self._approval_cancelled = False
        arguments = json.dumps(args, ensure_ascii=False, indent=2)
        self._approval_writing = True
        try:
            await self._record_approval(
                tool_call_id, tool_name, "requested",
                f"审批请求 · {tool_name}\n{arguments}\n{reason or '需要用户确认'}",
            )
        finally:
            self._approval_writing = False
        if self._approval_cancelled:
            run.cancel()
            return
        self._publish_session_history()
        self.approval_requested.emit(
            tool_call_id,
            tool_name,
            arguments,
        )
        self.status_changed.emit(f"等待工具审批：{reason or tool_name}")
        approved = await future
        if run.done or self._approval_cancelled:
            return
        self._approval_writing = True
        try:
            await self._record_approval(
                tool_call_id, tool_name, "approved" if approved else "denied",
                f"审批{'允许' if approved else '拒绝'} · {tool_name}",
            )
        finally:
            self._approval_writing = False
        if self._approval_cancelled:
            run.cancel()
            return
        if approved:
            accepted = run.approve_tool(tool_call_id)
        else:
            accepted = run.deny_tool(tool_call_id, "The desktop user denied this tool call.")
        if not accepted:
            raise RuntimeError("审批请求已失效，未能提交处理结果。")
        self._pending_approval_id = None
        self._pending_approval_name = None
        self._approval_future = None
        self._publish_session_history()

    def _cancel_trace(self) -> None:
        self._approval_cancelled = True
        self._ui_cancelled = True
        run = self._active_trace
        if run is not None and not self._approval_writing:
            run.cancel()
        future = self._approval_future
        if future is not None and not future.done():
            future.set_result(False)
        ui_future = self._ui_future
        if ui_future is not None and not ui_future.done():
            ui_future.set_result({"type": "extension_ui_response", "cancelled": True})

    def _resolve_approval(self, tool_call_id: str, approved: bool) -> None:
        future = self._approval_future
        if (
            tool_call_id != self._pending_approval_id
            or future is None
            or future.done()
        ):
            return
        future.set_result(approved)

    async def _request_extension_ui(
        self,
        run: Trace,
        request_id: str,
        extension_name: str,
        request: dict,
    ) -> None:
        if request.get("method") == "notify":
            self.status_changed.emit(
                f"扩展 {extension_name}：{request.get('message', '')}"
            )
            return
        future = asyncio.get_running_loop().create_future()
        self._pending_ui_id = request_id
        self._ui_future = future
        self._ui_cancelled = False
        self.extension_ui_requested.emit(request_id, request)
        self.status_changed.emit(f"等待扩展 UI 输入：{extension_name}")
        response = await future
        if run.done or self._ui_cancelled:
            return
        if response is None:
            response = {"type": "extension_ui_response", "id": request_id, "cancelled": True}
        run.respond_ui(request_id, response)
        self._pending_ui_id = None
        self._ui_future = None

    def resolve_ui(self, request_id: str, response: dict) -> None:
        loop = self._require_loop()
        loop.call_soon_threadsafe(self._resolve_ui, request_id, response)

    def _resolve_ui(self, request_id: str, response: dict) -> None:
        future = self._ui_future
        if (
            request_id != self._pending_ui_id
            or future is None
            or future.done()
        ):
            return
        future.set_result(response)
