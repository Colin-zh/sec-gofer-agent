"""按名称管理 sec 工具绑定并提供模型所需的定义与 schema。

注册表以唯一名称保存权威 Tool 对象，只负责登记、查找和提供 schema；
工具校验、授权与执行由 Runtime 的统一派发路径负责。
"""

from __future__ import annotations

from typing import Any

from sec.tools.tool import Tool, ToolDefinition


class ToolRegistry:
    """按唯一名称保存并查找权威运行时 Tool 绑定。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, registered: Tool) -> None:
        """登记唯一名称的工具绑定；名称重复时抛出 ValueError。"""
        name = registered.definition.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = registered

    def get(self, name: str) -> Tool | None:
        """按名称查找工具，未登记时返回 None。"""
        return self._tools.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """返回注册顺序对应的运行时工具绑定。"""
        return tuple(self._tools.values())

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回注册顺序对应的模型可见工具定义。"""
        return tuple(tool.definition for tool in self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """生成 Provider 可使用的工具 schema 字典列表。"""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": dict(definition.input_schema),
            }
            for definition in self.definitions()
        ]
