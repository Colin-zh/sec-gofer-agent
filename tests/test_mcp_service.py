"""Local fixture service used by MCP transport integration tests."""

from __future__ import annotations

import sys

from mcp import types
from mcp.server.fastmcp import Context, FastMCP


def serve() -> None:
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    server = FastMCP("sec test fixture", host="127.0.0.1", port=port, log_level="ERROR")

    @server.tool()
    async def echo(value: str, ctx: Context) -> dict[str, str]:
        if value == "__notify__":
            await ctx.session.send_tool_list_changed()
        return {"echo": value}

    @server.tool()
    def remote_error() -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="remote refused")],
            isError=True,
            structuredContent={"reason": "fixture"},
            _meta={"trace": "fixture-trace"},
        )

    @server.tool()
    def image() -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.ImageContent(type="image", data="AA==", mimeType="image/png")]
        )

    @server.resource("fixture://greeting")
    def greeting() -> str:
        return "hello resource"

    @server.prompt()
    def welcome(name: str) -> str:
        return f"Hello {name}"

    server.run(transport="streamable-http" if sys.argv[1] == "--http" else "stdio")


if __name__ == "__main__":
    serve()
