"""集中维护 CLI 命令名称、说明、别名和共享解析行为。"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CommandDefinition:
    """定义不依赖执行界面的命令元数据。"""

    name: str
    description: str
    aliases: tuple[str, ...] = ()


class CommandCatalog:
    """为交互聊天和一次性 CLI 提供一致的注册、查找与帮助逻辑。"""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        self._aliases: dict[str, str] = {}

    @property
    def commands(self) -> tuple[CommandDefinition, ...]:
        """返回按注册顺序排列的规范命令。"""
        return tuple(self._commands.values())

    def register(self, command: CommandDefinition) -> CommandDefinition:
        """校验命令名和别名，并拒绝冲突注册。"""
        names = tuple(
            self.normalize_name(name)
            for name in (command.name, *command.aliases)
        )
        if len(set(names)) != len(names):
            raise ValueError("A command and its aliases must have unique names")
        if any(name in self._aliases for name in names):
            raise ValueError("Command names and aliases must be unique")
        if not command.description.strip():
            raise ValueError("command description must not be empty")

        registered = CommandDefinition(
            name=names[0],
            description=command.description,
            aliases=names[1:],
        )
        self._commands[registered.name] = registered
        for name in names:
            self._aliases[name] = registered.name
        return registered

    def get(self, name: str) -> CommandDefinition | None:
        """按规范名称或别名查找命令。"""
        try:
            normalized = self.normalize_name(name)
        except ValueError:
            return None
        command_name = self._aliases.get(normalized)
        return None if command_name is None else self._commands[command_name]

    def canonical_name(self, name: str) -> str | None:
        """返回命令或别名对应的规范名称。"""
        try:
            normalized = self.normalize_name(name)
        except ValueError:
            return None
        return self._aliases.get(normalized)

    def completion_items(self) -> tuple[tuple[str, str], ...]:
        """返回所有规范名称和别名的输入补全项。"""
        return tuple(
            (f"/{name}", self._commands[canonical].description)
            for name, canonical in self._aliases.items()
        )

    def help_text(self) -> str:
        """返回按规范命令分组的帮助文本。"""
        lines = ["Available commands:"]
        for command in self._commands.values():
            names = (command.name, *command.aliases)
            rendered_names = ", ".join(f"/{name}" for name in names)
            lines.append(f"  {rendered_names} — {command.description}")
        return "\n".join(lines)

    @staticmethod
    def normalize_name(name: str) -> str:
        """规范化命令名并校验 kebab-case 格式。"""
        normalized = name.strip().lstrip("/").casefold()
        if re.fullmatch(r"[a-z][a-z0-9-]*", normalized) is None:
            raise ValueError(f"Invalid slash-command name: {name!r}")
        return normalized


__all__ = ["CommandCatalog", "CommandDefinition"]
