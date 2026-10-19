"""发现工作区和用户目录中的 Markdown Skills 与 Prompts 资源。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
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
    activation: Literal["manual", "auto", "required"] = "auto"
    when_to_use: str = ""
    match: tuple[str, ...] = ()
    digest: str = ""

    def matches(self, text: str) -> bool:
        """Match explicit literal triggers without regex or glob semantics."""
        return any(pattern in text.casefold() for pattern in self.match)


@dataclass(frozen=True)
class ResourceIssue:
    """描述单个资源文件未能加载或被覆盖的原因。"""

    path: Path
    message: str
    severity: Literal["error", "notice"] = "error"


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
        activation_path: Path | None = None,
    ) -> None:
        self._roots = (
            ("user", Path.home() / ".sec" if user_root is None else Path(user_root)),
            ("workspace", Path(workspace_root) / ".sec"),
        )
        self._activation_path = activation_path
        self._activation = self._load_activation()
        self._available_snapshot = ResourceSnapshot((), (), ())

    @property
    def available_snapshot(self) -> ResourceSnapshot:
        """返回完整的已发现资源，包括已禁用的资源。"""
        return self._available_snapshot

    @property
    def snapshot(self) -> ResourceSnapshot:
        available = self._available_snapshot
        return ResourceSnapshot(
            tuple(item for item in available.skills if self._enabled(item.kind, item.name)),
            tuple(item for item in available.prompts if self._enabled(item.kind, item.name)),
            available.issues,
        )

    def is_enabled(self, kind: Literal["skill", "prompt"], name: str) -> bool:
        """查询已发现资源的启用状态；无配置时默认启用。"""
        resource = self._require_resource(kind, name)
        return self._enabled(kind, resource.name)

    def set_enabled(
        self, kind: Literal["skill", "prompt"], name: str, enabled: bool
    ) -> None:
        """更新状态；配置路径可选，持久化失败时保留原状态。"""
        resource = self._require_resource(kind, name)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        activation = {key: dict(values) for key, values in self._activation.items()}
        activation.setdefault(kind, {})[resource.name] = enabled
        self._save_activation(activation)
        self._activation = activation

    def _enabled(self, kind: str, name: str) -> bool:
        return self._activation.get(kind, {}).get(name, True)

    def _require_resource(self, kind: str, name: str) -> CliResource:
        self._validate_kind(kind)
        available = self._available_snapshot
        resources = available.skills if kind == "skill" else available.prompts
        resource = next((item for item in resources if item.name == name.casefold()), None)
        if resource is None:
            raise ValueError(f"unknown {kind} resource: {name}")
        return resource

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in ("skill", "prompt"):
            raise ValueError(f"invalid resource kind: {kind!r}")

    def _load_activation(self) -> dict[str, dict[str, bool]]:
        """读取 JSON：{"skill": {"name": false}, "prompt": {"name": true}}。"""
        if self._activation_path is None:
            return {}
        try:
            source = self._activation_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read resource activation config {self._activation_path}: {exc}") from exc
        try:
            activation = json.loads(source)
            if not isinstance(activation, dict):
                raise ValueError("expected an object")
            for kind, values in activation.items():
                self._validate_kind(kind)
                if not isinstance(values, dict):
                    raise ValueError(f"{kind} settings must be an object")
                for name, enabled in values.items():
                    if _RESOURCE_NAME.fullmatch(name) is None:
                        raise ValueError(f"invalid resource name: {name!r}")
                    if not isinstance(enabled, bool):
                        raise ValueError(f"{kind}/{name} state must be a boolean")
        except ValueError as exc:
            raise ValueError(f"invalid resource activation config {self._activation_path}: {exc}") from exc
        return activation

    def _save_activation(self, activation: dict[str, dict[str, bool]]) -> None:
        path = self._activation_path
        if path is None:
            return
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(activation, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
        except OSError as exc:
            raise OSError(f"cannot save resource activation config {path}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def reload(self, *, strict: bool = False) -> ResourceSnapshot:
        """重新加载 Skills 与 Prompts，并显式记录格式错误。"""
        skills, skill_issues = self._discover("skill")
        prompts, prompt_issues = self._discover("prompt")
        staged = ResourceSnapshot(
            skills,
            prompts,
            (*skill_issues, *prompt_issues),
        )
        if strict and any(issue.severity == "error" for issue in staged.issues):
            raise ValueError(
                "invalid resources: "
                + "; ".join(
                    f"{issue.path}: {issue.message}"
                    for issue in staged.issues if issue.severity == "error"
                )
            )
        self._available_snapshot = staged
        return self.snapshot

    def get(
        self,
        kind: Literal["skill", "prompt"],
        name: str,
    ) -> CliResource | None:
        self._validate_kind(kind)
        resources = self.snapshot.skills if kind == "skill" else self.snapshot.prompts
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
                                "notice",
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
    with resolved.open("rb") as stream:
        source_bytes = stream.read(MAX_RESOURCE_BYTES + 1)
    if len(source_bytes) > MAX_RESOURCE_BYTES:
        raise ValueError(f"resource exceeds {MAX_RESOURCE_BYTES} bytes")
    source = source_bytes.decode("utf-8")
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
    activation = metadata.get("activation", "auto")
    if activation not in ("manual", "auto", "required"):
        raise ValueError("activation must be manual, auto, or required")
    patterns: tuple[str, ...] = ()
    if "match" in metadata:
        values = tuple(value.strip().casefold() for value in metadata["match"].split(","))
        if not all(values):
            raise ValueError("match must contain nonempty comma-separated substring triggers")
        patterns = tuple(dict.fromkeys(values))
    if kind == "skill" and activation == "required" and not patterns:
        raise ValueError("required skills must define nonempty match triggers")
    content = content.strip()
    return CliResource(
        kind, name, description, content, resolved, activation,
        metadata.get("when_to_use", "").strip(), patterns,
        sha256(content.encode("utf-8")).hexdigest(),
    )


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
        key = key.strip().casefold()
        if key in metadata:
            raise ValueError(f"duplicate frontmatter field: {key}")
        metadata[key] = value.strip().strip("\"'")
    return metadata, "\n".join(lines[closing + 1 :])


ResourceManager = CliResourceManager
Resource = CliResource
Issue = ResourceIssue
Snapshot = ResourceSnapshot

__all__ = [
    "CliResource",
    "CliResourceManager",
    "ResourceIssue",
    "ResourceSnapshot",
    "ResourceManager",
    "Resource",
    "Issue",
    "Snapshot",
]
