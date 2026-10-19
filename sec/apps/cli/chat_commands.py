"""定义交互式聊天命令及其异步分发行为。"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Literal

from sec.apps.cli.command_catalog import CommandCatalog, CommandDefinition

CommandDisplayKind = Literal["follow_up", "steer", "should_stop", "info", "error"]
CommandHandler = Callable[
    [tuple[str, ...]], "ChatCommandOutcome | Awaitable[ChatCommandOutcome]"
]


@dataclass(frozen=True)
class ChatCommandOutcome:
    """描述聊天命令的提示内容及是否结束交互。"""

    message: str | None = None
    should_exit: bool = False
    display_kind: CommandDisplayKind | None = None
    run_prompt: str | None = None
    display_prompt: str | None = None


@dataclass(frozen=True)
class ChatCommand:
    """保存交互式 slash command 的名称、说明、处理器和别名。"""

    name: str
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()


class ChatCommandRegistry:
    """注册聊天命令，支持帮助文本、补全和同步/异步处理器。"""

    def __init__(self) -> None:
        self._catalog = CommandCatalog()
        self._commands: dict[str, ChatCommand] = {}
        self._resource_commands: dict[str, ChatCommand] = {}
        self._extension_commands: dict[str, ChatCommand] = {}

    def register(self, command: ChatCommand) -> None:
        definition = self._catalog.register(
            CommandDefinition(
                command.name,
                command.description,
                command.aliases,
            )
        )
        registered = ChatCommand(
            name=definition.name,
            description=definition.description,
            handler=command.handler,
            aliases=definition.aliases,
        )
        self._commands[definition.name] = registered

    def completion_items(self) -> tuple[tuple[str, str], ...]:
        """返回可供终端输入框显示的命令补全项。"""
        return (
            *self._catalog.completion_items(),
            *(
                (f"/{name}", command.description)
                for name, command in self._resource_commands.items()
            ),
            *(
                (f"/{name}", command.description)
                for name, command in self._extension_commands.items()
            ),
        )

    def help_text(self) -> str:
        """返回当前已注册命令及说明。"""
        text = self._catalog.help_text()
        if self._resource_commands:
            text += "\n\nResources:"
            text += "".join(
                f"\n  /{name} — {command.description}"
                for name, command in self._resource_commands.items()
            )
        if self._extension_commands:
            text += "\n\nExtensions:"
            text += "".join(
                f"\n  /{name} — {command.description}"
                for name, command in self._extension_commands.items()
            )
        return text

    def replace_resource_commands(
        self,
        commands: Sequence[tuple[str, str, str, CommandHandler]],
    ) -> None:
        """Replace dynamically discovered /skill:name and /prompt:name commands."""
        registered: dict[str, ChatCommand] = {}
        for namespace, name, description, handler in commands:
            if namespace not in {"skill", "prompt"}:
                raise ValueError(f"unsupported resource command namespace: {namespace!r}")
            normalized_name = CommandCatalog.normalize_name(name)
            full_name = f"{namespace}:{normalized_name}"
            if full_name in registered:
                raise ValueError(f"resource command {full_name!r} is duplicated")
            registered[full_name] = ChatCommand(full_name, description, handler)
        self._resource_commands = registered

    def replace_extension_commands(
        self,
        commands: Sequence[ChatCommand],
    ) -> None:
        """Replace commands contributed by currently enabled extensions."""
        registered: dict[str, ChatCommand] = {}
        for command in commands:
            name = CommandCatalog.normalize_name(command.name)
            if self._catalog.get(name) is not None:
                raise ValueError(
                    f"extension command {name!r} conflicts with a built-in command"
                )
            if name in registered:
                raise ValueError(f"extension command {name!r} is duplicated")
            registered[name] = ChatCommand(
                name,
                command.description,
                command.handler,
            )
        self._extension_commands = registered

    async def dispatch(self, text: str) -> ChatCommandOutcome | None:
        """执行 slash command；普通对话文本返回 None。"""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None

        try:
            parts = shlex.split(stripped)
        except ValueError as exc:
            return ChatCommandOutcome(
                f"Invalid command syntax: {exc}",
                display_kind="error",
            )
        if not parts:
            return ChatCommandOutcome("Empty command. Type /help for commands.")

        entered_name = parts[0][1:].casefold()
        if ":" in entered_name:
            command = self._resource_commands.get(entered_name)
            if command is None:
                return ChatCommandOutcome(
                    f"Unknown resource command '/{entered_name}'. "
                    "Type /help for available resources.",
                    display_kind="error",
                )
            return await _invoke(command, tuple(parts[1:]))
        command_name = self._catalog.canonical_name(entered_name)
        command = (
            self._commands[command_name]
            if command_name is not None
            else self._extension_commands.get(entered_name)
        )
        if command is None:
            return ChatCommandOutcome(
                f"Unknown command '/{entered_name}'. Type /help for commands.",
                display_kind="error",
            )
        return await _invoke(command, tuple(parts[1:]))

    @staticmethod
    def _normalize_name(name: str) -> str:
        return CommandCatalog.normalize_name(name)


def create_default_chat_commands(
    *,
    enqueue_follow_up: Callable[[str], str | None] | None = None,
    enqueue_steering: Callable[[str], str | None] | None = None,
    request_stop: Callable[[], str | None] | None = None,
    get_permission_mode: Callable[[], str] | None = None,
    change_permission_mode: Callable[[str], str | None] | None = None,
    session_commands: Mapping[str, CommandHandler] | None = None,
    additional_commands: Mapping[
        str,
        tuple[str, CommandHandler],
    ] | None = None,
    resource_commands: Sequence[tuple[str, str, str, CommandHandler]] = (),
    help_details: Callable[[], str] | None = None,
) -> ChatCommandRegistry:
    """将内建聊天命令绑定到当前活动 Run。"""
    commands = ChatCommandRegistry()

    def show_help(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args not in ((), ("full",)):
            return ChatCommandOutcome("Usage: /help", display_kind="error")
        details = "" if help_details is None else help_details()
        if args:
            details = (
                "Shortcuts: Ctrl+C cancels the active Run; Ctrl+D exits; "
                "Tab completes commands; Ctrl+O opens this expanded help.\n\n"
                + details
            )
        return ChatCommandOutcome(
            "\n\n".join(part for part in (commands.help_text(), details) if part)
        )

    def queue_message(
        args: tuple[str, ...],
        command_name: str,
        enqueue: Callable[[str], str | None] | None,
    ) -> ChatCommandOutcome:
        if not args:
            return ChatCommandOutcome(f"Usage: /{command_name} <message>")
        if enqueue is None:
            return ChatCommandOutcome(f"/{command_name} requires an active Run.")
        status = enqueue(" ".join(args))
        if status is None:
            return ChatCommandOutcome(f"/{command_name} requires an active Run.")
        display_kind: CommandDisplayKind = (
            "follow_up" if command_name == "followup" else "steer"
        )
        return ChatCommandOutcome(status, display_kind=display_kind)

    def follow_up(args: tuple[str, ...]) -> ChatCommandOutcome:
        return queue_message(args, "followup", enqueue_follow_up)

    def steer(args: tuple[str, ...]) -> ChatCommandOutcome:
        return queue_message(args, "steer", enqueue_steering)

    def stop(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /stop", display_kind="error")
        if request_stop is None:
            return ChatCommandOutcome("/stop requires an active Run.")
        status = request_stop()
        if status is None:
            return ChatCommandOutcome("/stop requires an active Run.")
        return ChatCommandOutcome(status, display_kind="should_stop")

    def exit_chat(args: tuple[str, ...]) -> ChatCommandOutcome:
        if args:
            return ChatCommandOutcome("Usage: /exit", display_kind="error")
        return ChatCommandOutcome(should_exit=True)

    def permission_mode(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) > 1:
            return ChatCommandOutcome(
                "Usage: /mode [plan|normal|auto]",
                display_kind="error",
            )
        if get_permission_mode is None or change_permission_mode is None:
            return ChatCommandOutcome("/mode is not available in this client.")
        if not args:
            return ChatCommandOutcome(
                f"Tool permission mode: {get_permission_mode()}."
            )
        result = change_permission_mode(args[0])
        failed = (
            not result
            or result.startswith("Usage:")
            or "Finish or stop" in result
        )
        return ChatCommandOutcome(
            result or "Could not change the tool permission mode.",
            display_kind=(
                "error"
                if failed
                else "info"
            ),
        )

    def workspace_trust(args: tuple[str, ...]) -> ChatCommandOutcome:
        if len(args) > 1 or (args and args[0].casefold() not in {"on", "off"}):
            return ChatCommandOutcome(
                "Usage: /trust [on|off]",
                display_kind="error",
            )
        if get_permission_mode is None or change_permission_mode is None:
            return ChatCommandOutcome("/trust is not available in this client.")
        if not args:
            state = "enabled" if get_permission_mode() == "auto" else "disabled"
            return ChatCommandOutcome(
                f"Workspace file auto-approval is {state} for this chat only. "
                "Bash and remote capabilities still require approval."
            )
        enable = args[0].casefold() == "on"
        result = change_permission_mode("auto" if enable else "normal")
        failed = not result or "Finish or stop" in result
        if failed:
            return ChatCommandOutcome(
                result or "Could not change workspace trust.",
                display_kind="error",
            )
        state = "enabled" if enable else "disabled"
        return ChatCommandOutcome(
            f"Workspace file auto-approval {state} for this chat only. "
            "Bash and remote capabilities still require approval.",
            display_kind="info",
        )

    commands.register(ChatCommand("help", "Show available commands", show_help))
    commands.register(
        ChatCommand(
            "followup",
            "Queue a message after the current assistant turn",
            follow_up,
            aliases=("follow-up", "follow"),
        )
    )
    commands.register(
        ChatCommand("steer", "Steer the active assistant turn", steer)
    )
    commands.register(ChatCommand("stop", "Cancel the active Run", stop))
    if get_permission_mode is not None and change_permission_mode is not None:
        commands.register(
            ChatCommand(
                "mode",
                "Show or change the tool permission mode",
                permission_mode,
            )
        )
        commands.register(
            ChatCommand(
                "trust",
                "Toggle session-only workspace file auto-approval",
                workspace_trust,
            )
        )
    commands.register(
        ChatCommand(
            "exit",
            "Leave the chat",
            exit_chat,
            aliases=("quit", "q"),
        )
    )
    session_definitions = (
        ("new", "Start a new Session"),
        ("resume", "List or resume a saved Session"),
        ("name", "Show or set the current Session name"),
        ("session", "Show current Session information"),
        ("tree", "Show the current Session branch tree"),
        ("fork", "Copy the active branch into a new Session"),
        ("clone", "Copy the full Session tree into a new Session"),
    )
    for name, description in session_definitions:
        handler = None if session_commands is None else session_commands.get(name)
        if handler is not None:
            commands.register(ChatCommand(name, description, handler))
    for name, (description, handler) in (additional_commands or {}).items():
        commands.register(ChatCommand(name, description, handler))
    commands.replace_resource_commands(resource_commands)
    return commands


def format_reload_report(report: object) -> str:
    """Render the public reload summary without depending on a report class."""
    generation = getattr(report, "generation", None)
    changes = tuple(
        f"{label}: {', '.join(getattr(report, field, ()))}"
        for label, field in (
            ("added", "added"),
            ("updated", "updated"),
            ("removed", "removed"),
        )
        if getattr(report, field, ())
    )
    parts = [f"Resources reloaded (generation {generation})." if generation is not None else "Resources reloaded."]
    parts.extend(changes)
    pending = tuple(getattr(report, "pending", ()))
    issues = tuple(getattr(report, "issues", ()))
    if pending:
        parts.append("Pending: " + "; ".join(pending))
    if issues:
        parts.append("Issues: " + "; ".join(issues))
    return "\n".join(parts)


async def _invoke(
    command: ChatCommand,
    args: tuple[str, ...],
) -> ChatCommandOutcome:
    outcome = command.handler(args)
    if isinstance(outcome, Awaitable):
        return await outcome
    return outcome


__all__ = [
    "ChatCommand",
    "ChatCommandOutcome",
    "ChatCommandRegistry",
    "create_default_chat_commands",
    "format_reload_report",
]
