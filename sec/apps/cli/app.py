"""装配原生 Client，并管理交互式与管道式 CLI 生命周期。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
from importlib.metadata import entry_points
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values
from rich.console import Console

from sec import (
    AsyncSecClient,
    ConfigError,
    MessageEntry,
    Trace,
    SessionEntry,
    SessionStoreError,
    SessionSummary,
)
from sec.apps.cli.chat_commands import (
    ChatCommand,
    ChatCommandOutcome,
    ChatCommandRegistry,
    create_default_chat_commands,
    format_reload_report,
)
from sec.apps.cli.presentation import (
    show_event,
    show_noninteractive_event,
    welcome_panel,
)
from sec.apps.cli.resources import CliResourceManager, ResourceSnapshot
from sec.apps.cli.session_io import (
    copy_to_clipboard,
    read_session_file,
    write_session_file,
)
from sec.apps.cli.slash_commands import (
    SlashCommand,
    SlashCommandError,
    SlashCommandRegistry,
    command_registry,
)
from sec.apps.cli.terminal import TerminalUI
from sec.extensions import ExtensionLoadError, ExtensionManager
from sec.policies import PermissionMode, PermissionModePolicy
from sec.tools._builtins import create_builtin_tools

DEFAULT_INSTRUCTIONS = (
    "You are Sec, a helpful AI assistant. Follow the user's instructions, "
    "be clear and concise, and be transparent about uncertainty."
)
FAREWELL_MESSAGE = "Consider it done. See you in a second."
ERROR_CONSOLE = Console(stderr=True, no_color=True, force_terminal=False)


def _farewell_message(session_id: str) -> str:
    """在退出提示中保留当前 Session ID，便于稍后恢复对话。"""
    return f"{FAREWELL_MESSAGE}\nSession ID: {session_id}"


def main(
    argv: Sequence[str] | None = None,
    *,
    enabled_extensions: Sequence[str] = (),
) -> int:
    """解析 sec 命令；无命令时启动聊天，slash command 保持一次性执行。"""
    parser = argparse.ArgumentParser(prog="sec")
    parser.add_argument(
        "--session",
        help="resume a previously persisted session",
    )
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        help="enable an installed sec.extensions package (repeatable)",
    )
    parser.add_argument(
        "--mode",
        choices=("plan", "normal", "auto"),
        default="normal",
        help="tool permission mode (default: normal)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="run a slash command or the dashboard",
    )
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    extensions = (*enabled_extensions, *arguments.extension)

    if arguments.command:
        if arguments.session:
            parser.error("--session can only be used with the interactive chat")
        if arguments.command[0] == "dashboard":
            ERROR_CONSOLE.print(
                "The dashboard application is not implemented yet.",
                markup=False,
            )
            return 2
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if extensions:
                try:
                    return asyncio.run(
                        _dispatch_with_extensions(
                            tuple(arguments.command),
                            extensions,
                        )
                    )
                except (ExtensionLoadError, ValueError) as exc:
                    ERROR_CONSOLE.print(f"sec: {exc}", markup=False)
                    return 2
            return _dispatch_command(tuple(arguments.command))
        raise RuntimeError("the sec CLI cannot run inside an active event loop")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_chat(arguments.session, extensions, arguments.mode))
    raise RuntimeError("the sec CLI cannot run inside an active event loop")


def _dispatch_command(arguments: tuple[str, ...]) -> int:
    """执行已注册的一次性命令并统一报告命令解析错误。"""
    try:
        return command_registry.dispatch(arguments)
    except SlashCommandError as exc:
        ERROR_CONSOLE.print(f"sec: {exc}", markup=False)
        return 2


async def _dispatch_with_extensions(
    arguments: tuple[str, ...],
    enabled_extensions: Sequence[str],
) -> int:
    """显式加载扩展后执行其一次性 CLI 命令。"""
    manager = ExtensionManager()
    await manager.load(enabled_extensions)
    registry = SlashCommandRegistry()
    for command in command_registry.commands:
        registry.register(command)
    try:
        for command in manager.catalog.commands:
            registry.register(command)
        try:
            return registry.dispatch(arguments)
        except SlashCommandError as exc:
            ERROR_CONSOLE.print(f"sec: {exc}", markup=False)
            return 2
    finally:
        await manager.aclose()


async def _chat(
    resume_session_id: str | None,
    enabled_extensions: Sequence[str],
    permission_mode: PermissionMode = "normal",
) -> int:
    """启动 Client；根据标准输入/输出是否为 TTY 选择交互模式。"""
    workspace_root = Path.cwd()
    mode_policy = PermissionModePolicy(
        permission_mode,
        workspace_root=workspace_root,
    )
    try:
        async with AsyncSecClient(
            "sec",
            DEFAULT_INSTRUCTIONS,
            workspace=workspace_root,
            session_id=resume_session_id,
            enabled_extensions=enabled_extensions,
            tools=create_builtin_tools(workspace_root),
            policy=mode_policy,
        ) as client:
            resource_manager = client.resources
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                await _chat_noninteractive(
                    client,
                    mode_policy,
                    resource_manager=resource_manager,
                )
            else:
                await _chat_interactive(
                    client,
                    mode_policy,
                    resource_manager=resource_manager,
                    enabled_extensions=enabled_extensions,
                )
    except (ConfigError, ExtensionLoadError, SessionStoreError, ValueError) as exc:
        ERROR_CONSOLE.print(f"sec: {exc}", markup=False)
        return 2
    return 0


async def _chat_noninteractive(
    client: AsyncSecClient,
    mode_policy: PermissionModePolicy | None = None,
    *,
    resource_manager: CliResourceManager | None = None,
) -> None:
    """逐行读取管道输入并流式输出；无法交互审批的工具默认拒绝。"""
    output = Console(no_color=True, force_terminal=False)
    commands = create_chat_commands(
        client,
        active_run=None,
        permission_policy=mode_policy,
        resource_manager=resource_manager,
    )

    for raw_line in sys.stdin:
        user_message = raw_line.rstrip("\r\n")
        if not user_message.strip():
            continue
        output.print(f"you > {user_message}", markup=False)
        try:
            outcome = await commands.dispatch(user_message)
        except Exception as exc:
            output.print(f"Command failed: {exc}", markup=False)
            continue
        if outcome is not None:
            if outcome.message:
                output.print(outcome.message, markup=False)
            if outcome.should_exit:
                break
            if outcome.run_prompt is None:
                continue
            user_message = outcome.run_prompt

        run = client.run(user_message)
        events_task = asyncio.create_task(
            _consume_noninteractive_events(output, run)
        )
        try:
            result = await run.result()
        except Exception as exc:
            if not events_task.done():
                events_task.cancel()
                await asyncio.gather(events_task, return_exceptions=True)
            output.print(f"\nRun failed: {exc}", markup=False)
        else:
            try:
                await events_task
            except Exception as exc:
                output.print(f"\nRun event stream failed: {exc}", markup=False)
            output.print(
                f"\n[Trace completed: {result.model_calls} model call(s), "
                f"{result.tool_calls} tool call(s)]",
                markup=False,
            )

    if client.session.messages:
        output.print(
            f"To resume this session: sec --session {client.session.id}",
            markup=False,
        )


async def _consume_noninteractive_events(
    output: Console,
    run: Trace,
) -> None:
    async for event in run.events():
        show_noninteractive_event(output, run, event)


async def _chat_interactive(
    client: AsyncSecClient,
    mode_policy: PermissionModePolicy | None = None,
    *,
    resource_manager: CliResourceManager | None = None,
    enabled_extensions: Sequence[str] = (),
) -> None:
    """启动旧版 Rich 输入体验，并把输入映射到原生 Trace 控制接口。"""
    home = str(Path.cwd())
    model = _configured_model()
    if resource_manager is None:
        if getattr(client, "agent_cards", ()):
            resource_manager = client.resources
        else:
            resource_manager = CliResourceManager(Path.cwd())
            resource_manager.reload()
    resources = resource_manager.snapshot
    selected_agent_id = getattr(client, "selected_agent_id", None)
    cards = getattr(client, "agent_cards", ())
    selected_agent = next(
        (
            f"{card.name} ({card.id})"
            for card in cards
            if card.id == selected_agent_id
        ),
        selected_agent_id,
    )
    pending_agent_id = getattr(client, "pending_agent_id", None)
    ui = TerminalUI(
        home=home,
        model=model,
        session_id=client.session.id,
    )
    ui.add_rich(
        welcome_panel(
            home=home,
            model=model,
            session_id=client.session.id,
            resources=resources,
            enabled_extensions=enabled_extensions,
            selected_agent=selected_agent,
            pending_agent_id=pending_agent_id,
            permission_mode=(
                "normal" if mode_policy is None else mode_policy.mode_name
            ),
        )
    )

    active_run: Trace | None = None
    active_task: asyncio.Task[None] | None = None
    trace_number = 0
    exit_after_active = False

    commands = create_chat_commands(
        client,
        get_active_trace=lambda: active_run,
        permission_policy=mode_policy,
        on_session_change=getattr(ui, "set_session_id", None),
        resource_manager=resource_manager,
        refresh_completions=getattr(ui, "set_command_completions", None),
    )
    ui.set_command_completions(commands.completion_items())
    application_task = asyncio.create_task(ui.run())

    def start_run(prompt: str) -> None:
        nonlocal active_run, active_task, trace_number
        trace_number += 1
        run = client.run(prompt)
        active_run = run
        ui.set_working(True)
        ui.add_status("trace", f"[Trace #{trace_number}] started")
        active_task = asyncio.create_task(
            _watch_run(ui, run, trace_number=trace_number)
        )

    input_task: asyncio.Task[str] | None = asyncio.create_task(
        ui.input_queue.get()
    )
    try:
        while input_task is not None:
            waiting: set[asyncio.Task[str] | asyncio.Task[None]] = {
                input_task,
                application_task,
            }
            if active_task is not None:
                waiting.add(active_task)
            completed, _ = await asyncio.wait(
                waiting,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if application_task in completed:
                application_task.result()
                break

            if active_task is not None and active_task in completed:
                active_task.result()
                active_run = None
                active_task = None
                ui.set_working(False)
                if exit_after_active:
                    ui.add_rich(_farewell_message(client.session.id))
                    ui.stop()
                    break

            if input_task in completed:
                user_message = input_task.result()
                input_task = None
                if ui.resolve_confirmation(user_message):
                    input_task = asyncio.create_task(ui.input_queue.get())
                    continue
                if ui.resolve_prompt(user_message):
                    input_task = asyncio.create_task(ui.input_queue.get())
                    continue

                if user_message.strip():
                    ui.add_user_message(user_message)
                    try:
                        outcome = await commands.dispatch(user_message)
                    except Exception as exc:
                        ui.add_status("error", f"Command failed: {exc}")
                        outcome = None
                        if user_message.lstrip().startswith("/"):
                            input_task = asyncio.create_task(
                                ui.input_queue.get()
                            )
                            continue

                    if outcome is not None:
                        if outcome.message:
                            ui.add_status(
                                outcome.display_kind or "info",
                                outcome.message,
                            )
                        if outcome.should_exit:
                            if active_run is None:
                                ui.add_rich(
                                    _farewell_message(client.session.id)
                                )
                                ui.stop()
                                break
                            exit_after_active = True
                            active_run.cancel()
                            ui.add_status(
                                "info",
                                "Cancelling the active Trace before exit.",
                            )
                        elif outcome.run_prompt is not None:
                            if exit_after_active:
                                ui.add_status(
                                    "error",
                                    "Cannot start a resource prompt while exit is pending.",
                                )
                            elif active_run is not None and not active_run.done:
                                if not active_run.follow_up(outcome.run_prompt):
                                    ui.add_status(
                                        "error",
                                        "The active Trace no longer accepts follow-up input.",
                                    )
                            else:
                                start_run(outcome.run_prompt)
                        input_task = asyncio.create_task(ui.input_queue.get())
                        continue

                    if exit_after_active:
                        ui.add_status("info", "Exit is pending; input ignored.")
                    elif active_run is not None and not active_run.done:
                        if active_run.follow_up(user_message):
                            ui.add_status(
                                "follow_up",
                                "Queued after the active assistant turn.",
                            )
                        else:
                            ui.add_status(
                                "error",
                                "The active Trace no longer accepts follow-up input.",
                            )
                    else:
                        start_run(user_message)

                input_task = asyncio.create_task(ui.input_queue.get())
    finally:
        ui.stop()
        if input_task is not None and not input_task.done():
            input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)
        if active_run is not None and not active_run.done:
            active_run.cancel()
        if active_task is not None and not active_task.done():
            await active_task
        if not application_task.done():
            await application_task


async def _watch_run(
    ui: TerminalUI,
    run: Trace,
    *,
    trace_number: int,
) -> None:
    """并行观察 Trace 结果与事件，并报告两条通道各自的错误。"""
    events_task = asyncio.create_task(
        _consume_events(ui, run, trace_number=trace_number)
    )
    try:
        result = await run.result()
    except asyncio.CancelledError:
        events_task.cancel()
        await asyncio.gather(events_task, return_exceptions=True)
        raise
    except Exception as exc:
        ui.finish_turn()
        ui.add_status("error", f"[Trace #{trace_number}] failed: {exc}")
    else:
        ui.finish_turn()
        ui.add_status(
            "trace",
            f"[Trace #{trace_number}] complete: {result.model_calls} model call(s), "
            f"{result.tool_calls} tool call(s).",
        )
    try:
        await events_task
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ui.finish_turn()
        ui.add_status(
            "error",
            f"[Trace #{trace_number}] event stream failed: {exc}",
        )


async def _consume_events(
    ui: TerminalUI,
    run: Trace,
    *,
    trace_number: int,
) -> None:
    async for event in run.events():
        await show_event(ui, run, event, trace_number=trace_number)


def create_chat_commands(
    client: AsyncSecClient,
    *,
    active_run: Trace | None = None,
    get_active_trace: Callable[[], Trace | None] | None = None,
    permission_policy: PermissionModePolicy | None = None,
    on_session_change: Callable[[str], None] | None = None,
    resource_manager: CliResourceManager | None = None,
    refresh_completions: Callable[[Sequence[tuple[str, str]]], None] | None = None,
) -> ChatCommandRegistry:
    """Build the shared interactive command registry for a Sec Client."""
    get_run = get_active_trace or (lambda: active_run)
    if resource_manager is None:
        if getattr(client, "agent_cards", ()):
            resource_manager = client.resources
        else:
            resource_manager = CliResourceManager(Path.cwd())
            resource_manager.reload()

    def current_resource_manager() -> CliResourceManager:
        cards = getattr(client, "agent_cards", ())
        if cards:
            return client.resources
        return resource_manager

    def enqueue_follow_up(text: str) -> str | None:
        run = get_run()
        if run is None or not run.follow_up(text):
            return None
        return "Queued after the active assistant turn."

    def enqueue_steering(text: str) -> str | None:
        run = get_run()
        if run is None or not run.steer(text):
            return None
        return "Steering message submitted to the active Trace."

    def request_stop() -> str | None:
        run = get_run()
        if run is None or not run.cancel():
            return None
        return "Cancellation requested for the active Trace."

    async def new_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /new", display_kind="error")
        session = await client.switch_session()
        if on_session_change is not None:
            on_session_change(session.id)
        return ChatCommandOutcome(f"Started new Session {session.id}.")

    async def resume_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) > 1:
            return ChatCommandOutcome(
                "Usage: /resume [session-id]",
                display_kind="error",
            )
        if not args:
            summaries = await client.list_sessions()
            if not summaries:
                return ChatCommandOutcome("No saved Sessions were found.")
            lines = ["Recent Sessions:"]
            for summary in summaries:
                label = f" — {summary.name}" if summary.name else ""
                updated = (
                    ""
                    if summary.updated_at is None
                    else " · "
                    + datetime.fromtimestamp(
                        summary.updated_at,
                        tz=timezone.utc,
                    ).strftime("%Y-%m-%d %H:%M UTC")
                )
                lines.append(
                    f"  {summary.session_id}{label}{updated}"
                )
            lines.append("Use /resume <session-id> to switch.")
            return ChatCommandOutcome("\n".join(lines))
        try:
            session = await client.switch_session(args[0])
        except KeyError:
            return ChatCommandOutcome(
                f"Session {args[0]!r} was not found.",
                display_kind="error",
            )
        if on_session_change is not None:
            on_session_change(session.id)
        return ChatCommandOutcome(f"Resumed Session {session.id}.")

    def session_info(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /session", display_kind="error")
        snapshot = client.session.snapshot
        summary = SessionSummary.from_snapshot(snapshot)
        children: dict[str, int] = {}
        for entry in snapshot.entries:
            if entry.parent_id is not None:
                children[entry.parent_id] = children.get(entry.parent_id, 0) + 1
        branch_points = sum(child_count > 1 for child_count in children.values())
        name = summary.name or "(unnamed)"
        message_count = len(snapshot.messages)
        return ChatCommandOutcome(
            f"Session: {client.session.id}\n"
            f"Name: {name}\n"
            f"Version: {client.session.version}\n"
            f"Entries: {len(snapshot.entries)} · Messages: {message_count}\n"
            f"Branches: {branch_points} · Active leaf: "
            f"{snapshot.active_leaf_id or '(empty)'}"
        )

    async def rename_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if not args:
            name = SessionSummary.from_snapshot(client.session.snapshot).name
            return ChatCommandOutcome(
                f"Session name: {name or '(unnamed)'}"
            )
        active = get_run()
        if active is not None and not active.done:
            return ChatCommandOutcome(
                "Finish or stop the active Trace before renaming the Session.",
                display_kind="error",
            )
        name = " ".join(args).strip()
        if not name:
            return ChatCommandOutcome("Usage: /name [name]", display_kind="error")
        await client.rename_session(client.session.id, name)
        return ChatCommandOutcome(f"Session renamed to {name!r}.")

    def session_tree(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /tree", display_kind="error")
        return ChatCommandOutcome(
            _format_session_tree(
                client.session.entries,
                client.session.active_leaf_id,
            )
        )

    async def fork_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /fork", display_kind="error")
        session = await client.fork_session()
        if on_session_change is not None:
            on_session_change(session.id)
        return ChatCommandOutcome(f"Forked active branch into Session {session.id}.")

    async def clone_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /clone", display_kind="error")
        session = await client.clone_session()
        if on_session_change is not None:
            on_session_change(session.id)
        return ChatCommandOutcome(f"Cloned Session tree into {session.id}.")

    def export_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) > 1:
            return ChatCommandOutcome(
                "Usage: /export [path]",
                display_kind="error",
            )
        path = (
            Path(args[0]).expanduser()
            if args
            else _default_export_path(client.session.id)
        )
        try:
            written = write_session_file(client.session.snapshot, path)
        except FileExistsError:
            return ChatCommandOutcome(
                f"Refusing to overwrite existing file: {path}",
                display_kind="error",
            )
        except OSError as exc:
            return ChatCommandOutcome(
                f"Could not export Session to {path}: {exc}",
                display_kind="error",
            )
        return ChatCommandOutcome(f"Exported Session to {written}.")

    async def import_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) != 1:
            return ChatCommandOutcome(
                "Usage: /import <path>",
                display_kind="error",
            )
        path = Path(args[0]).expanduser()
        try:
            snapshot = read_session_file(path)
        except OSError as exc:
            return ChatCommandOutcome(
                f"Could not read Session file {path}: {exc}",
                display_kind="error",
            )
        except ValueError as exc:
            return ChatCommandOutcome(
                f"Could not import Session file {path}: {exc}",
                display_kind="error",
            )
        session = await client.import_session(snapshot)
        if on_session_change is not None:
            on_session_change(session.id)
        entry_word = "entry" if len(session.entries) == 1 else "entries"
        return ChatCommandOutcome(
            f"Imported {len(session.entries)} {entry_word} into Session {session.id}."
        )

    def copy_session(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /copy", display_kind="error")
        text = "\n\n".join(message.text for message in client.session.messages)
        if not text:
            return ChatCommandOutcome(
                "The active Session has no message text to copy.",
                display_kind="error",
            )
        try:
            copy_to_clipboard(text)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return ChatCommandOutcome(
                f"Could not copy to clipboard: {exc}",
                display_kind="error",
            )
        return ChatCommandOutcome(f"Copied {len(text)} characters to the clipboard.")

    commands: ChatCommandRegistry

    async def reload_resources(args: tuple[str, ...]) -> ChatCommandOutcome:
        nonlocal resource_manager
        if args and (len(args) < 2 or args[0] not in {"enable", "disable"}):
            return ChatCommandOutcome(
                "Usage: /reload [enable|disable <extension-name...>]",
                display_kind="error",
            )
        if args and not args[1:]:
            return ChatCommandOutcome(
                f"Usage: /reload {args[0]} <extension-name...>",
                display_kind="error",
            )
        active_run = get_run()
        if args and active_run is not None and not active_run.done:
            return ChatCommandOutcome(
                "Finish or stop the active Trace before changing Extensions.",
                display_kind="error",
            )

        extension_message = ""
        previous_enabled = client.enabled_extensions
        try:
            if args:
                action, *names = args
                current = previous_enabled
                if len(names) != len(set(names)):
                    return ChatCommandOutcome(
                        "Extension names must not be repeated.",
                        display_kind="error",
                    )
                if action == "enable":
                    desired = tuple(dict.fromkeys((*current, *names)))
                else:
                    missing = [name for name in names if name not in current]
                    if missing:
                        return ChatCommandOutcome(
                            f"Extension(s) not enabled: {', '.join(missing)}",
                            display_kind="error",
                        )
                    desired = tuple(name for name in current if name not in names)
                await client.set_enabled_extensions(desired)
                extension_message = (
                    f"Enabled {', '.join(names)}."
                    if action == "enable"
                    else f"Disabled {', '.join(names)}."
                )
            else:
                pass
        except (ConfigError, ExtensionLoadError, ValueError, RuntimeError, ExceptionGroup) as exc:
            return ChatCommandOutcome(
                f"Could not reload Extensions: {exc}",
                display_kind="error",
            )

        try:
            commands.replace_extension_commands(_extension_chat_commands(client))
        except ValueError as command_error:
            if client.enabled_extensions != previous_enabled:
                try:
                    await client.set_enabled_extensions(previous_enabled)
                    commands.replace_extension_commands(
                        _extension_chat_commands(client)
                    )
                except (
                    ConfigError,
                    ExtensionLoadError,
                    ValueError,
                    RuntimeError,
                    ExceptionGroup,
                ) as rollback_error:
                    return ChatCommandOutcome(
                        "Extension command registration failed and the previous "
                        f"set could not be restored: {command_error}; {rollback_error}",
                        display_kind="error",
                    )
            return ChatCommandOutcome(
                f"Extension command registration failed: {command_error}",
                display_kind="error",
            )

        if not args and callable(getattr(client, "reload_resources", None)):
            try:
                report = await client.reload_resources()
            except Exception as exc:
                return ChatCommandOutcome(
                    f"Could not reload resources: {exc}",
                    display_kind="error",
                )
            resource_manager = current_resource_manager()
            extension_message = format_reload_report(report)
        else:
            snapshot = resource_manager.reload()
            resource_message = (
                f"Reloaded {len(snapshot.skills)} Skill(s) and "
                f"{len(snapshot.prompts)} Prompt(s)."
            )
            extension_message = " ".join(
                part for part in (resource_message, extension_message) if part
            )
        if not args:
            available = client.discover_extensions()
            enabled = set(previous_enabled)
            all_names = tuple(sorted(set(available) | enabled))
            extension_message += (
                "\nInstalled Extensions:\n"
                + (
                    "\n".join(
                        f"  {name} ({'enabled' if name in enabled else 'disabled'})"
                        if name in available
                        else f"  {name} (enabled, entry point not found)"
                        for name in all_names
                    )
                    if all_names
                    else "  (none)"
                )
                + "\nUse /reload enable <name...> or "
                "/reload disable <name...>."
            )
        commands.replace_resource_commands(
            _resource_command_specs(current_resource_manager())
        )
        if refresh_completions is not None:
            refresh_completions(commands.completion_items())
        return ChatCommandOutcome(extension_message)

    def help_details() -> str:
        return _format_resource_help(
            current_resource_manager().snapshot,
            client.extensions.commands,
        )

    def list_agents(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /agents", display_kind="error")
        cards = getattr(client, "agent_cards", ())
        if not cards:
            return ChatCommandOutcome(
                "Agent selection is unavailable in this client.",
                display_kind="error",
            )
        selected = getattr(client, "selected_agent_id", "default")
        pending = getattr(client, "pending_agent_id", None)
        lines = ["Available Agents:"]
        for card in cards:
            marker = " (selected)" if card.id == selected else ""
            if card.id == pending:
                marker += " (queued for next Trace)"
            lines.append(f"  {card.id}{marker} — {card.name}: {card.description}")
        lines.append("Use /agent <id> to select an Agent.")
        return ChatCommandOutcome("\n".join(lines))

    async def select_agent(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) != 1:
            return ChatCommandOutcome("Usage: /agent <id>", display_kind="error")
        selector = getattr(client, "select_agent", None)
        if not callable(selector):
            return ChatCommandOutcome(
                "Agent selection is unavailable in this client.",
                display_kind="error",
            )
        try:
            await selector(args[0])
        except (ValueError, RuntimeError) as exc:
            return ChatCommandOutcome(f"Could not select Agent: {exc}", display_kind="error")
        pending = getattr(client, "pending_agent_id", None)
        if pending == args[0]:
            return ChatCommandOutcome(f"Agent {args[0]} queued for the next Trace.")
        return ChatCommandOutcome(f"Agent {args[0]} selected.")

    def mcp_status_line(status: object) -> str:
        def value(*names: str, default: object = "") -> object:
            if isinstance(status, dict):
                return next((status[name] for name in names if name in status), default)
            return next(
                (getattr(status, name) for name in names if hasattr(status, name)),
                default,
            )
        identifier = value("id", "server_id", "name", default="(unknown)")
        label = value("name", "label", default=identifier)
        state = value("status", "state", default="unknown")
        enabled = value("enabled", default=None)
        error = value("error", "message", default="")
        line = f"  {identifier} — {label}: {state}"
        if enabled is not None:
            line += f" ({'enabled' if enabled else 'disabled'})"
        if error:
            line += f" — {error}"
        return line

    async def manage_mcp(args: tuple[str, ...]) -> ChatCommandOutcome:
        action = args[0].casefold() if args else "list"
        if action == "list" and len(args) == 1 or not args:
            statuses = getattr(client, "mcp_statuses", ())
            lines = ["MCP services:"]
            lines.extend(mcp_status_line(status) for status in statuses)
            if not statuses:
                lines.append("  No MCP services are configured.")
            lines.append("Use /mcp enable|disable <id> or /mcp refresh.")
            return ChatCommandOutcome("\n".join(lines))
        if action == "refresh" and len(args) == 1:
            reload_method = getattr(client, "reload_resources", None)
            if not callable(reload_method):
                return ChatCommandOutcome(
                    "MCP refresh is unavailable in this client.",
                    display_kind="error",
                )
            try:
                report = await reload_method()
            except Exception as exc:
                return ChatCommandOutcome(f"Could not refresh MCP services: {exc}", display_kind="error")
            return ChatCommandOutcome(format_reload_report(report))
        if action in {"enable", "disable"} and len(args) == 2:
            setter = getattr(client, "set_mcp_enabled", None)
            if not callable(setter):
                return ChatCommandOutcome(
                    "MCP configuration is unavailable in this client.",
                    display_kind="error",
                )
            try:
                report = await setter(args[1], action == "enable")
            except Exception as exc:
                return ChatCommandOutcome(f"Could not {action} MCP service {args[1]!r}: {exc}", display_kind="error")
            return ChatCommandOutcome(
                f"MCP service {args[1]} {action}d.\n{format_reload_report(report)}"
            )
        return ChatCommandOutcome(
            "Usage: /mcp [list|enable <id>|disable <id>|refresh]",
            display_kind="error",
        )

    async def manage_plugins(args: tuple[str, ...]) -> ChatCommandOutcome:
        action = args[0].casefold() if args else "list"
        names = tuple(getattr(client, "worker_extensions", ()))
        if action == "list" and len(args) == 1 or not args:
            installed = tuple(sorted(
                point.name for point in entry_points(group="sec.worker_extensions")
            ))
            all_names = tuple(sorted(set(installed) | set(names)))
            lines = ["Worker extensions (isolated tools only):"]
            lines.extend(
                f"  {name} ({'enabled' if name in names else 'disabled'})"
                for name in all_names
            )
            if not all_names:
                lines.append("  (none installed)")
            lines.append("Use /plugins enable|disable <name...> or /plugins reload.")
            return ChatCommandOutcome("\n".join(lines))
        if action == "reload" and len(args) == 1:
            reload_method = getattr(client, "reload_resources", None)
            if not callable(reload_method):
                return ChatCommandOutcome(
                    "Worker extension reload is unavailable in this client.",
                    display_kind="error",
                )
            try:
                report = await reload_method()
            except Exception as exc:
                return ChatCommandOutcome(f"Could not reload worker extensions: {exc}", display_kind="error")
            return ChatCommandOutcome(format_reload_report(report))
        if action in {"enable", "disable"} and len(args) >= 2:
            setter = getattr(client, "set_worker_extensions", None)
            if not callable(setter):
                return ChatCommandOutcome(
                    "Worker extension configuration is unavailable in this client.",
                    display_kind="error",
                )
            requested = args[1:]
            if len(requested) != len(set(requested)):
                return ChatCommandOutcome("Extension names must not be repeated.", display_kind="error")
            if action == "enable":
                desired = tuple(dict.fromkeys((*names, *requested)))
            else:
                missing = tuple(name for name in requested if name not in names)
                if missing:
                    return ChatCommandOutcome(
                        f"Worker extension(s) not enabled: {', '.join(missing)}",
                        display_kind="error",
                    )
                desired = tuple(name for name in names if name not in requested)
            try:
                report = await setter(desired)
            except Exception as exc:
                return ChatCommandOutcome(
                    f"Could not {action} worker extension(s): {exc}",
                    display_kind="error",
                )
            return ChatCommandOutcome(
                f"Worker extension(s) {action}d: {', '.join(requested)}.\n"
                + format_reload_report(report)
            )
        return ChatCommandOutcome(
            "Usage: /plugins [list|enable <name...>|disable <name...>|reload]",
            display_kind="error",
        )

    commands = create_default_chat_commands(
        enqueue_follow_up=enqueue_follow_up,
        enqueue_steering=enqueue_steering,
        request_stop=request_stop,
        get_permission_mode=(
            None if permission_policy is None else lambda: permission_policy.mode_name
        ),
        change_permission_mode=(
            None
            if permission_policy is None
            else lambda mode: _change_permission_mode(
                permission_policy,
                mode,
                get_run(),
            )
        ),
        session_commands={
            "new": new_session,
            "resume": resume_session,
            "name": rename_session,
            "session": session_info,
            "tree": session_tree,
            "fork": fork_session,
            "clone": clone_session,
        },
        additional_commands={
            "reload": (
                "Reload Skills/Prompts and list or change enabled Extensions",
                reload_resources,
            ),
            "agents": ("List available Agents and the current selection", list_agents),
            "agent": ("Select the Agent for this and subsequent Runs", select_agent),
            "mcp": ("List, enable, disable, or refresh MCP services", manage_mcp),
            "plugins": ("Configure isolated worker extensions", manage_plugins),
            "export": ("Export the current Session to JSON", export_session),
            "import": ("Import a Session from a JSON file", import_session),
            "copy": ("Copy active conversation text to the clipboard", copy_session),
        },
        resource_commands=_resource_command_specs(current_resource_manager()),
        help_details=help_details,
    )
    commands.replace_extension_commands(_extension_chat_commands(client))
    return commands


def _extension_chat_commands(client: AsyncSecClient) -> tuple[ChatCommand, ...]:
    """Adapt currently enabled extension commands to the interactive registry."""
    commands: list[ChatCommand] = []
    for extension_command in client.extensions.commands:
        def invoke(
            args: tuple[str, ...],
            command=extension_command,
        ) -> ChatCommandOutcome:
            captured = StringIO()
            with redirect_stdout(captured), redirect_stderr(captured):
                status = command.handler(args)
            if type(status) is not int:
                raise TypeError(
                    f"extension command /{command.name} must return an integer exit code"
                )
            message = captured.getvalue().rstrip()
            if status != 0:
                failure = f"/{command.name} exited with status {status}"
                message = f"{message}\n{failure}".strip()
            return ChatCommandOutcome(
                message=message or None,
                display_kind="info" if status == 0 else "error",
            )

        commands.append(
            ChatCommand(
                extension_command.name,
                extension_command.description,
                invoke,
            )
        )
    return tuple(commands)


def _default_export_path(session_id: str) -> Path:
    digest = sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return Path.cwd() / ".sec" / "sessions" / f"session-{digest}.json"


def _resource_command_specs(
    resource_manager: CliResourceManager,
) -> tuple[
    tuple[str, str, str, Callable[[tuple[str, ...]], ChatCommandOutcome]],
    ...,
]:
    specs: list[
        tuple[str, str, str, Callable[[tuple[str, ...]], ChatCommandOutcome]]
    ] = []
    snapshot = resource_manager.snapshot
    for resource in (*snapshot.skills, *snapshot.prompts):
        def invoke(
            args: tuple[str, ...],
            *,
            kind: str = resource.kind,
            name: str = resource.name,
        ) -> ChatCommandOutcome:
            current = resource_manager.get(kind, name)
            if current is None:
                return ChatCommandOutcome(
                    f"{kind.title()} {name!r} is no longer available; use /reload.",
                    display_kind="error",
                )
            prompt = current.content
            request = " ".join(args).strip()
            if request:
                prompt += f"\n\nUser request:\n{request}"
            elif kind == "skill":
                prompt += "\n\nApply this Skill to the current project."
            return ChatCommandOutcome(
                f"Loaded {kind.title()} {name}.",
                run_prompt=prompt,
                display_prompt=f"/{kind}:{name}" + (f" {request}" if request else ""),
            )

        specs.append(
            (
                resource.kind,
                resource.name,
                resource.description,
                invoke,
            )
        )
    return tuple(specs)


def _format_resource_help(
    snapshot: ResourceSnapshot,
    extension_commands: Sequence[SlashCommand],
) -> str:
    lines = ["Resources:"]
    lines.append(
        "  Skills: "
        + (
            ", ".join(f"{item.name} ({item.description})" for item in snapshot.skills)
            if snapshot.skills
            else "none"
        )
    )
    lines.append(
        "  Prompts: "
        + (
            ", ".join(f"{item.name} ({item.description})" for item in snapshot.prompts)
            if snapshot.prompts
            else "none"
        )
    )
    lines.append(
        "  Extensions: "
        + (
            ", ".join(command.name for command in extension_commands)
            if extension_commands
            else "none enabled"
        )
    )
    if snapshot.issues:
        lines.append("Resource issues:")
        lines.extend(f"  {issue.path}: {issue.message}" for issue in snapshot.issues)
    else:
        lines.append(
            "Resource directories: .sec/skills, .sec/prompts, "
            "~/.sec/skills, ~/.sec/prompts."
        )
    return "\n".join(lines)


def _format_session_tree(
    entries: Sequence[SessionEntry],
    active_leaf_id: str | None,
) -> str:
    if not entries:
        return "Session tree is empty."
    children: dict[str | None, list[SessionEntry]] = {}
    child_counts: dict[str, int] = {}
    by_id = {entry.id: entry for entry in entries}
    for entry in entries:
        children.setdefault(entry.parent_id, []).append(entry)
        if entry.parent_id is not None:
            child_counts[entry.parent_id] = child_counts.get(entry.parent_id, 0) + 1
    active_path: set[str] = set()
    current = by_id.get(active_leaf_id) if active_leaf_id is not None else None
    while current is not None:
        active_path.add(current.id)
        current = by_id.get(current.parent_id) if current.parent_id else None

    lines = ["Session tree:"]
    stack = [(entry, 0) for entry in reversed(children.get(None, []))]
    while stack:
        entry, depth = stack.pop()
        marker = "*" if entry.id in active_path else " "
        branch = " ├─" if child_counts.get(entry.id, 0) > 1 else " └─"
        label = entry.type
        if isinstance(entry, MessageEntry):
            text = entry.message.text.replace("\n", " ").strip()
            if text:
                label += f": {text[:72]}"
                if len(text) > 72:
                    label += "..."
        lines.append(f"{'  ' * depth}{marker}{branch} {label} [{entry.id[:8]}]")
        stack.extend(
            (child, depth + 1)
            for child in reversed(children.get(entry.id, []))
        )
    lines.append("* marks the active branch.")
    return "\n".join(lines)


def _change_permission_mode(
    policy: PermissionModePolicy,
    mode: str,
    active_run: Trace | None,
) -> str:
    """切换工具权限模式；活动 Trace 中不改变正在使用的授权规则。"""
    if active_run is not None and not active_run.done:
        return "Finish or stop the active Trace before changing permission mode."
    if mode == "plan":
        policy.set_mode("plan")
    elif mode == "normal":
        policy.set_mode("normal")
    elif mode == "auto":
        policy.set_mode("auto")
    else:
        return "Usage: /mode [plan|normal|auto]"
    return f"Tool permission mode set to {mode}."


def _configured_model() -> str:
    """读取模型名称用于 UI 展示，不触碰或输出凭据。"""
    environment = {**dotenv_values(".env"), **os.environ}
    provider = environment.get("SEC_PROVIDER") or "openai"
    prefix = provider.upper().replace("-", "_")
    return (
        environment.get("SEC_MODEL")
        or environment.get(f"{prefix}_MODEL")
        or "not configured"
    )


# Preserve the original internal import used by the CLI's focused tests and
# downstream integrations while exposing the command factory for other hosts.
_create_commands = create_chat_commands

__all__ = ["create_chat_commands", "main"]
