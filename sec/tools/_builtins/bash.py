"""定义需审批的工作区 Bash 执行工具。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path
from typing import Any

from sec.tools.tool import Tool, tool

MAX_TOOL_OUTPUT_BYTES = 64_000
MAX_BASH_COMMAND_LENGTH = 8_192
_BASH_TIMEOUT_SECONDS = 30


def create_bash_tool(workspace_root: Path) -> Tool:
    root = workspace_root.expanduser().resolve()

    @tool(
        risk_level="execute",
        source="sec.builtin.shell",
        workspace_scoped=False,
    )
    async def bash(command: str) -> dict[str, Any]:
        """Run an explicitly approved Bash command with a timeout and output cap."""
        if not command.strip() or len(command) > MAX_BASH_COMMAND_LENGTH:
            raise ValueError("bash command must contain 1 to 8192 characters")
        executable = shutil.which("bash")
        if executable is None:
            raise RuntimeError("Bash executable was not found on PATH")
        process = await asyncio.create_subprocess_exec(
            executable,
            "-lc",
            command,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        wait_task = asyncio.create_task(process.wait())
        stdout_task = asyncio.create_task(_read_limited(process.stdout))
        stderr_task = asyncio.create_task(_read_limited(process.stderr))
        timed_out = False
        try:
            tasks = {wait_task, stdout_task, stderr_task}
            _, pending = await asyncio.wait(
                tasks,
                timeout=_BASH_TIMEOUT_SECONDS,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                timed_out = True
                await _terminate_process(process)
                await asyncio.gather(*pending)
        except asyncio.CancelledError:
            await _terminate_process(process)
            await asyncio.gather(
                wait_task,
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            raise
        stdout, stdout_truncated = stdout_task.result()
        stderr, stderr_truncated = stderr_task.result()
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "timed_out": timed_out,
            "output_truncated": stdout_truncated or stderr_truncated,
        }

    return bash


async def _read_limited(
    reader: asyncio.StreamReader | None,
) -> tuple[bytes, bool]:
    if reader is None:
        return b"", False
    collected = bytearray()
    truncated = False
    while chunk := await reader.read(8_192):
        remaining = MAX_TOOL_OUTPUT_BYTES - len(collected)
        if remaining > 0:
            collected.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(collected), truncated


async def _terminate_process(
    process: asyncio.subprocess.Process,
) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()
