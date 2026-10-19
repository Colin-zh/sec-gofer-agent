"""定义 sec 工具 schema、运行时绑定、参数校验与底层调用。

本模块根据 Python 函数签名生成受支持的 JSON Schema，将模型可见的
ToolDefinition 与本地 callable 绑定，并在执行前校验参数。
同步函数在线程中调用；异步函数在当前事件循环中等待。
"""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, get_args, get_origin, get_type_hints

from sec._types.policy import PolicyCapabilityType, PolicyRiskLevel
from jsonschema import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

ToolRiskLevel = PolicyRiskLevel


@dataclass(frozen=True)
class ToolOutput:
    """Text and JSON results with explicit remote error semantics."""

    text: str = ""
    structured_content: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] | None = None
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("tool output text must be a string")
        if not isinstance(self.is_error, bool):
            raise TypeError("tool output is_error must be a boolean")
        for value in (self.structured_content, self.metadata):
            if value is not None and not isinstance(value, Mapping):
                raise TypeError("tool output structured content and metadata must be objects")


def _schema_for(annotation: Any) -> dict[str, Any]:
    schemas = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        type(None): {"type": "null"},
        dict: {"type": "object"},
        list: {"type": "array"},
    }
    if annotation is inspect.Signature.empty or annotation is Any or annotation is ToolOutput:
        return {}
    if annotation in schemas:
        return schemas[annotation]

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is list:
        item_schema = _schema_for(arguments[0]) if arguments else {}
        return {"type": "array", "items": item_schema}
    if origin is dict:
        return {"type": "object"}
    raise TypeError(f"unsupported tool parameter annotation: {annotation!r}")


def _validate_value(value: Any, schema: dict[str, Any], parameter_name: str) -> None:
    expected_type = schema.get("type")
    valid = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: type(item) is int,
        "number": lambda item: type(item) in (int, float),
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
    }.get(expected_type)
    if valid is not None and not valid(value):
        raise TypeError(
            f"tool argument {parameter_name!r} must have JSON type {expected_type}"
        )
    if expected_type == "array" and "items" in schema:
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{parameter_name}[{index}]")


@dataclass(frozen=True)
class ToolDefinition:
    """描述模型可见的工具名称、说明及输入输出 schema，不含可执行函数。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]


@dataclass(frozen=True)
class Tool:
    """将 Python callable 与其模型可见定义及参数签名绑定。"""

    function: Callable[..., Any]
    definition: ToolDefinition
    signature: inspect.Signature | None
    retry_safe: bool = False
    risk_level: ToolRiskLevel = "unknown"
    source: str = "local"
    workspace_scoped: bool = False
    capability_type: PolicyCapabilityType = "tool"

    def __post_init__(self) -> None:
        if self.risk_level not in ("read", "write", "execute", "network", "unknown"):
            raise ValueError("unsupported tool risk level")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("tool source must not be empty")
        if not isinstance(self.workspace_scoped, bool):
            raise TypeError("workspace_scoped must be a boolean")
        if self.capability_type not in ("tool", "mcp", "skill"):
            raise ValueError("unsupported tool capability type")

    @classmethod
    def from_callable(
        cls,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        retry_safe: bool = False,
        risk_level: ToolRiskLevel = "unknown",
        source: str = "local",
        workspace_scoped: bool = False,
        capability_type: PolicyCapabilityType = "tool",
    ) -> Tool:
        """从 Python 函数签名生成工具 schema，并可声明调用是否安全重试。"""
        signature = inspect.signature(function)
        type_hints = get_type_hints(function)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                raise TypeError("tool functions cannot use *args or **kwargs")
            annotation = type_hints.get(parameter.name, parameter.annotation)
            properties[parameter.name] = _schema_for(annotation)
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)

        tool_name = function.__name__ if name is None else name
        if not tool_name.strip():
            raise ValueError("tool name must not be empty")
        tool_description = description
        if tool_description is None:
            tool_description = inspect.getdoc(function) or ""
        return_annotation = type_hints.get("return", signature.return_annotation)
        definition = ToolDefinition(
            name=tool_name,
            description=tool_description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            output_schema=_schema_for(return_annotation),
        )
        return cls(
            function=function,
            definition=definition,
            signature=signature,
            retry_safe=retry_safe,
            risk_level=risk_level,
            source=source,
            workspace_scoped=workspace_scoped,
            capability_type=capability_type,
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        """检查参数是否符合函数签名及生成的 JSON 类型约束。"""
        if self.signature is None:
            validator_class = validator_for(self.definition.input_schema)
            try:
                validator_class(
                    self.definition.input_schema, registry=Registry()
                ).validate(arguments)
            except ValidationError as error:
                raise TypeError(f"tool arguments do not match JSON schema: {error.message}") from error
            except Unresolvable as error:
                raise TypeError(f"tool JSON schema reference cannot be resolved: {error.ref}") from error
            return
        self.signature.bind(**arguments)
        properties = self.definition.input_schema["properties"]
        for name, value in arguments.items():
            _validate_value(value, properties[name], name)

    @classmethod
    def from_schema(
        cls,
        function: Callable[[dict[str, Any]], Any],
        *,
        name: str,
        description: str = "",
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None = None,
        retry_safe: bool = False,
        risk_level: ToolRiskLevel = "unknown",
        source: str = "local",
        workspace_scoped: bool = False,
        capability_type: PolicyCapabilityType = "tool",
    ) -> Tool:
        """Bind a JSON-schema tool to a callable accepting one argument object."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool name must not be empty")
        schemas = [deepcopy(dict(input_schema)), deepcopy(dict(output_schema or {}))]
        for schema in schemas:
            validator_for(schema).check_schema(schema)
        return cls(
            function=function,
            definition=ToolDefinition(name, description, schemas[0], schemas[1]),
            signature=None,
            retry_safe=retry_safe,
            risk_level=risk_level,
            source=source,
            workspace_scoped=workspace_scoped,
            capability_type=capability_type,
        )

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """校验参数后调用工具，并等待同步或异步函数完成。"""
        self.validate_arguments(arguments)
        if self.signature is None:
            if inspect.iscoroutinefunction(self.function):
                return await self.function(arguments)
            result = await asyncio.to_thread(self.function, arguments)
            return await result if inspect.isawaitable(result) else result
        if inspect.iscoroutinefunction(self.function):
            return await self.function(**arguments)
        result = await asyncio.to_thread(self.function, **arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def tool(
    function: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    retry_safe: bool = False,
    risk_level: ToolRiskLevel = "unknown",
    source: str = "local",
    workspace_scoped: bool = False,
    capability_type: PolicyCapabilityType = "tool",
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """将 Python 函数封装为工具；重试安全必须由调用方显式声明。"""

    def decorate(target: Callable[..., Any]) -> Tool:
        return Tool.from_callable(
            target,
            name=name,
            description=description,
            retry_safe=retry_safe,
            risk_level=risk_level,
            source=source,
            workspace_scoped=workspace_scoped,
            capability_type=capability_type,
        )

    if function is not None:
        return decorate(function)
    return decorate
