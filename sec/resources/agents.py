"""Strict, atomic discovery of TOML agent cards."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import tomllib

from sec.agent.card import AgentCapabilities, AgentCard, AgentDelegation, AgentLimits
from sec.resources.markdown import MAX_RESOURCE_BYTES, ResourceIssue

_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_CARD_KEYS = {
    "schema_version", "id", "name", "description", "when_to_use",
    "instructions", "instructions_file", "capabilities", "limits", "delegation",
}


def _read(path: Path, root: Path) -> tuple[str, Path]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("agent file symlink resolves outside its agent directory")
    with resolved.open("rb") as stream:
        source = stream.read(MAX_RESOURCE_BYTES + 1)
    if len(source) > MAX_RESOURCE_BYTES:
        raise ValueError(f"agent file exceeds {MAX_RESOURCE_BYTES} bytes")
    return source.decode("utf-8"), resolved


def _keys(value: object, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"unknown {label} keys: {', '.join(sorted(unknown))}")
    return value


def _text(data: dict, key: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value.strip()


def _strings(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{key} must not contain duplicates")
    return tuple(value)


def _integer(data: dict, key: str, minimum: int, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return value


def _load_card(path: Path, root: Path) -> AgentCard:
    source, resolved = _read(path, root)
    data = _keys(tomllib.loads(source), _CARD_KEYS, "agent card")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    identifier = _text(data, "id")
    if not _ID.fullmatch(identifier) or identifier != path.parent.name:
        raise ValueError("id must match the lowercase agent directory name")
    if ("instructions" in data) == ("instructions_file" in data):
        raise ValueError("define exactly one of instructions or instructions_file")
    if "instructions_file" in data:
        relative = Path(_text(data, "instructions_file"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("instructions_file must be a relative path without traversal")
        instructions, _ = _read(path.parent / relative, path.parent)
        if not instructions.strip():
            raise ValueError("instructions_file must not be empty")
        instructions = instructions.strip()
    else:
        instructions = _text(data, "instructions")
    caps = _keys(data.get("capabilities", {}), {
        "tools", "skills", "mcp_servers", "extensions",
    }, "capabilities")
    capabilities = AgentCapabilities(**{
        key: _strings(caps, key)
        for key in ("tools", "skills", "mcp_servers", "extensions")
    })
    limits_data = _keys(data.get("limits", {}), {
        "max_model_calls", "max_tool_calls",
    }, "limits")
    limits = AgentLimits(
        _integer(limits_data, "max_model_calls", 1, 8),
        _integer(limits_data, "max_tool_calls", 1, 32),
    )
    delegation_data = _keys(data.get("delegation", {}), {
        "allowed_agents", "max_depth",
    }, "delegation")
    delegation = AgentDelegation(
        _strings(delegation_data, "allowed_agents"),
        _integer(delegation_data, "max_depth", 0, 0),
    )
    values = dict(
        schema_version=1, id=identifier, name=_text(data, "name"),
        description=_text(data, "description"), when_to_use=_text(data, "when_to_use"),
        instructions=instructions, capabilities=capabilities,
        limits=limits, delegation=delegation,
    )
    canonical = {
        **values, "capabilities": asdict(capabilities),
        "limits": asdict(limits), "delegation": asdict(delegation),
    }
    digest = sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()
    values.pop("capabilities")
    values.pop("limits")
    values.pop("delegation")
    return AgentCard(
        **values, **asdict(capabilities), **asdict(limits), **asdict(delegation),
        digest=digest, path=resolved,
    )


class AgentCatalog:
    """Workspace cards override user cards; invalid reloads retain all prior cards."""

    def __init__(self, workspace_root: Path, user_root: Path | None = None) -> None:
        self._roots = (
            Path.home() / ".sec" if user_root is None else Path(user_root),
            Path(workspace_root) / ".sec",
        )
        self._cards: tuple[AgentCard, ...] = ()
        self._issues: tuple[ResourceIssue, ...] = ()

    @property
    def cards(self) -> tuple[AgentCard, ...]:
        return self._cards

    @property
    def issues(self) -> tuple[ResourceIssue, ...]:
        return self._issues

    def get(self, identifier: str) -> AgentCard | None:
        return next((card for card in self.cards if card.id == identifier), None)

    def reload(self) -> tuple[AgentCard, ...]:
        cards: dict[str, AgentCard] = {}
        issues: list[ResourceIssue] = []
        for root in self._roots:
            directory = root / "agents"
            try:
                if not directory.exists():
                    continue
                if not directory.is_dir():
                    raise ValueError("agents path must be a directory")
                paths = sorted(directory.glob("*/card.toml"))
                for path in paths:
                    try:
                        card = _load_card(path, directory)
                    except (OSError, UnicodeError, ValueError) as exc:
                        issues.append(ResourceIssue(path, str(exc)))
                        continue
                    previous = cards.get(card.id)
                    if previous is not None:
                        issues.append(ResourceIssue(
                            previous.path,
                            f"overridden by workspace agent card {path}", "notice",
                        ))
                    cards[card.id] = card
            except (OSError, ValueError) as exc:
                issues.append(ResourceIssue(directory, str(exc)))
        self._issues = tuple(issues)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            raise ValueError("invalid agent cards: " + "; ".join(
                f"{issue.path}: {issue.message}" for issue in errors
            ))
        self._cards = tuple(cards[key] for key in sorted(cards))
        return self.cards
