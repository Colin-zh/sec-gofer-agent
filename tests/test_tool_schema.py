"""Schema-backed tools retain JSON Schema and typed executor result semantics."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sec._types.content import ToolUseContent
from sec.executors.local import LocalToolExecutor
from sec.tools.tool import Tool, ToolOutput, tool


class SchemaToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolved_schema_references_are_argument_errors_without_network(self) -> None:
        for reference in ("https://example.invalid/schema.json", "#/$defs/missing"):
            with self.subTest(reference=reference):
                wrapped = Tool.from_schema(
                    lambda arguments: arguments, name="references",
                    input_schema={"$ref": reference},
                )
                with patch("socket.socket.connect") as connect:
                    with self.assertRaisesRegex(TypeError, "cannot be resolved"):
                        await wrapped.invoke({})
                    connect.assert_not_called()

    async def test_full_schema_and_original_arguments(self) -> None:
        schema = {
            "$defs": {"tag": {"type": "string", "pattern": "^[a-z]+$", "minLength": 2}},
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}, "minItems": 1},
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
                "mode": {"enum": ["read", "write"]},
            },
            "required": ["tags", "count"],
            "additionalProperties": False,
            "allOf": [{"if": {"properties": {"mode": {"const": "write"}}, "required": ["mode"]},
                       "then": {"properties": {"count": {"minimum": 3}}}}],
        }
        seen = []

        async def invoke(arguments: dict) -> dict:
            seen.append(arguments)
            return arguments

        wrapped = Tool.from_schema(invoke, name="schema", input_schema=schema)
        self.assertIsNone(wrapped.signature)
        self.assertEqual(wrapped.definition.input_schema, schema)
        arguments = {"tags": ["yes"], "count": 2}
        self.assertIs(await wrapped.invoke(arguments), arguments)
        for invalid in (
            {"tags": ["X"], "count": 2},
            {"tags": [], "count": 2},
            {"tags": ["ok"], "count": True},
            {"tags": ["ok"], "count": 8},
            {"tags": ["ok"], "count": 2, "mode": "write"},
            {"tags": ["ok"], "count": 2, "extra": 1},
            {"count": 2},
        ):
            with self.assertRaises(TypeError):
                await wrapped.invoke(invalid)
        self.assertEqual(seen, [arguments])

    async def test_callable_api_preserved(self) -> None:
        @tool
        def combine(value: str, count: int = 2) -> str:
            return value * count

        self.assertIsNotNone(combine.signature)
        self.assertEqual(await combine.invoke({"value": "x"}), "xx")
        with self.assertRaises(TypeError):
            await combine.invoke({"value": 3})

    async def test_sync_schema_binding_and_schema_copy(self) -> None:
        schema = {"type": "object", "additionalProperties": False}
        wrapped = Tool.from_schema(lambda arguments: arguments, name="schema", input_schema=schema)
        schema["additionalProperties"] = True
        self.assertEqual(await wrapped.invoke({}), {})
        with self.assertRaises(TypeError):
            await wrapped.invoke({"unexpected": True})

    async def test_typed_output_preserves_error_and_metadata(self) -> None:
        async def invoke(arguments: dict) -> ToolOutput:
            return ToolOutput("refused", {"reason": "policy"}, {"trace": "abc"}, True)

        wrapped = Tool.from_schema(invoke, name="remote", input_schema={"type": "object"})
        result = await LocalToolExecutor().execute(wrapped, ToolUseContent("call-1", "remote", {}))
        self.assertEqual(result.tool_call_id, "call-1")
        self.assertEqual(result.text, "refused")
        self.assertTrue(result.is_error)
        self.assertEqual(result.metadata, {"trace": "abc", "structured_content": {"reason": "policy"}})

    async def test_structured_only_output_is_model_visible(self) -> None:
        wrapped = Tool.from_schema(
            lambda arguments: ToolOutput(structured_content={"answer": 42}),
            name="structured", input_schema={},
        )
        result = await LocalToolExecutor().execute(wrapped, ToolUseContent("id", "structured", {}))
        self.assertEqual(result.text, '{"answer": 42}')
        with self.assertRaises(TypeError):
            ToolOutput(text=["unsupported"])  # type: ignore[arg-type]

    async def test_callable_can_declare_typed_output(self) -> None:
        @tool
        def invoke() -> ToolOutput:
            return ToolOutput(text="typed", metadata={"trace": "callable"})

        result = await LocalToolExecutor().execute(invoke, ToolUseContent("id", "invoke", {}))
        self.assertEqual(result.text, "typed")
        self.assertEqual(result.metadata, {"trace": "callable"})
