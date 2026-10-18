"""Register and dispatch executable slash commands for the sec CLI."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sec.apps.cli.command_catalog import CommandCatalog, CommandDefinition

CommandHandler = Callable[[tuple[str, ...]], int]


class SlashCommandError(ValueError):
    """A CLI slash command invocation is malformed or unknown."""


@dataclass(frozen=True)
class SlashCommand:
    """An executable CLI command with a name and user-facing description."""

    name: str
    description: str
    handler: CommandHandler = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]*", self.name) is None:
            raise ValueError("slash command name must be lowercase kebab-case")
        if not self.description.strip():
            raise ValueError("slash command description must not be empty")
        if not callable(self.handler):
            raise TypeError("slash command handler must be callable")


class SlashCommandRegistry:
    """Store uniquely named CLI slash commands and invoke registered handlers."""

    def __init__(self) -> None:
        self._catalog = CommandCatalog()
        self._commands: dict[str, SlashCommand] = {}

    @property
    def commands(self) -> tuple[SlashCommand, ...]:
        """Return the registered commands in registration order."""
        return tuple(self._commands.values())

    def completion_items(self) -> tuple[tuple[str, str], ...]:
        """Return command names and descriptions for interactive completion."""
        return self._catalog.completion_items()

    def help_text(self) -> str:
        """Return the shared help text for registered commands."""
        return self._catalog.help_text()

    def register(self, command: SlashCommand) -> None:
        """Register a command, rejecting duplicate names."""
        if self._catalog.get(command.name) is not None:
            raise ValueError(f"slash command {command.name!r} is already registered")
        definition = self._catalog.register(
            CommandDefinition(command.name, command.description)
        )
        self._commands[definition.name] = SlashCommand(
            definition.name,
            definition.description,
            command.handler,
        )

    def get(self, name: str) -> SlashCommand | None:
        """Look up a command by its name without the leading slash."""
        canonical = self._catalog.canonical_name(name)
        return None if canonical is None else self._commands[canonical]

    def dispatch(self, arguments: Sequence[str]) -> int:
        """Dispatch an argv-style invocation such as ``('/help', 'run')``."""
        if isinstance(arguments, str):
            raise TypeError("slash command arguments must be a sequence of tokens")
        if any(not isinstance(argument, str) for argument in arguments):
            raise TypeError("slash command arguments must all be strings")
        if not arguments:
            raise SlashCommandError("a slash command is required")
        token = arguments[0]
        if not token.startswith("/") or token == "/":
            raise SlashCommandError("CLI commands must start with '/'")
        name = token[1:]
        canonical = self._catalog.canonical_name(name)
        command = None if canonical is None else self._commands[canonical]
        if command is None:
            raise SlashCommandError(f"unknown slash command: /{name}")
        status = command.handler(tuple(arguments[1:]))
        if type(status) is not int:
            raise TypeError(f"slash command /{name} must return an integer exit code")
        return status


command_registry = SlashCommandRegistry()


def _show_help(arguments: tuple[str, ...]) -> int:
    if len(arguments) > 1:
        raise SlashCommandError("usage: /help [command]")
    if arguments:
        name = arguments[0].removeprefix("/")
        command = command_registry.get(name)
        if command is None:
            raise SlashCommandError(f"unknown slash command: /{name}")
        print(f"/{command.name}: {command.description}")
        return 0
    print(command_registry.help_text())
    return 0


command_registry.register(
    SlashCommand("help", "Show available commands or details for one command.", _show_help)
)

__all__ = [
    "SlashCommand",
    "SlashCommandError",
    "SlashCommandRegistry",
    "command_registry",
]