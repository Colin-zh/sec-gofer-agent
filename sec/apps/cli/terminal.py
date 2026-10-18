"""实现保留对话输出、支持多行编辑的 Rich 终端界面。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from io import StringIO
import os
from pathlib import Path
import shutil
import sys
from typing import TextIO

from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.text import Text

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows uses the line-input fallback.
    termios = None
    tty = None

STATUS_COLORS = {
    "trace": "green",
    "follow_up": "cyan",
    "steer": "yellow",
    "should_stop": "red",
    "llm": "dim",
    "hook": "magenta",
    "info": "dim",
    "error": "red",
    "prompt": "blue",
}

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
KEY_SEQUENCES = {
    b"\x1b[1;5A": "ignore",
    b"\x1b[1;5B": "ignore",
    b"\x1b[13;3u": "newline",
    b"\x1b[13;3:1u": "newline",
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1bOH": "home",
    b"\x1bOF": "end",
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[3~": "delete",
}


class TerminalUI:
    """显示流式对话、命令补全和审批提示，并维护可编辑输入框。"""

    def __init__(
        self,
        *,
        home: str = "",
        model: str = "",
        session_id: str | None = None,
        output: TextIO | None = None,
        force_terminal: bool | None = None,
        columns: int | None = None,
    ) -> None:
        self.input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._input_text = ""
        self._cursor = 0
        self._command_items: tuple[tuple[str, str], ...] = ()
        self._home = home
        self._model = model
        self._session_id = session_id
        self._output = output or sys.stdout
        if force_terminal is None:
            force_terminal = bool(getattr(self._output, "isatty", lambda: False)())
        self._force_terminal = force_terminal
        self._columns = columns
        self.console = Console(
            file=self._output,
            force_terminal=force_terminal,
            color_system="standard" if force_terminal else None,
            highlight=False,
        )
        self._stop_requested = False
        self._running = False
        self._stop_event = asyncio.Event()
        self._working = False
        self._spinner_index = 0
        self._frame_visible = False
        self._frame_line_count = 0
        self._frame_cursor_row = 0
        self._frame_cursor_column = 0
        self._stream_cursor_column = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._input_fd: int | None = None
        self._old_terminal_state: list[object] | None = None
        self._pending_input = bytearray()
        self._pending_stream: list[str] = []
        self._stream_line_open = False
        self._stream_flush_handle: asyncio.TimerHandle | None = None
        self._spinner_handle: asyncio.TimerHandle | None = None
        self._escape_handle: asyncio.TimerHandle | None = None
        self._confirmation_future: asyncio.Future[bool] | None = None

    async def run(self) -> None:
        if os.name == "nt" or termios is None or tty is None:
            await self._run_line_input()
            return

        self._loop = asyncio.get_running_loop()
        self._input_fd = sys.stdin.fileno()
        self._old_terminal_state = termios.tcgetattr(self._input_fd)
        try:
            tty.setraw(self._input_fd)
            output_state = termios.tcgetattr(self._input_fd)
            output_state[1] |= termios.OPOST
            if hasattr(termios, "ONLCR"):
                output_state[1] |= termios.ONLCR
            termios.tcsetattr(
                self._input_fd,
                termios.TCSADRAIN,
                output_state,
            )
            self._loop.add_reader(self._input_fd, self._read_input)
            self._running = True
            self._write("\x1b[?25l")
            self._draw_frame()
            if self._working:
                self._schedule_spinner()
            await self._stop_event.wait()
        finally:
            self._erase_frame()
            self._running = False
            if self._input_fd is not None:
                self._loop.remove_reader(self._input_fd)
                if self._old_terminal_state is not None:
                    termios.tcsetattr(
                        self._input_fd,
                        termios.TCSADRAIN,
                        self._old_terminal_state,
                    )
            self._cancel_timer("_spinner_handle")
            self._cancel_timer("_stream_flush_handle")
            self._cancel_timer("_escape_handle")
            self._write("\x1b[?25h")

    async def _run_line_input(self) -> None:
        """Keep non-POSIX terminals usable, with standard line editing."""
        while not self._stop_requested:
            self.console.print("you > ", end="", style="bold blue")
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                self.input_queue.put_nowait("/exit")
                await self._stop_event.wait()
                return
            line = line.rstrip("\r\n")
            self.input_queue.put_nowait(line)
            if line.strip().casefold() in {"/exit", "/quit", "/q"}:
                return

    def stop(self) -> None:
        if self._stop_requested:
            return
        self._stop_requested = True
        if self._confirmation_future is not None:
            if not self._confirmation_future.done():
                self._confirmation_future.set_result(False)
        self._flush_pending_stream()
        self._erase_frame()
        self._write("\n")
        self._stop_event.set()

    def set_working(self, working: bool) -> None:
        self._working = working
        if working and self._loop is not None and self._spinner_handle is None:
            self._schedule_spinner()
        elif not working:
            self._cancel_timer("_spinner_handle")
        self._draw_frame()

    def add_user_message(self, text: str) -> None:
        self._print_text(f"you > {text}", "prompt")

    def set_command_completions(
        self, items: Iterable[tuple[str, str]]
    ) -> None:
        self._command_items = tuple(items)
        self._draw_frame()

    def set_session_id(self, session_id: str) -> None:
        """更新终端元数据中显示的当前 Session ID。"""
        self._session_id = session_id
        self._draw_frame()

    def add_status(self, kind: str, text: str) -> None:
        self._print_text(text, kind)

    async def confirm(self, title: str, message: str) -> bool:
        if self._confirmation_future is not None:
            raise RuntimeError("a terminal confirmation is already pending")
        future = asyncio.get_running_loop().create_future()
        self._confirmation_future = future
        self.add_status("prompt", f"{title}: {message} [y/N]")
        try:
            return await future
        finally:
            if self._confirmation_future is future:
                self._confirmation_future = None

    def resolve_confirmation(self, answer: str) -> bool:
        future = self._confirmation_future
        if future is None or future.done():
            return False
        normalized = answer.strip().lower()
        if normalized not in {"y", "yes", "n", "no"}:
            if normalized.startswith("/"):
                future.set_result(False)
                return False
            self.add_status("prompt", "Please answer y or n.")
            return True
        future.set_result(normalized in {"y", "yes"})
        return True

    def add_rich(self, renderable: RenderableType) -> None:
        self._flush_pending_stream()
        self._end_stream_line()
        self._erase_frame()
        self.console.print(renderable, justify="left")
        self._draw_frame()

    def append_stream_text(self, text: str) -> None:
        if not text:
            return
        self._pending_stream.append(text)
        if self._loop is None:
            self._flush_pending_stream()
            return
        if self._stream_flush_handle is None:
            self._stream_flush_handle = self._loop.call_later(
                0.04, self._flush_pending_stream
            )

    def reset_stream_text(self) -> None:
        self._flush_pending_stream()
        self._end_stream_line()

    def finish_turn(self) -> None:
        self._flush_pending_stream()
        self._end_stream_line()

    def _end_stream_line(self) -> None:
        if self._stream_line_open:
            self._erase_frame()
            self._write("\n")
            self._stream_line_open = False
            self._draw_frame()
        self._stream_cursor_column = 0

    def _print_text(self, text: str, kind: str) -> None:
        self._flush_pending_stream()
        self._end_stream_line()
        self._erase_frame()
        self.console.print(
            Text(text, style=STATUS_COLORS.get(kind, "white")),
            justify="left",
            markup=False,
        )
        self._draw_frame()

    def _flush_pending_stream(self) -> None:
        self._cancel_timer("_stream_flush_handle")
        if not self._pending_stream:
            return
        text = "".join(self._pending_stream)
        self._pending_stream.clear()
        self._erase_frame()
        self.console.print(
            Text(text, style="white"),
            end="",
            soft_wrap=True,
            markup=False,
        )
        columns = self._columns or shutil.get_terminal_size((80, 24)).columns
        for char in text:
            if char in "\r\n":
                self._stream_cursor_column = 0
            else:
                self._stream_cursor_column += cell_len(char)
                if self._stream_cursor_column >= columns:
                    self._stream_cursor_column %= columns
        self._stream_line_open = True
        self._draw_frame()

    def _term_width(self) -> int:
        columns = self._columns or shutil.get_terminal_size((80, 24)).columns
        return max(20, columns - 1)

    @staticmethod
    def _fit_row(parts: Iterable[tuple[str, str]], width: int) -> Text:
        row = Text()
        for value, style in parts:
            row.append(value, style=style)
        visible_width = cell_len(row.plain)
        if visible_width > width:
            row.truncate(width, overflow="ellipsis")
        else:
            row.append(" " * (width - visible_width))
        return row

    def _input_rows(self, width: int) -> tuple[list[Text], int, int]:
        interior_width = width - 2
        prompt = " you > "
        continuation = "       "
        rows: list[Text] = []
        row = Text(prompt, style="bold blue")
        current_width = cell_len(prompt)
        caret_row = 0
        caret_column = 1 + current_width

        def finish_row() -> None:
            nonlocal row
            row.truncate(interior_width)
            rows.append(row)

        for index, char in enumerate(self._input_text):
            if index == self._cursor:
                caret_row = len(rows)
                caret_column = 1 + current_width
            if char == "\n":
                finish_row()
                row = Text(continuation, style="bold blue")
                current_width = cell_len(continuation)
                continue

            char_width = cell_len(char)
            if current_width + char_width > interior_width:
                finish_row()
                row = Text(continuation, style="bold blue")
                current_width = cell_len(continuation)
            row.append(char, style="white")
            current_width += char_width

        if self._cursor == len(self._input_text):
            caret_row = len(rows)
            caret_column = 1 + current_width
        finish_row()
        return rows, caret_row, min(caret_column, width - 2)

    def _metadata(self, width: int) -> Text:
        fields = [
            ("⌂", self.display_home(self._home), "cyan"),
            ("◈", self._model, "magenta"),
            ("#", self._session_id or "", "yellow"),
        ]
        fields = [(icon, value, style) for icon, value, style in fields if value]
        if not fields:
            return Text(" " * width)

        values = [value for _, value, _ in fields]
        separator_width = 5
        while (
            sum(cell_len(value) + 2 for value in values)
            + separator_width * (len(values) - 1)
            > width
            and values[0]
        ):
            home_value = values[0]
            home_width = cell_len(home_value)
            if home_width <= 2:
                break
            values[0] = "…" + home_value[-(home_width - 2) :]

        row = Text()
        for index, ((icon, _, style), value) in enumerate(zip(fields, values, strict=True)):
            if index:
                row.append("  ·  ", style="dim")
            row.append(f"{icon} ", style=style)
            row.append(value, style="bold " + style)
        if cell_len(row.plain) > width:
            row.truncate(width, overflow="ellipsis")
        else:
            row.pad_left(width - cell_len(row.plain))
        return row

    @staticmethod
    def display_home(home: str) -> str:
        if not home:
            return ""
        return str(Path(home).expanduser().resolve())

    def _build_frame(self) -> tuple[list[Text], int, int]:
        width = self._term_width()
        label = (
            f" {SPINNER_FRAMES[self._spinner_index]} Working "
            if self._working
            else ""
        )
        label_style = "magenta" if self._working else "cyan"
        top = Text()
        top.append("╭─", style="cyan")
        top.append(label, style=label_style)
        top.append("─" * max(0, width - 3 - cell_len(label)), style="cyan")
        top.append("╮", style="cyan")
        input_rows, caret_row, caret_column = self._input_rows(width)
        frame = [top]
        for input_row in input_rows:
            frame.append(
                self._fit_row(
                    (
                        ("│", "cyan"),
                        (input_row.plain, ""),
                        ("│", "cyan"),
                    ),
                    width,
                )
            )
            frame[-1].stylize("bold blue", 1, min(width, 8))
        frame.append(
            self._fit_row(
                (("╰", "cyan"), ("─" * (width - 2), "cyan"), ("╯", "cyan")),
                width,
            )
        )

        matches = self._command_suggestions()
        if matches:
            suggestion_row = ""
            for command, _ in matches:
                candidate = (
                    f"{suggestion_row}  {command}"
                    if suggestion_row
                    else f"  {command}"
                )
                if cell_len(candidate) > width and suggestion_row:
                    frame.append(
                        self._fit_row(((suggestion_row, "dim cyan"),), width)
                    )
                    suggestion_row = f"  {command}"
                else:
                    suggestion_row = candidate
            if suggestion_row:
                frame.append(
                    self._fit_row(((suggestion_row, "dim cyan"),), width)
                )
        frame.append(
            self._fit_row(
                (
                    (
                        "Enter: send · Alt+Enter: newline · Alt+S: steer · Alt+F: follow-up",
                        "dim",
                    ),
                ),
                width,
            )
        )
        frame.append(
            self._fit_row(
                (
                    (
                        "Tab: complete · Ctrl+C: stop · Ctrl+D: exit",
                        "dim",
                    ),
                ),
                width,
            )
        )
        frame.append(self._metadata(width))
        return frame, caret_row + 1, caret_column

    def _command_suggestions(self) -> list[tuple[str, str]]:
        prefix = self._input_text[: self._cursor]
        if not prefix.startswith("/") or any(char.isspace() for char in prefix):
            return []
        normalized = prefix.casefold()
        return [
            item
            for item in self._command_items
            if item[0].casefold().startswith(normalized)
        ]

    def _ansi_line(self, text: Text, width: int) -> str:
        buffer = StringIO()
        renderer = Console(
            file=buffer,
            width=width,
            force_terminal=self._force_terminal,
            color_system="standard" if self._force_terminal else None,
            highlight=False,
        )
        renderer.print(text, end="", soft_wrap=True)
        return buffer.getvalue()

    def _draw_frame(self) -> None:
        if (
            self._stop_requested
            or not self._running
            or not self._force_terminal
        ):
            return
        self._erase_frame()
        if self._stream_line_open:
            self._write("\x1b[1B\r")
        frame, cursor_row, cursor_column = self._build_frame()
        width = self._term_width()
        for line in frame:
            self._write(self._ansi_line(line, width) + "\n")
        rows_to_cursor = len(frame) - cursor_row
        if rows_to_cursor > 0:
            self._write(f"\x1b[{rows_to_cursor}A")
        self._write(f"\r\x1b[{cursor_column + 1}G")
        self._frame_visible = True
        self._frame_line_count = len(frame)
        self._frame_cursor_row = cursor_row
        self._frame_cursor_column = cursor_column

    def _erase_frame(self) -> None:
        if not self._frame_visible:
            return
        self._write(f"\x1b[{self._frame_cursor_row}A\r")
        for index in range(self._frame_line_count):
            self._write("\x1b[2K")
            if index < self._frame_line_count - 1:
                self._write("\x1b[1B\r")
        if self._frame_line_count > 1:
            self._write(f"\x1b[{self._frame_line_count - 1}A")
        self._write("\r")
        if self._stream_line_open:
            self._write(f"\x1b[1A\r\x1b[{self._stream_cursor_column + 1}G")
        self._frame_visible = False
        self._frame_line_count = 0

    def _write(self, data: str) -> None:
        self._output.write(data)
        self._output.flush()

    def _schedule_spinner(self) -> None:
        if self._loop is None or not self._working or self._stop_requested:
            return
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._draw_frame()
        self._spinner_handle = self._loop.call_later(0.1, self._schedule_spinner)

    def _cancel_timer(self, attribute: str) -> None:
        handle = getattr(self, attribute)
        if handle is not None:
            handle.cancel()
            setattr(self, attribute, None)

    def _read_input(self) -> None:
        if self._input_fd is None:
            return
        data = os.read(self._input_fd, 4096)
        if not data:
            self.input_queue.put_nowait("/exit")
            return
        self._pending_input.extend(data)
        self._consume_input()

    def _consume_input(self) -> None:
        sequences = sorted(KEY_SEQUENCES, key=len, reverse=True)
        while self._pending_input:
            data = bytes(self._pending_input)
            if data.startswith(PASTE_START):
                end = data.find(PASTE_END, len(PASTE_START))
                if end < 0:
                    return
                pasted = data[len(PASTE_START) : end].decode("utf-8", errors="replace")
                del self._pending_input[: end + len(PASTE_END)]
                self._insert_text(pasted.replace("\r\n", "\n").replace("\r", "\n"))
                continue

            match = next(
                (sequence for sequence in sequences if data.startswith(sequence)),
                None,
            )
            if match is not None:
                del self._pending_input[: len(match)]
                self._handle_key(KEY_SEQUENCES[match])
                continue
            if any(sequence.startswith(data) for sequence in sequences):
                return

            if data.startswith(b"\x1b"):
                if len(data) == 1:
                    if self._escape_handle is None and self._loop is not None:
                        self._escape_handle = self._loop.call_later(
                            0.04, self._flush_escape
                        )
                    return
                if data.startswith((b"\x1b\r", b"\x1b\n")):
                    del self._pending_input[:2]
                    self._handle_key("newline")
                    continue
                alt_key = data[1:2].decode("ascii", errors="ignore").casefold()
                if alt_key in {"s", "f"}:
                    del self._pending_input[:2]
                    self._handle_key("steer" if alt_key == "s" else "followup")
                    continue
                if data.startswith(b"\x1b["):
                    if len(data) < 3:
                        return
                    del self._pending_input[:1]
                    self._handle_key("escape")
                    continue
                del self._pending_input[:2]
                continue

            first = data[0]
            if first == 3:
                del self._pending_input[:1]
                self._handle_key("stop")
                continue
            if first == 4:
                del self._pending_input[:1]
                self._handle_key("exit")
                continue
            if first == 9:
                del self._pending_input[:1]
                self._handle_key("tab")
                continue
            if first == 15:
                del self._pending_input[:1]
                self._handle_key("help")
                continue
            if first in (8, 127):
                del self._pending_input[:1]
                self._handle_key("backspace")
                continue
            if first == 1:
                del self._pending_input[:1]
                self._handle_key("home")
                continue
            if first == 5:
                del self._pending_input[:1]
                self._handle_key("end")
                continue
            if first in (10, 13):
                del self._pending_input[:1]
                self._handle_key("enter")
                continue

            char_length = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            if len(data) < char_length:
                return
            char = data[:char_length].decode("utf-8", errors="replace")
            del self._pending_input[:char_length]
            self._insert_text(char)

    def _flush_escape(self) -> None:
        self._escape_handle = None
        if self._pending_input == b"\x1b":
            self._pending_input.clear()
            self._handle_key("escape")
        else:
            self._consume_input()

    def _handle_key(self, key: str) -> None:
        if key == "enter":
            self._submit()
        elif key == "newline":
            self._insert_text("\n")
        elif key == "steer":
            self._submit("steer")
        elif key == "followup":
            self._submit("followup")
        elif key == "stop":
            self._input_text = ""
            self._cursor = 0
            self.input_queue.put_nowait("/stop")
            self._draw_frame()
        elif key == "exit":
            self._input_text = ""
            self._cursor = 0
            self.input_queue.put_nowait("/exit")
            self._draw_frame()
        elif key == "help":
            self.input_queue.put_nowait("/help full")
        elif key == "backspace" and self._cursor > 0:
            self._input_text = (
                self._input_text[: self._cursor - 1]
                + self._input_text[self._cursor :]
            )
            self._cursor -= 1
            self._draw_frame()
        elif key == "delete" and self._cursor < len(self._input_text):
            self._input_text = (
                self._input_text[: self._cursor]
                + self._input_text[self._cursor + 1 :]
            )
            self._draw_frame()
        elif key == "left":
            self._cursor = max(0, self._cursor - 1)
            self._draw_frame()
        elif key == "right":
            self._cursor = min(len(self._input_text), self._cursor + 1)
            self._draw_frame()
        elif key == "home":
            self._move_cursor_to_line_start()
            self._draw_frame()
        elif key == "end":
            self._move_cursor_to_line_end()
            self._draw_frame()
        elif key == "up":
            self._move_cursor_vertical(-1)
        elif key == "down":
            self._move_cursor_vertical(1)
        elif key == "tab":
            self._complete_command()
        elif key == "escape":
            self._draw_frame()

    def _submit(self, command: str | None = None) -> None:
        text = self._input_text
        if command is not None:
            text = f"/{command} {text}".rstrip()
        self._input_text = ""
        self._cursor = 0
        self.input_queue.put_nowait(text)
        self._draw_frame()

    def _insert_text(self, text: str) -> None:
        self._input_text = (
            self._input_text[: self._cursor]
            + text
            + self._input_text[self._cursor :]
        )
        self._cursor += len(text)
        self._draw_frame()

    def _move_cursor_to_line_start(self) -> None:
        start = self._input_text.rfind("\n", 0, self._cursor) + 1
        self._cursor = start

    def _move_cursor_to_line_end(self) -> None:
        end = self._input_text.find("\n", self._cursor)
        self._cursor = len(self._input_text) if end < 0 else end

    def _move_cursor_vertical(self, direction: int) -> None:
        if "\n" not in self._input_text:
            return
        line_start = self._input_text.rfind("\n", 0, self._cursor) + 1
        line_end = self._input_text.find("\n", self._cursor)
        if line_end < 0:
            line_end = len(self._input_text)
        column = self._cursor - line_start
        if direction < 0 and line_start > 0:
            previous_start = self._input_text.rfind("\n", 0, line_start - 1) + 1
            self._cursor = min(previous_start + column, line_start - 1)
        elif direction > 0 and line_end < len(self._input_text):
            next_end = self._input_text.find("\n", line_end + 1)
            if next_end < 0:
                next_end = len(self._input_text)
            self._cursor = min(line_end + 1 + column, next_end)
        self._draw_frame()

    def _complete_command(self) -> None:
        matches = self._command_suggestions()
        if not matches:
            return
        command = matches[0][0]
        suffix = " " if len(matches) == 1 else ""
        self._input_text = command + suffix + self._input_text[self._cursor :]
        self._cursor = len(command + suffix)
        self._draw_frame()
