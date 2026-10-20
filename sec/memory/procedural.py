"""程序性记忆（「怎么做」）来源：本地 USER.md 与已发现的 Skills。

统一为 `ProceduralSource` 契约（`load(query) -> tuple[ProceduralBlock, ...]`），
由 `MemoryRetriever` 逐源加载后注入上下文。USER.md 是用户偏好文件，缺失视为空；
读取失败作为检索失败上报；SkillSource 按 query 关键词匹配
已发现技能的名称/描述/适用场景，返回相关技能正文。

Skills 在此仅作为可检索的过程性记忆被引用，不改变 `capabilities` 既有的
加载/授权路径。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sec._types.memory import ProceduralBlock, ProceduralSource
from sec.exceptions import MemoryStoreError
from sec.resources.markdown import CliResource


class UserProfile:
    """从本地文件读取用户偏好，作为程序性记忆来源之一。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None

    async def load(self, query: str = "") -> tuple[ProceduralBlock, ...]:
        """返回 USER.md 内容块；未配置、缺失、读取失败或为空返回 ()。"""
        if self._path is None:
            return ()
        try:
            content = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeError) as exc:
            raise MemoryStoreError(
                f"could not read user memory profile {self._path}: {exc}"
            ) from exc
        if not content:
            return ()
        return (ProceduralBlock("User preferences", content, "user_rule"),)


class SkillSource:
    """把已发现的 Skill 作为可检索的过程性记忆来源。"""

    def __init__(
        self,
        skills_provider: Callable[[], tuple[CliResource, ...]],
        *,
        top_k: int = 5,
    ) -> None:
        if not callable(skills_provider):
            raise TypeError("skills_provider must be callable")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._skills_provider = skills_provider
        self._top_k = top_k

    async def load(self, query: str) -> tuple[ProceduralBlock, ...]:
        """按 query 关键词匹配技能，返回相关技能正文；空 query 返回 ()。

        匹配复用 `capabilities_search` 的口径：对 `name + description +
        when_to_use` 做全词包含匹配。
        """
        terms = [term for term in query.casefold().split() if term]
        if not terms:
            return ()
        try:
            skills = self._skills_provider()
        except Exception as exc:
            raise MemoryStoreError(
                f"could not load Skill resources: {type(exc).__name__}: {exc}"
            ) from exc

        blocks: list[ProceduralBlock] = []
        for skill in skills:
            haystack = f"{skill.name} {skill.description} {skill.when_to_use}".casefold()
            if all(term in haystack for term in terms):
                blocks.append(ProceduralBlock(skill.name, skill.content, "skill"))
                if len(blocks) >= self._top_k:
                    break
        return tuple(blocks)
