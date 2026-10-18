"""sec 命令行应用的公开启动入口与一次性命令扩展接口。"""

from sec.apps.cli.app import create_chat_commands, main
from sec.apps.cli.slash_commands import (
    SlashCommand,
    SlashCommandError,
    SlashCommandRegistry,
    command_registry,
)

__all__ = [
    "SlashCommand",
    "SlashCommandError",
    "SlashCommandRegistry",
    "command_registry",
    "create_chat_commands",
    "main",
]