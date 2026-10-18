"""Polished PySide6 chat window for Sec Desktop."""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, QStringListModel, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sec.apps.desktop.appearance import application_icon
from sec.apps.desktop.floating_ball import FloatingBall
from sec.apps.desktop.runtime import SecRuntime


class CommandInput(QPlainTextEdit):
    """Multiline prompt editor with slash-command completion."""

    submitted = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._completer = QCompleter(self)
        self._completion_model = QStringListModel(self)
        self._completer.setModel(self._completion_model)
        self._completer.setWidget(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.activated[str].connect(self._insert_completion)

    def set_commands(self, commands: tuple[str, ...]) -> None:
        self._completion_model.setStringList(list(commands))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        popup = self._completer.popup()
        if popup.isVisible() and event.key() in {
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Escape,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        }:
            event.ignore()
            return
        if event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return} and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(event)
        self._update_completion()

    def _update_completion(self) -> None:
        cursor = self.textCursor()
        line_prefix = cursor.block().text()[: cursor.positionInBlock()]
        command_prefix = line_prefix.lstrip()
        if not command_prefix.startswith("/") or any(
            character.isspace() for character in command_prefix
        ):
            self._completer.popup().hide()
            return

        self._completer.setCompletionPrefix(command_prefix)
        if self._completer.completionCount() == 0:
            self._completer.popup().hide()
            return
        self._completer.setCurrentRow(0)
        popup = self._completer.popup()
        popup.setCurrentIndex(self._completer.currentIndex())
        rect = self.cursorRect()
        rect.setWidth(
            popup.sizeHintForColumn(0)
            + popup.verticalScrollBar().sizeHint().width()
        )
        self._completer.complete(rect)

    def _insert_completion(self, completion: str) -> None:
        cursor = self.textCursor()
        line_prefix = cursor.block().text()[: cursor.positionInBlock()]
        token = line_prefix.lstrip().split(maxsplit=1)[0]
        cursor.setPosition(cursor.position() - len(token))
        cursor.movePosition(
            QTextCursor.MoveOperation.Right,
            QTextCursor.MoveMode.KeepAnchor,
            len(token),
        )
        cursor.insertText(f"{completion} ")
        self.setTextCursor(cursor)


class FloatingWindow(QMainWindow):
    def __init__(
        self,
        workspace: Path,
        *,
        enabled_extensions: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.setWindowTitle("Sec Desktop")
        self.setWindowIcon(application_icon())
        self.setMinimumSize(900, 600)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )
        self._settings = QSettings("Sec", "Sec Desktop")
        self._session_id: str | None = None
        self._active_assistant = False
        self._messages: list[tuple[str, str]] = []
        self._runtime = SecRuntime(
            workspace,
            enabled_extensions=enabled_extensions,
        )
        self._build_ui()
        self._ball = FloatingBall(self)
        self._ball.restore_requested.connect(self.show_focus_mode)
        self._ball.quit_requested.connect(self._quit_application)
        self._ball.setToolTip("Sec · 点击返回大屏专注，拖动可移动，右键打开菜单")
        self._connect_runtime()
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1120, 760)
        self._runtime.start()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#mainPane { background: #f7f8fa; color: #202124; }
            QWidget#sidebar { background: #f1f3f5; }
            QLabel#brand { font-size: 18px; font-weight: 700; color: #1c2330; }
            QLabel#sectionLabel { color: #858b95; font-size: 11px; font-weight: 600; }
            QLabel#sessionTitle { color: #222831; font-size: 14px; font-weight: 600; }
            QLabel#subtle { color: #8a9099; font-size: 11px; }
            QPushButton {
                background: transparent; border: 0; border-radius: 8px;
                color: #3b424d; padding: 8px 10px; text-align: left;
            }
            QPushButton:hover { background: #e6e9ed; }
            QPushButton#primary {
                background: #252b35; color: white; font-weight: 600;
                padding: 9px 16px;
            }
            QPushButton#primary:hover { background: #3a4350; }
            QPushButton#stop {
                border: 1px solid #d9dde3; background: white; color: #414854;
            }
            QPushButton#stop:hover { background: #f0f1f3; }
            QListWidget#sessions {
                background: transparent; border: 0; outline: 0;
            }
            QListWidget#sessions::item {
                border-radius: 8px; padding: 8px 10px; margin: 1px 2px;
                color: #333a45;
            }
            QListWidget#sessions::item:hover { background: #e8ebef; }
            QListWidget#sessions::item:selected {
                background: white; color: #1d232c;
            }
            QFrame#composer {
                background: white; border: 1px solid #dfe3e8; border-radius: 16px;
            }
            QPlainTextEdit#input {
                background: transparent; border: 0; padding: 6px 8px;
                color: #20242b; selection-background-color: #dce7ff;
            }
            QTextBrowser#conversation {
                background: transparent; border: 0; padding: 12px 16px;
                selection-background-color: #dce7ff;
            }
            QSplitter::handle { background: #e1e4e8; width: 1px; }
            QCheckBox { color: #68707c; spacing: 6px; }
            """
        )

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_main_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([252, 868])
        self.setCentralWidget(splitter)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(310)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 18, 14, 14)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(application_icon().pixmap(28, 28))
        brand_row.addWidget(brand_icon)
        brand = QLabel("Sec")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        self._pin = QCheckBox("置顶")
        self._pin.setChecked(True)
        self._pin.toggled.connect(self._set_pinned)
        brand_row.addWidget(self._pin)
        layout.addLayout(brand_row)

        self._new_chat = QPushButton("＋  新对话")
        self._new_chat.clicked.connect(lambda: self._submit_command("/new"))
        layout.addWidget(self._new_chat)

        commands_button = QPushButton("⌘  命令与快捷方式")
        commands_button.clicked.connect(lambda: self._submit_command("/help"))
        layout.addWidget(commands_button)

        permissions_button = QPushButton("◇  工具权限")
        permissions_button.clicked.connect(lambda: self._submit_command("/mode"))
        layout.addWidget(permissions_button)

        section = QHBoxLayout()
        recent_label = QLabel("最近会话")
        recent_label.setObjectName("sectionLabel")
        section.addWidget(recent_label)
        section.addStretch(1)
        self._refresh_sessions_button = QPushButton("↻")
        self._refresh_sessions_button.setToolTip("刷新最近会话")
        self._refresh_sessions_button.clicked.connect(
            lambda: self._submit_command("/resume")
        )
        section.addWidget(self._refresh_sessions_button)
        layout.addLayout(section)

        self._sessions = QListWidget()
        self._sessions.setObjectName("sessions")
        self._sessions.itemClicked.connect(self._select_session)
        layout.addWidget(self._sessions, 1)

        workspace_caption = QLabel("工作区")
        workspace_caption.setObjectName("sectionLabel")
        self._workspace = QLabel(str(self._runtime.workspace))
        self._workspace.setObjectName("subtle")
        self._workspace.setWordWrap(True)
        self._workspace.setToolTip(str(self._runtime.workspace))
        layout.addWidget(workspace_caption)
        layout.addWidget(self._workspace)
        return sidebar

    def _build_main_pane(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("mainPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(28, 18, 28, 20)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_stack = QVBoxLayout()
        self._title = QLabel("新对话")
        self._title.setObjectName("sessionTitle")
        self._session_caption = QLabel("SEC 智能助手")
        self._session_caption.setObjectName("subtle")
        title_stack.addWidget(self._title)
        title_stack.addWidget(self._session_caption)
        header.addLayout(title_stack)
        header.addStretch(1)
        self._status = QLabel("正在连接…")
        self._status.setObjectName("subtle")
        header.addWidget(self._status)
        self._compact = QPushButton("切换悬浮球")
        self._compact.setToolTip("收起为悬浮球，保留当前会话和任务")
        self._compact.clicked.connect(self.show_ball_mode)
        header.addWidget(self._compact)
        layout.addLayout(header)

        self._welcome = self._build_welcome()
        layout.addWidget(self._welcome)

        self._transcript = QTextBrowser()
        self._transcript.setObjectName("conversation")
        self._transcript.setOpenExternalLinks(False)
        self._transcript.setPlaceholderText("")
        layout.addWidget(self._transcript, 1)

        self._composer = self._build_composer()
        layout.addWidget(self._composer)
        return pane

    def _build_welcome(self) -> QWidget:
        welcome = QWidget()
        layout = QVBoxLayout(welcome)
        layout.setContentsMargins(0, 28, 0, 12)
        layout.addStretch(1)
        title = QLabel("今天想一起完成什么？")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        title.setStyleSheet("font-size: 25px; font-weight: 650; color: #242a33;")
        subtitle = QLabel("直接提问，或使用 SEC 命令管理会话、权限和工具。")
        subtitle.setObjectName("subtle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        suggestions = QHBoxLayout()
        suggestions.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        for label, command in (
            ("查看可用命令", "/help"),
            ("查看权限模式", "/mode"),
            ("重新加载资源", "/reload"),
        ):
            button = QPushButton(label)
            button.setStyleSheet(
                "QPushButton { background: white; border: 1px solid #e2e5e9; "
                "border-radius: 14px; padding: 8px 12px; }"
            )
            button.clicked.connect(
                lambda _checked=False, value=command: self._submit_command(value)
            )
            suggestions.addWidget(button)
        layout.addLayout(suggestions)
        layout.addStretch(1)
        return welcome

    def _build_composer(self) -> QFrame:
        composer = QFrame()
        composer.setObjectName("composer")
        layout = QVBoxLayout(composer)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._input = CommandInput()
        self._input.setObjectName("input")
        self._input.setPlaceholderText(
            "发消息给 Sec…  输入 / 可浏览命令，⌘/Ctrl + Enter 发送"
        )
        self._input.setMinimumHeight(58)
        self._input.setMaximumHeight(130)
        self._input.submitted.connect(self._send_prompt)
        layout.addWidget(self._input)

        toolbar = QHBoxLayout()
        help_button = QPushButton("/help")
        help_button.setToolTip("查看可用命令")
        help_button.clicked.connect(lambda: self._submit_command("/help"))
        mode_button = QPushButton("/mode")
        mode_button.setToolTip("查看或切换工具权限模式")
        mode_button.clicked.connect(lambda: self._submit_command("/mode"))
        reload_button = QPushButton("/reload")
        reload_button.setToolTip("重新加载 Skills 和 Prompts")
        reload_button.clicked.connect(lambda: self._submit_command("/reload"))
        toolbar.addWidget(help_button)
        toolbar.addWidget(mode_button)
        toolbar.addWidget(reload_button)
        toolbar.addStretch(1)
        self._stop = QPushButton("停止")
        self._stop.setObjectName("stop")
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self._runtime.cancel_run)
        toolbar.addWidget(self._stop)
        self._send = QPushButton("发送  ⏎")
        self._send.setObjectName("primary")
        self._send.setEnabled(False)
        self._send.clicked.connect(self._send_prompt)
        toolbar.addWidget(self._send)
        layout.addLayout(toolbar)
        return composer

    def _connect_runtime(self) -> None:
        self._runtime.ready.connect(self._runtime_ready)
        self._runtime.startup_failed.connect(self._show_error)
        self._runtime.text_delta.connect(self._append_delta)
        self._runtime.status_changed.connect(self._update_status)
        self._runtime.approval_requested.connect(self._approve_tool)
        self._runtime.command_result.connect(self._show_command_result)
        self._runtime.command_completions.connect(self._set_command_completions)
        self._runtime.session_changed.connect(self._set_session_id)
        self._runtime.session_history.connect(self._set_session_history)
        self._runtime.sessions_updated.connect(self._set_sessions)
        self._runtime.assistant_started.connect(self._assistant_started)
        self._runtime.exit_requested.connect(self.close)
        self._runtime.run_failed.connect(self._show_error)
        self._runtime.run_finished.connect(self._run_finished)

    def _runtime_ready(self, workspace: str) -> None:
        self._workspace.setText(workspace)
        self._workspace.setToolTip(workspace)
        self._status.setText("就绪")
        self._send.setEnabled(True)
        self._input.setFocus()

    def _send_prompt(self) -> None:
        prompt = self._input.toPlainText().strip()
        if not prompt:
            return
        self._input.clear()
        if not prompt.startswith("/"):
            self._messages.append(("user", prompt))
            self._append_user_message(prompt)
            self._welcome.hide()
        self._runtime.submit_input(prompt)

    def _submit_command(self, command: str) -> None:
        self._runtime.submit_input(command)

    def _append_user_message(self, text: str) -> None:
        cursor = self._transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._transcript.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(
            '<p style="margin:12px 0 6px;color:#7c8490;font-size:12px">'
            "你</p>"
            f'<p style="margin:0 0 18px;color:#252b34">{escape(text).replace(chr(10), "<br>")}</p>'
        )
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _assistant_started(self) -> None:
        self._active_assistant = True
        self._messages.append(("assistant", ""))
        self._welcome.hide()
        cursor = self._transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        cursor.insertHtml(
            '<p style="margin:12px 0 6px;color:#7c8490;font-size:12px">'
            "Sec</p><p style=\"margin:0 0 18px;color:#252b34\">"
        )
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _append_delta(self, text: str) -> None:
        if not self._active_assistant:
            return
        role, previous = self._messages[-1]
        self._messages[-1] = (role, previous + text)
        cursor = self._transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _set_session_history(self, messages: tuple[tuple[str, str], ...]) -> None:
        self._messages = list(messages)
        self._active_assistant = False
        self._title.setText("新对话" if not messages else "对话")
        if not messages:
            self._transcript.clear()
            self._welcome.show()
            return
        self._welcome.hide()
        markdown = "\n\n".join(
            f"**{'你' if role == 'user' else 'Sec'}**\n\n{text}"
            for role, text in messages
        )
        self._transcript.setMarkdown(markdown)
        self._transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _show_command_result(self, message: str, display_kind: str) -> None:
        if not message:
            return
        colors = {
            "error": "#a93636",
            "info": "#566273",
            "follow_up": "#536d9b",
            "steer": "#536d9b",
            "should_stop": "#8b6c39",
        }
        color = colors.get(display_kind, "#566273")
        cursor = self._transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertBlock()
        cursor.insertHtml(
            f'<p style="background:#fff;border:1px solid #e5e8ec;'
            f'border-radius:8px;padding:9px 11px;color:{color};">'
            f"{escape(message).replace(chr(10), '<br>')}</p>"
        )
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()
        self._welcome.hide()

    def _set_command_completions(
        self,
        items: tuple[tuple[str, str], ...],
    ) -> None:
        self._input.set_commands(tuple(name for name, _description in items))

    def _set_session_id(self, session_id: str) -> None:
        self._session_id = session_id
        self._title.setText("新对话" if not self._messages else "对话")
        self._session_caption.setText(f"Session  ·  {session_id[:10]}")
        self._select_active_session()

    def _set_sessions(
        self,
        sessions: tuple[tuple[str, str | None, float | None], ...],
    ) -> None:
        self._sessions.clear()
        for session_id, name, updated_at in sessions:
            title = name or f"对话 {session_id[:8]}"
            if updated_at is not None:
                date = datetime.fromtimestamp(updated_at).strftime("%m-%d %H:%M")
                label = f"{title}  ·  {date}"
            else:
                label = title
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, session_id)
            item.setToolTip(f"{title}\n{session_id}")
            self._sessions.addItem(item)
        self._select_active_session()

    def _select_active_session(self) -> None:
        for index in range(self._sessions.count()):
            item = self._sessions.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == self._session_id:
                self._sessions.setCurrentItem(item)
                return
        self._sessions.clearSelection()

    def _select_session(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id and session_id != self._session_id:
            self._submit_command(f"/resume {session_id}")

    def _update_status(self, status: str) -> None:
        self._status.setText(status)
        self._stop.setEnabled(
            status in {"正在运行"} or status.startswith(("工具运行中", "等待工具审批"))
        )

    def _run_finished(self) -> None:
        self._active_assistant = False
        if self._messages:
            markdown = "\n\n".join(
                f"**{'你' if role == 'user' else 'Sec'}**\n\n{text}"
                for role, text in self._messages
            )
            self._transcript.setMarkdown(markdown)
            self._transcript.moveCursor(QTextCursor.MoveOperation.End)
        self._send.setEnabled(True)
        self._stop.setEnabled(False)
        self._input.setFocus()

    def _approve_tool(self, tool_call_id: str, tool_name: str, arguments: str) -> None:
        if self._ball.isVisible():
            self.show_focus_mode()
        answer = QMessageBox.question(
            self,
            f"允许工具：{tool_name}",
            f"SEC 请求运行工具 {tool_name}。\n\n"
            f"参数：\n{arguments}\n\n"
            "请确认这是你信任的操作。工具在当前用户权限下运行，不是沙箱。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        approved = answer == QMessageBox.StandardButton.Yes
        self._runtime.resolve_approval(tool_call_id, approved)
        if not approved:
            self._show_command_result(f"已拒绝工具调用：{tool_name}", "info")

    def _show_error(self, message: str) -> None:
        if self._ball.isVisible():
            self.show_focus_mode()
        self._status.setText("发生错误")
        QMessageBox.critical(self, "Sec Desktop", message)

    def _set_pinned(self, pinned: bool) -> None:
        flags = self.windowFlags()
        if pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def show_ball_mode(self) -> None:
        if self._ball.isVisible():
            return
        self._settings.setValue("window/geometry", self.saveGeometry())
        position = self._settings.value("ball/position")
        if not isinstance(position, QPoint):
            area = self.screen().availableGeometry()
            position = QPoint(
                area.right() - self._ball.width() - 20,
                area.center().y() - self._ball.height() // 2,
            )
        self._ball.move_on_screen(position)
        self._input._completer.popup().hide()
        self.hide()
        self._ball.show()
        self._ball.raise_()

    def show_focus_mode(self) -> None:
        if self._ball.isVisible():
            self._settings.setValue("ball/position", self._ball.pos())
        self.show()
        self._ball.hide()
        self.raise_()
        self.activateWindow()
        self._input.setFocus()

    def _quit_application(self) -> None:
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        if self._ball.isVisible():
            self._settings.setValue("ball/position", self._ball.pos())
        self._ball.close()
        self._runtime.shutdown()
        event.accept()
