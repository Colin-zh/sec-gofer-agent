"""将原生 Trace 事件呈现为终端聊天输出。"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from sec import Trace, SessionEvent
from sec.apps.cli.terminal import TerminalUI
from sec.apps.cli.resources import ResourceSnapshot


def welcome_panel(
    *,
    home: str,
    model: str,
    session_id: str,
    resources: ResourceSnapshot | None = None,
    enabled_extensions: tuple[str, ...] = (),
    selected_agent: str | None = None,
    pending_agent_id: str | None = None,
    permission_mode: str = "normal",
) -> Panel:
    """呈现版本、当前上下文、加载资源和主要终端快捷键。"""
    try:
        app_version = version("sec-gofer-agent")
    except PackageNotFoundError:
        app_version = "development"
    content = Text()
    content.append(
        f"Sec v{app_version} — Your gofer. A second you, in a second.",
        style="bold blue",
    )
    content.append("\n\n")
    content.append("⌂  Working directory  ", style="cyan")
    content.append(TerminalUI.display_home(home), style="bold")
    content.append("\n")
    content.append("◈  Model              ", style="magenta")
    content.append(model or "not configured", style="bold")
    content.append("\n")
    content.append("✦  Agent              ", style="cyan")
    content.append(selected_agent or "default", style="bold")
    if pending_agent_id is not None:
        content.append(f" · {pending_agent_id} queued for next Trace", style="yellow")
    content.append("\n")
    content.append("#  Session            ", style="yellow")
    content.append(session_id, style="bold")
    content.append("\n")
    content.append("◉  Tool mode           ", style="green")
    content.append(permission_mode, style="bold")
    snapshot = resources or ResourceSnapshot((), (), ())
    content.append("\n")
    content.append("✦  Resources           ", style="cyan")
    content.append(
        f"{len(snapshot.skills)} Skills · {len(snapshot.prompts)} Prompts",
        style="bold",
    )
    if enabled_extensions:
        content.append("\n")
        content.append("◇  Extensions          ", style="magenta")
        content.append(", ".join(enabled_extensions), style="bold")
    if snapshot.issues:
        content.append("\n")
        content.append("!  Resource issues     ", style="yellow")
        content.append(str(len(snapshot.issues)), style="bold")
    content.append("\n\n")
    content.append(
        "Ctrl+C cancel Trace · Ctrl+D exit · Tab complete · /help commands",
        style="dim",
    )
    content.append("\n")
    content.append("Press Ctrl+O for full startup help and loaded resources.", style="dim")
    return Panel(content, border_style="cyan")


async def show_event(
    ui: TerminalUI,
    run: Trace,
    event: SessionEvent,
    *,
    trace_number: int,
) -> None:
    """呈现一个事件；遇到工具审批时通过输入界面收集明确决定。"""
    if event.type == "message_update":
        message_event = event.assistant_message_event
        if message_event.type == "text_delta":
            ui.append_stream_text(message_event.delta)
    elif event.type == "turn_end":
        ui.finish_turn()
        ui.add_status("llm", f"[Trace #{trace_number}] assistant turn complete")
    elif event.type == "tool_execution_start":
        ui.add_status("info", f"[tool] {event.tool_name} started")
    elif event.type == "tool_execution_end":
        result = _format_json(event.result)
        result = result if len(result) <= 240 else result[:237] + "..."
        kind = "error" if event.is_error else "info"
        ui.add_status(kind, f"[tool] {event.tool_name}: {result}")
    elif event.type == "tool_approval_requested":
        args = json.dumps(event.args, ensure_ascii=False, indent=2)
        details = (
            f"Policy: {event.reason}\n\n{args}"
            if event.reason
            else args
        )
        approved = await ui.confirm(
            f"Allow tool {event.tool_name}?",
            details,
        )
        if approved:
            accepted = run.approve_tool(event.tool_call_id)
        else:
            accepted = run.deny_tool(
                event.tool_call_id,
                "The user declined this tool call.",
            )
        if not accepted:
            ui.add_status(
                "info",
                f"[tool] approval for {event.tool_name} was no longer pending",
            )
    elif event.type == "tool_approval_resolved":
        ui.add_status(
            "info",
            f"[tool] {event.tool_name} approval {event.outcome}",
        )
    elif event.type == "extension_ui_requested":
        await _render_extension_ui_request(ui, run, event)
    elif event.type == "extension_ui_resolved":
        ui.add_status("info", f"[extension] UI request {event.outcome}")
    elif event.type == "agent_settled":
        ui.finish_turn()
        ui.add_status("info", f"[Trace #{trace_number}] status: {event.status}")


async def _render_extension_ui_request(
    ui: TerminalUI,
    run: Trace,
    event: SessionEvent,
) -> None:
    """渲染扩展 UI 请求并收集宿主回传的响应。"""
    request = event.request  # type: ignore[attr-defined]
    method = request["method"]
    request_id = event.request_id  # type: ignore[attr-defined]
    label = event.extension_name  # type: ignore[attr-defined]

    if method == "notify":
        ui.add_status("info", f"[extension] {request.get('message', '')}")
        return
    if method == "select":
        options = request.get("options", [])
        ui.add_status("prompt", f"{label} asks: {request.get('title', '')}")
        for index, option in enumerate(options, 1):
            ui.add_status("prompt", f"  {index}. {option}")
        answer = await ui.prompt("Choose a number")
        selected = _parse_selection(answer, options)
        if selected is None:
            run.respond_ui(
                request_id,
                {"type": "extension_ui_response", "id": request_id, "cancelled": True},
            )
        else:
            run.respond_ui(
                request_id,
                {"type": "extension_ui_response", "id": request_id, "value": selected},
            )
        return
    if method == "confirm":
        confirmed = await ui.confirm(
            f"{label} asks: {request.get('title', '')}",
            request.get("message", ""),
        )
        run.respond_ui(
            request_id,
            {"type": "extension_ui_response", "id": request_id, "confirmed": bool(confirmed)},
        )
        return
    answer = await ui.prompt(f"{label} asks: {request.get('title', '')}")
    if answer is None:
        run.respond_ui(
            request_id,
            {"type": "extension_ui_response", "id": request_id, "cancelled": True},
        )
    else:
        run.respond_ui(
            request_id,
            {"type": "extension_ui_response", "id": request_id, "value": answer},
        )


def _parse_selection(answer: str | None, options: list[str]) -> str | None:
    if answer is None:
        return None
    stripped = answer.strip()
    if stripped.isdigit():
        index = int(stripped)
        if 1 <= index <= len(options):
            return options[index - 1]
    return None


def show_noninteractive_event(
    output: Console,
    run: Trace,
    event: SessionEvent,
) -> None:
    """呈现管道模式事件，并默认拒绝无法交互确认的工具调用。"""
    if event.type == "message_update":
        message_event = event.assistant_message_event
        if message_event.type == "text_delta":
            output.print(message_event.delta, end="", markup=False)
    elif event.type == "turn_end":
        output.print()
    elif event.type == "tool_approval_requested":
        accepted = run.deny_tool(
            event.tool_call_id,
            "Tool approval requires an interactive terminal.",
        )
        if accepted:
            output.print(
                f"[tool] denied {event.tool_name}: approval requires an interactive terminal",
                markup=False,
            )
    elif event.type == "tool_execution_start":
        output.print(f"[tool] {event.tool_name} started", markup=False)
    elif event.type == "tool_execution_end":
        output.print(
            f"[tool] {event.tool_name}: {_format_json(event.result)}",
            markup=False,
        )
    elif event.type == "extension_ui_requested":
        if event.request.get("method") == "notify":
            output.print(f"[extension] {event.request.get('message', '')}", markup=False)
        else:
            run.respond_ui(
                event.request_id,
                {"type": "extension_ui_response", "id": event.request_id, "cancelled": True},
            )


def _format_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)
