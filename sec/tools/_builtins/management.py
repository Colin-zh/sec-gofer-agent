"""内置管理工具的工厂与命名事实源。

``skills_load`` / ``capabilities_search`` / ``agents_delegate`` 与文件工具不同：
它们要读取当前 Trace 的能力快照与可变状态（已加载技能、已发现工具、委派回调），
因此必须按 Trace 现做，而不是进程级静态注册。本模块把它们的构造集中于此，并作为
管理工具名称与来源的唯一事实源，供 ``capabilities`` 构造、``client`` 保留名校验
与 ``policies`` 放行规则引用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sec.agent.card import AgentCard
from sec.exceptions import RecoverableToolError
from sec.resources.markdown import CliResource
from sec.tools.tool import Tool

SKILLS_LOAD_NAME = "skills_load"
CAPABILITIES_SEARCH_NAME = "capabilities_search"
AGENTS_DELEGATE_NAME = "agents_delegate"
MANAGEMENT_TOOL_SOURCE = "sec.capabilities"

# 管理工具占用的名称；扩展 / MCP / worker 工具不得与其重名。
RESERVED_TOOL_NAMES = frozenset(
    {SKILLS_LOAD_NAME, CAPABILITIES_SEARCH_NAME, AGENTS_DELEGATE_NAME}
)
# 只读的管理工具子集；权限模式对其始终放行（只读已授权目录）。
MANAGEMENT_READ_TOOL_NAMES = frozenset(
    {SKILLS_LOAD_NAME, CAPABILITIES_SEARCH_NAME}
)


class _ManagementBinding(Protocol):
    """capabilities.CapabilityBinding 中管理工具用到的可变 / 回调状态。"""

    loaded: set[str]
    discovered: set[str]
    delegate: Callable[[str, str, Any], Awaitable[str]] | None
    ceiling: tuple[AgentCard, ...]
    depth: int


def create_management_tools(
    binding: _ManagementBinding,
    *,
    skills: tuple[CliResource, ...],
    tools: tuple[Tool, ...],
    relevant: str,
    card: AgentCard,
) -> tuple[Tool, ...]:
    """按 Trace 构建三个管理工具，把该 Trace 的能力快照与可变状态闭包进去。"""
    async def load_skill(name: str) -> str:
        """Load an available skill's instructions before applying its workflow."""
        skill = next((item for item in skills if item.name == name), None)
        if skill is None:
            raise RecoverableToolError(f"Skill {name!r} is not available to this agent")
        if skill.activation == "manual" and name.casefold() not in relevant:
            raise RecoverableToolError(
                f"Manual Skill {name!r} requires an explicit user request naming it"
            )
        binding.loaded.add(name)
        return skill.content

    async def search_capabilities(query: str, offset: int = 0) -> dict[str, object]:
        """Find a page of 20 permitted capabilities; expose returned tools next request."""
        if offset < 0:
            raise RecoverableToolError("capability search offset must not be negative")
        terms = query.casefold().split()
        results: list[dict[str, str]] = []
        for item in tools:
            definition = item.definition
            haystack = f"{definition.name} {definition.description}".casefold()
            if all(term in haystack for term in terms):
                results.append({"kind": "tool", "name": definition.name,
                                "description": definition.description})
        for skill in skills:
            haystack = f"{skill.name} {skill.description} {skill.when_to_use}".casefold()
            if all(term in haystack for term in terms):
                results.append({"kind": "skill", "name": skill.name,
                                "description": skill.description})
        page = results[offset:offset + 20]
        binding.discovered.update(item["name"] for item in page if item["kind"] == "tool")
        return {
            "matches": page, "total": len(results),
            "next_offset": offset + 20 if offset + 20 < len(results) else None,
        }

    management = (
        Tool.from_callable(load_skill, name=SKILLS_LOAD_NAME, risk_level="read",
                           source=MANAGEMENT_TOOL_SOURCE, capability_type="skill"),
        Tool.from_callable(search_capabilities, name=CAPABILITIES_SEARCH_NAME,
                           risk_level="read", source=MANAGEMENT_TOOL_SOURCE),
    )
    if binding.delegate is not None and card.allowed_agents and binding.depth < card.max_depth:
        async def delegate_agent(agent_id: str, task: str) -> str:
            """Delegate an explicit task to one of the permitted specialist agents."""
            if agent_id not in card.allowed_agents:
                raise RecoverableToolError(f"Delegation to {agent_id!r} is not permitted")
            if any(agent_id not in ancestor.allowed_agents for ancestor in binding.ceiling):
                raise RecoverableToolError("Delegation exceeds an ancestor's agent scope")
            if binding.delegate is None:
                raise RuntimeError("delegation is unavailable")
            return await binding.delegate(agent_id, task, binding)

        management += (Tool.from_callable(delegate_agent, name=AGENTS_DELEGATE_NAME,
                                          risk_level="execute", source=MANAGEMENT_TOOL_SOURCE),)
    return management
