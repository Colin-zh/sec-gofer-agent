"""提供 CLI 使用的 plan、normal 和 auto 工具权限模式。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from sec._types.policy import PolicyContext, PolicyDecision

PermissionMode = Literal["plan", "normal", "auto"]
BUILTIN_WORKSPACE_SOURCE = "sec.builtin.workspace"


class PermissionModePolicy:
    """按权限模式、工具风险和工作区边界决定 allow、ask 或 deny。"""

    def __init__(
        self,
        mode: PermissionMode = "normal",
        *,
        workspace_root: str | Path,
    ) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self.set_mode(mode)

    @property
    def mode_name(self) -> PermissionMode:
        """返回当前权限模式名，供 PolicyContext 和 UI 使用。"""
        return self._mode

    @property
    def workspace_root(self) -> Path:
        """返回解析后的策略工作区根目录。"""
        return self._workspace_root

    def set_mode(self, mode: PermissionMode) -> None:
        """切换到已知模式；未知值 fail closed。"""
        if mode not in ("plan", "normal", "auto"):
            raise ValueError("permission mode must be plan, normal, or auto")
        self._mode = mode

    def decide(self, context: PolicyContext) -> PolicyDecision:
        """根据风险和资源边界生成结构化策略决策。"""
        in_workspace = self._is_workspace_file_operation(context)
        if context.workspace_scoped and not in_workspace:
            return PolicyDecision.deny(
                "Workspace-scoped tools cannot access paths outside the workspace "
                "or protected files."
            )

        if self._mode == "plan":
            if in_workspace and context.risk_level == "read":
                return PolicyDecision.allow("Plan mode permits workspace reads.")
            return PolicyDecision.deny("Plan mode permits read-only workspace tools.")

        if self._mode == "normal":
            return PolicyDecision.ask(
                "Normal mode requires approval for every tool call."
            )

        if in_workspace and context.risk_level in ("read", "write"):
            return PolicyDecision.allow(
                "Auto mode permits workspace-scoped file operations."
            )
        if context.capability_type == "mcp" or context.risk_level in (
            "execute",
            "network",
        ):
            return PolicyDecision.ask(
                "Auto mode still requires approval for shell and remote capabilities."
            )
        return PolicyDecision.ask(
            "Auto mode only auto-approves built-in workspace file tools."
        )

    def _is_workspace_file_operation(self, context: PolicyContext) -> bool:
        if (
            context.capability_type != "tool"
            or context.source != BUILTIN_WORKSPACE_SOURCE
            or not context.workspace_scoped
            or context.risk_level not in ("read", "write")
        ):
            return False

        for key in ("path", "directory", "cwd"):
            value = context.arguments.get(key)
            if isinstance(value, str) and not self._path_is_safe(value):
                return False
        return True

    def _path_is_safe(self, value: str) -> bool:
        path = Path(value).expanduser()
        resolved = (path if path.is_absolute() else self._workspace_root / path).resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError:
            return False
        return not _is_sensitive_path(resolved)


def _is_sensitive_path(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    if parts.intersection({".git", ".ssh", ".aws", ".gnupg"}):
        return True
    name = path.name.casefold()
    if name.startswith(".env") and name != ".env.example":
        return True
    return path.suffix.casefold() in {".pem", ".p12", ".pfx", ".key"}


__all__ = [
    "BUILTIN_WORKSPACE_SOURCE",
    "PermissionMode",
    "PermissionModePolicy",
]
