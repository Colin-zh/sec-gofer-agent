"""发现工作区和用户目录中的 Markdown Skills 与 Prompts 资源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

MAX_RESOURCE_BYTES = 256 * 1024
_RESOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


@dataclass(frozen=True)
class CliResource:
    """保存已解析资源的类型、名称、说明、正文和来源路径。"""

    kind: Literal["skill", "prompt"]
    name: str
    description: str
    content: str
    path: Path


@dataclass(frozen=True)
class ResourceIssue:
    """描述单个资源文件未能加载或被覆盖的原因。"""

    path: Path
    message: str


@dataclass(frozen=True)
class ResourceSnapshot:
    """提供一次资源发现的 Skills、Prompts 与问题快照。"""

    skills: tuple[CliResource, ...]
    prompts: tuple[CliResource, ...]
    issues: tuple[ResourceIssue, ...]


class CliResourceManager:
    """加载 Markdown 提示资源，不执行资源文件内容。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        user_root: Path | None = None,
    ) -> None:
        self._roots = (
            ("user", Path.home() / ".sec" if user_root is None else user_root),
            ("workspace", workspace_root / ".sec"),
        )
        self._snapshot = ResourceSnapshot((), (), ())

    @property
    def snapshot(self) -> ResourceSnapshot:
        return self._snapshot

    def reload(self) -> ResourceSnapshot:
        """重新加载 Skills 与 Prompts，并显式记录格式错误。"""
        skills, skill_issues = self._discover("skill")
        prompts, prompt_issues = self._discover("prompt")
        self._snapshot = ResourceSnapshot(
            skills,
            prompts,
            (*skill_issues, *prompt_issues),
        )
        return self._snapshot

    def get(
        self,
        kind: Literal["skill", "prompt"],
        name: str,
    ) -> CliResource | None:
        resources = self._snapshot.skills if kind == "skill" else self._snapshot.prompts
        return next((item for item in resources if item.name == name.casefold()), None)

    def _discover(
        self,
        kind: Literal["skill", "prompt"],
    ) -> tuple[tuple[CliResource, ...], tuple[ResourceIssue, ...]]:
        resources: dict[str, CliResource] = {}
        scopes: dict[str, str] = {}
        issues: list[ResourceIssue] = []
        for scope, root in self._roots:
            directory = root / ("skills" if kind == "skill" else "prompts")
            try:
                paths = _resource_paths(directory, kind)
            except (OSError, ValueError) as exc:
                issues.append(ResourceIssue(directory, str(exc)))
                continue
            for path in paths:
                try:
                    resource = _load_resource(path, directory, kind)
                except (OSError, UnicodeError, ValueError) as exc:
                    issues.append(ResourceIssue(path, str(exc)))
                    continue
                previous = resources.get(resource.name)
                if previous is not None:
                    if scope == "workspace" and scopes[resource.name] == "user":
                        issues.append(
                            ResourceIssue(
                                previous.path,
                                f"overridden by workspace resource {path}",
                            )
                        )
                    elif scope == "workspace":
                        issues.append(
                            ResourceIssue(
                                path,
                                f"duplicate workspace resource name; keeping {previous.path}",
                            )
                        )
                    else:
                        issues.append(
                            ResourceIssue(
                                path,
                                f"duplicate user resource name; keeping {previous.path}",
                            )
                        )
                if previous is None or (
                    scope == "workspace" and scopes[resource.name] == "user"
                ):
                    resources[resource.name] = resource
                    scopes[resource.name] = scope
        return (
            tuple(sorted(resources.values(), key=lambda item: item.name)),
            tuple(issues),
        )


def _resource_paths(
    directory: Path,
    kind: Literal["skill", "prompt"],
) -> tuple[Path, ...]:
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError(f"resource path is not a directory: {directory}")
    candidates = (
        (*directory.glob("*/SKILL.md"), *directory.glob("*.md"))
        if kind == "skill"
        else tuple(directory.glob("*.md"))
    )
    return tuple(sorted((path for path in candidates if path.is_file())))


def _load_resource(
    path: Path,
    directory: Path,
    kind: Literal["skill", "prompt"],
) -> CliResource:
    resolved = path.resolve(strict=True)
    allowed_root = directory.resolve(strict=True)
    if not resolved.is_relative_to(allowed_root):
        raise ValueError("resource symlink resolves outside its resource directory")
    if resolved.stat().st_size > MAX_RESOURCE_BYTES:
        raise ValueError(f"resource exceeds {MAX_RESOURCE_BYTES} bytes")
    source = resolved.read_text(encoding="utf-8")
    metadata, content = _frontmatter(source)
    fallback_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name = metadata.get("name", fallback_name).strip().casefold()
    if _RESOURCE_NAME.fullmatch(name) is None:
        raise ValueError("resource name must contain lowercase letters, digits, and hyphens")
    description = metadata.get("description", "").strip()
    if kind == "skill" and not description:
        raise ValueError("Skill frontmatter must define a description")
    if not description:
        description = next(
            (line.strip().lstrip("#").strip() for line in content.splitlines() if line.strip()),
            "Custom prompt",
        )
    if not content.strip():
        raise ValueError("resource content must not be empty")
    return CliResource(kind, name, description, content.strip(), resolved)


def _frontmatter(source: str) -> tuple[dict[str, str], str]:
    lines = source.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, source
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("frontmatter is missing its closing '---'") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"invalid frontmatter field: {line!r}")
        metadata[key.strip().casefold()] = value.strip().strip("\"'")
    return metadata, "\n".join(lines[closing + 1 :])


__all__ = [
    "CliResource",
    "CliResourceManager",
    "ResourceIssue",
    "ResourceSnapshot",
]
