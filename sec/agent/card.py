"""Immutable authored-agent configuration, independent of runtime state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path


@dataclass(frozen=True)
class AgentCapabilities:
    """Authored cards deny capabilities unless explicitly granted."""

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentLimits:
    max_model_calls: int = 8
    max_tool_calls: int = 32


@dataclass(frozen=True)
class AgentDelegation:
    allowed_agents: tuple[str, ...] = ()
    max_depth: int = 0


@dataclass(frozen=True)
class AgentCard:
    id: str
    name: str
    description: str
    instructions: str
    schema_version: int = 1
    when_to_use: str = ""
    digest: str = ""
    path: Path | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    max_model_calls: int = 8
    max_tool_calls: int = 32
    allowed_agents: tuple[str, ...] = ()
    max_depth: int = 0

    def __post_init__(self) -> None:
        if not self.digest:
            data = asdict(self)
            data.pop("path")
            data.pop("digest")
            object.__setattr__(self, "digest", sha256(
                json.dumps(data, sort_keys=True).encode("utf-8")
            ).hexdigest())

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(self.tools, self.skills, self.mcp_servers, self.extensions)

    @property
    def limits(self) -> AgentLimits:
        return AgentLimits(self.max_model_calls, self.max_tool_calls)

    @property
    def delegation(self) -> AgentDelegation:
        return AgentDelegation(self.allowed_agents, self.max_depth)


def __getattr__(name: str):
    if name == "AgentCatalog":
        from sec.resources.agents import AgentCatalog

        return AgentCatalog
    raise AttributeError(name)


__all__ = [
    "AgentCard", "AgentCapabilities", "AgentLimits", "AgentDelegation", "AgentCatalog",
]
