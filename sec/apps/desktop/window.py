"""Polished PySide6 chat window for Sec Desktop."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QActionGroup, QCloseEvent, QColor, QIcon, QKeyEvent, QPainter, QPaintEvent, QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sec.apps.desktop.appearance import application_icon, control_icon
from sec.apps.desktop.conversation import Conversation, MessageBubble
from sec.apps.desktop.floating_ball import FloatingBall
from sec.apps.desktop.runtime import SecRuntime
from sec.apps.desktop.session_list import SessionItemDelegate
from sec.apps.desktop.tool_page import TOOL_CATEGORIES, ToolInventory, ToolKind, ToolPage
from sec.apps.desktop.trace_page import TracePage
from sec.apps.desktop.commands_page import CommandsPage
from sec.apps.desktop.scheduled_tasks_page import ScheduledTasksPage
from sec.apps.desktop.memory_page import MemoryPage
from sec.apps.desktop.toast import ToastManager
from sec.apps.desktop.dialogs import confirm_delete, export_session, rename_session, show_error


_ICON_TOGGLE_STYLE = (
    "QPushButton { padding: 0; text-align: center; }"
    "QPushButton:checked { background: #dfe7f2; }"
)


class ComposerButton(QPushButton):
    """Keep visible icon/text bounds independent of native button/menu margins."""

    def sizeHint(self) -> QSize:
        return QSize(8 + self.iconSize().width() + 6 + self.fontMetrics().horizontalAdvance(self.text()), 36)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isEnabled() and (self.underMouse() or self.isDown() or self.hasFocus()):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e6e9ed"))
            painter.drawRoundedRect(self.rect(), 8, 8)
        size = self.iconSize()
        self.icon().paint(
            painter, QRect(4, (self.height() - size.height()) // 2, size.width(), size.height()),
            Qt.AlignmentFlag.AlignCenter,
            QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled,
        )
        painter.setPen(self.palette().buttonText().color())
        painter.drawText(
            QRect(4 + size.width() + 6, 0, self.width() - size.width() - 14, self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text(),
        )


class CommandInput(QPlainTextEdit):
    """Multiline prompt editor with slash-command completion."""

    submitted = Signal()
    steer_requested = Signal()
    followup_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._completer = QCompleter(self)
        self._completion_model = QStandardItemModel(self)
        self._completer.setModel(self._completion_model)
        self._completer.setCompletionRole(Qt.ItemDataRole.UserRole)
        self._completer.setWidget(self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.activated[str].connect(self._insert_completion)
        self._completer.setMaxVisibleItems(7)
        self._completer.popup().setStyleSheet(
            "QAbstractItemView { background: white; color: #000; border: 1px solid #dfe3ea;"
            "border-radius: 10px; padding: 6px; outline: 0; font-size: 12px; }"
            "QAbstractItemView::item { padding: 10px 12px; border-radius: 6px; }"
            "QAbstractItemView::item:selected { background: #edf2fa; color: #000; }"
        )

    def set_commands(self, commands: tuple[tuple[str, str], ...]) -> None:
        self._completion_model.clear()
        for name, description in commands:
            item = QStandardItem(f"{name}    {description}")
            item.setData(name, Qt.ItemDataRole.UserRole)
            item.setToolTip(description)
            self._completion_model.appendRow(item)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return}:
            modifiers = event.modifiers()
            primary = Qt.KeyboardModifier.ControlModifier
            if modifiers == primary | Qt.KeyboardModifier.ShiftModifier:
                self._completer.popup().hide()
                self.steer_requested.emit()
                return
            if modifiers == primary | Qt.KeyboardModifier.AltModifier:
                self._completer.popup().hide()
                self.followup_requested.emit()
                return
            if modifiers == primary:
                self._completer.popup().hide()
                self.submitted.emit()
                return
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
        rect.setWidth(min(560, max(340, self.width())))
        self._completer.complete(rect)

    def _insert_completion(self, completion: str) -> None:
        cursor = self.textCursor()
        line_prefix = cursor.block().text()[: cursor.positionInBlock()]
        token = line_prefix.lstrip()
        if not token.startswith("/") or any(character.isspace() for character in token):
            return
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
        self._session_names: dict[str, str] = {}
        self._send_mode = "replace"
        self._ready = False
        self._run_active = False
        self._history_timestamps: tuple[float, ...] = ()
        self._active_assistant = False
        self._assistant_index: int | None = None
        self._assistant_bubble: MessageBubble | None = None
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
                border: 1px solid #252b35; padding: 9px 16px;
            }
            QPushButton#primary:hover { background: #3a4350; border-color: #3a4350; }
            QPushButton#primary[stopping="true"] {
                border: 1px solid #d9dde3; background: white; color: #414854;
            }
            QPushButton#primary[stopping="true"]:hover {
                background: #f0f1f3; border-color: #d9dde3;
            }
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
            QLineEdit#sessionSearch {
                background: white; color: #202124; padding: 7px 10px;
                border: 1px solid #dfe3ea; border-radius: 8px;
            }
            QLineEdit#sessionSearch:focus { border-color: #a8b8cf; }
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
        self._main_splitter = splitter
        splitter.setChildrenCollapsible(False)
        self._sidebar = self._build_sidebar()
        splitter.addWidget(self._sidebar)
        splitter.addWidget(self._build_main_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([252, 868])
        self.setCentralWidget(splitter)

        self._toasts = ToastManager(self)
        self._toasts.setGeometry(self.rect())
        self._toasts.raise_()

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
        brand = QLabel("Sec · 小二")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        self._pin = QPushButton()
        self._pin.setCheckable(True)
        self._pin.setIcon(control_icon("pin"))
        self._pin.setFixedSize(32, 32)
        self._pin.setToolTip("窗口置顶（可切换）")
        self._pin.setAccessibleName("窗口置顶")
        self._pin.setStyleSheet(_ICON_TOGGLE_STYLE)
        self._pin.setChecked(True)
        self._pin.toggled.connect(self._set_pinned)
        brand_row.addWidget(self._pin)
        layout.addLayout(brand_row)

        self._new_chat = QPushButton("新对话")
        self._new_chat.setIcon(control_icon("new"))
        self._new_chat.setEnabled(False)
        self._new_chat.clicked.connect(lambda: self._submit_command("/new"))
        layout.addWidget(self._new_chat)

        commands_button = QPushButton("快捷指令")
        commands_button.setIcon(control_icon("instructions"))
        commands_button.clicked.connect(lambda: self._submit_command("/help"))
        layout.addWidget(commands_button)

        self._scheduled_tasks = QPushButton("定时任务")
        self._scheduled_tasks.setIcon(control_icon("timer", retain_disabled_color=True))
        self._scheduled_tasks.setToolTip("定时任务")
        self._scheduled_tasks.clicked.connect(self._show_scheduled_tasks)
        layout.addWidget(self._scheduled_tasks)

        self._memory_button = QPushButton("知识与记忆")
        self._memory_button.setIcon(control_icon("memory"))
        self._memory_button.setCheckable(True)
        self._memory_button.setToolTip("展开知识与记忆分类")
        self._memory_button.setStyleSheet(
            "QPushButton { padding-right: 34px; }"
            "QPushButton:checked { background: #e6e9ed; }"
        )
        memory_content = QHBoxLayout(self._memory_button)
        memory_content.setContentsMargins(0, 0, 10, 0)
        memory_content.addStretch()
        self._memory_more = QLabel()
        self._memory_more.setPixmap(control_icon("more").pixmap(16, 16))
        self._memory_more.setFixedSize(16, 16)
        self._memory_more.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        memory_content.addWidget(self._memory_more)
        self._memory_button.toggled.connect(self._toggle_memory)
        layout.addWidget(self._memory_button)

        self._memory_resources = QTreeWidget()
        self._memory_resources.setHeaderHidden(True)
        self._memory_resources.setRootIsDecorated(False)
        self._memory_resources.setColumnCount(1)
        self._memory_resources.setIndentation(0)
        self._memory_resources.setExpandsOnDoubleClick(False)
        self._memory_resources.itemClicked.connect(
            lambda item, _column: self._open_memory(
                item.data(0, Qt.ItemDataRole.UserRole)
            )
        )
        self._memory_resources.setFixedHeight(80)
        self._memory_resources.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._memory_resources.setStyleSheet(
            "QTreeWidget { background: #e9edf2; color: #333a45; border: 0;"
            "border-radius: 8px; padding: 4px 0 4px 22px; }"
            "QTreeWidget::item { padding: 8px 10px; }"
            "QTreeWidget::item:hover { background: #e0e6ee; }"
            "QTreeWidget::item:selected { background: #dce5f0; color: #202124; }"
        )
        self._memory_resources.setToolTip("选择知识或长期记忆管理")
        for kind, title in (("knowledge", "知识管理"), ("memory", "长期记忆")):
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, kind)
            self._memory_resources.addTopLevelItem(item)
        layout.addWidget(self._memory_resources)
        self._memory_resources.hide()

        self._tools_button = QPushButton("扩展能力")
        self._tools_button.setIcon(control_icon("components"))
        self._tools_button.setCheckable(True)
        self._tools_button.setToolTip("展开扩展能力分类")
        self._tools_button.setStyleSheet(
            "QPushButton { padding-right: 34px; }"
            "QPushButton:checked { background: #e6e9ed; }"
        )
        tools_content = QHBoxLayout(self._tools_button)
        tools_content.setContentsMargins(0, 0, 10, 0)
        tools_content.addStretch()
        self._tools_more = QLabel()
        self._tools_more.setPixmap(control_icon("more").pixmap(16, 16))
        self._tools_more.setFixedSize(16, 16)
        self._tools_more.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        tools_content.addWidget(self._tools_more)
        self._tools_button.toggled.connect(self._toggle_tools)
        layout.addWidget(self._tools_button)

        self._resources = QTreeWidget()
        self._resources.setHeaderHidden(True)
        self._resources.setRootIsDecorated(False)
        self._resources.setColumnCount(2)
        self._resources.setIndentation(0)
        self._resources.setExpandsOnDoubleClick(False)
        self._resources.itemClicked.connect(
            lambda item, _column: self._open_tools(item.data(0, Qt.ItemDataRole.UserRole))
        )
        self._resources.setFixedHeight(160)
        self._resources.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._resources.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._resources.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._resources.setColumnWidth(1, 40)
        self._resources.setStyleSheet(
            "QTreeWidget { background: #e9edf2; color: #333a45; border: 0;"
            "border-radius: 8px; padding: 4px 0 4px 22px; }"
            "QTreeWidget::item { padding: 8px 10px; }"
            "QTreeWidget::item:hover { background: #e0e6ee; }"
            "QTreeWidget::item:selected { background: #dce5f0; color: #202124; }"
        )
        self._resources.setToolTip("选择扩展能力分类，预览内容并配置权限与激活状态")
        self._inventory = ToolInventory()
        self._resource_counts: dict[ToolKind, QLabel] = {}
        for kind, title in TOOL_CATEGORIES:
            item = QTreeWidgetItem([title, ""])
            item.setData(1, Qt.ItemDataRole.UserRole, "0")
            item.setData(0, Qt.ItemDataRole.UserRole, kind)
            self._resources.addTopLevelItem(item)
            count = QLabel("0")
            count.setObjectName("subtle")
            count.setStyleSheet("color: #9299a3; font-size: 10px;")
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
            count.setContentsMargins(0, 8, 10, 1)
            self._resources.setItemWidget(item, 1, count)
            self._resource_counts[kind] = count
        layout.addWidget(self._resources)
        self._resources.hide()

        section = QHBoxLayout()
        recent_label = QLabel("最近会话")
        recent_label.setObjectName("sectionLabel")
        section.addWidget(recent_label)
        section.addStretch(1)
        self._refresh_sessions_button = QPushButton()
        self._refresh_sessions_button.setIcon(control_icon("refresh"))
        self._refresh_sessions_button.setAccessibleName("刷新最近会话")
        self._refresh_sessions_button.setToolTip("刷新最近会话")
        self._refresh_sessions_button.clicked.connect(
            self._runtime.refresh_sessions
        )
        section.addWidget(self._refresh_sessions_button)
        layout.addLayout(section)

        self._session_search = QLineEdit()
        self._session_search.setObjectName("sessionSearch")
        self._session_search.setPlaceholderText("搜索会话")
        self._session_search.setClearButtonEnabled(True)
        self._session_search.setAccessibleName("搜索最近会话")
        self._session_search.textChanged.connect(self._filter_sessions)
        layout.addWidget(self._session_search)

        self._sessions = QListWidget()
        self._sessions.setObjectName("sessions")
        self._sessions.setItemDelegate(SessionItemDelegate(self._sessions))
        self._sessions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._sessions.itemClicked.connect(self._select_session)
        self._sessions.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sessions.customContextMenuRequested.connect(self._session_menu)
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
        self._sidebar_toggle = QPushButton()
        self._sidebar_toggle.setIcon(control_icon("sidebar"))
        self._sidebar_toggle.setIconSize(QSize(18, 18))
        self._sidebar_toggle.setFixedSize(32, 32)
        self._sidebar_toggle.setCheckable(True)
        self._sidebar_toggle.setToolTip("收起侧边栏")
        self._sidebar_toggle.setAccessibleName("收起侧边栏")
        self._sidebar_toggle.setStyleSheet(_ICON_TOGGLE_STYLE)
        self._sidebar_toggle.toggled.connect(self._toggle_sidebar)
        header.addWidget(self._sidebar_toggle, 0, Qt.AlignmentFlag.AlignTop)
        title_stack = QVBoxLayout()
        self._title = QPushButton("新对话")
        self._title.setObjectName("sessionTitle")
        self._title.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title.setToolTip("点击修改对话名称")
        self._title.setAccessibleName("修改当前对话名称")
        self._title.setEnabled(False)
        self._title.setStyleSheet(
            "QPushButton { color: #222831; font-size: 14px; font-weight: 600;"
            "padding: 4px 6px; border: 0; border-radius: 6px; text-align: left; }"
            "QPushButton:hover { background: #e6e9ed; }"
        )
        self._title.clicked.connect(self._rename_current_session)
        self._session_caption = QLabel("SEC 智能助手")
        self._session_caption.setObjectName("subtle")
        self._session_caption.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._session_caption.setWordWrap(False)
        title_stack.addWidget(self._title, 0, Qt.AlignmentFlag.AlignLeft)
        session_row = QHBoxLayout()
        session_row.setSpacing(8)
        session_row.addWidget(self._session_caption)
        self._status_dot = QLabel()
        self._status_dot.setFixedSize(8, 8)
        self._status_dot.setStyleSheet("background: #9ca3af; border-radius: 4px;")
        session_row.addWidget(self._status_dot)
        self._status = QLabel("正在连接…")
        self._status.setObjectName("subtle")
        session_row.addWidget(self._status)
        session_row.addStretch(1)
        title_stack.addLayout(session_row)
        header.addLayout(title_stack, 1)
        self._compact = QPushButton()
        self._compact.setIcon(control_icon("compact"))
        self._compact.setIconSize(QSize(22, 22))
        self._compact.setFixedSize(36, 36)
        self._compact.setAccessibleName("切换悬浮球")
        self._compact.setToolTip("切换悬浮球 · 保留当前会话和任务")
        self._compact.clicked.connect(self.show_ball_mode)
        self._trace_button = QPushButton()
        self._trace_button.setIcon(control_icon("trace"))
        self._trace_button.setIconSize(QSize(22, 22))
        self._trace_button.setFixedSize(36, 36)
        self._trace_button.setCheckable(True)
        self._trace_button.setStyleSheet(_ICON_TOGGLE_STYLE)
        self._trace_button.setToolTip("观测 · 查看当前与历史 Trace")
        self._trace_button.setAccessibleName("打开执行 Trace")
        self._trace_button.clicked.connect(self._toggle_trace)
        header.addWidget(self._trace_button)
        header.addWidget(self._compact)
        layout.addLayout(header)

        self._pages = QStackedWidget()
        self._chat_page = QWidget()
        chat_layout = QVBoxLayout(self._chat_page)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        self._welcome = self._build_welcome()
        chat_layout.addWidget(self._welcome)

        self._transcript = Conversation()
        self._transcript.approval_decided.connect(self._runtime.resolve_approval)
        self._transcript.setObjectName("conversation")
        chat_layout.addWidget(self._transcript, 1)

        self._composer = self._build_composer()
        chat_layout.addWidget(self._composer)
        self._pages.addWidget(self._chat_page)
        self._tool_page = ToolPage()
        self._tool_page.back_requested.connect(self._show_chat)
        self._tool_page.activation_requested.connect(self._runtime.configure_resource)
        self._tool_page.permission_requested.connect(self._runtime.configure_permission)
        self._pages.addWidget(self._tool_page)
        self._commands_page = CommandsPage()
        self._commands_page.back_requested.connect(self._show_chat)
        self._commands_page.command_selected.connect(self._insert_command)
        self._pages.addWidget(self._commands_page)
        self._knowledge_page = self._build_memory_subpage(
            "知识管理",
            "为 Agent 提供外部数据来源与工作区知识库配置。",
            (
                ("数据源", "预留：配置知识管理的数据源。"),
                ("datalink", "预留：连接外部数据源（数据库 / 文件 / API），为知识管理供数。"),
                ("工作区知识库配置", "预留：配置当前工作区的知识管理目录、索引与同步策略。"),
            ),
        )
        self._pages.addWidget(self._knowledge_page)
        self._memory_page = MemoryPage()
        self._memory_page.setEnabled(False)
        self._memory_page.back_requested.connect(self._show_chat)
        self._pages.addWidget(self._memory_page)
        self._trace_page = TracePage(self._runtime.trace_store)
        self._pages.addWidget(self._trace_page)
        self._scheduled_page = ScheduledTasksPage()
        self._scheduled_page.setEnabled(False)
        self._scheduled_page.back_requested.connect(self._show_chat)
        self._scheduled_page.add_requested.connect(self._runtime.add_scheduled_task)
        self._scheduled_page.remove_requested.connect(self._runtime.remove_scheduled_task)
        self._scheduled_page.toggle_requested.connect(self._runtime.toggle_scheduled_task)
        self._pages.addWidget(self._scheduled_page)
        self._pages.currentChanged.connect(self._sync_trace_button)
        layout.addWidget(self._pages, 1)
        return pane

    def _toggle_sidebar(self, collapsed: bool) -> None:
        if collapsed:
            self._sidebar_sizes = self._main_splitter.sizes()
            self._sidebar.hide()
        else:
            self._sidebar.show()
            self._main_splitter.setSizes(self._sidebar_sizes)
        label = "展开侧边栏" if collapsed else "收起侧边栏"
        self._sidebar_toggle.setToolTip(label)
        self._sidebar_toggle.setAccessibleName(label)

    def _toggle_memory(self, expanded: bool) -> None:
        self._memory_resources.setVisible(expanded)
        self._memory_button.setToolTip(
            "收起知识与记忆分类" if expanded else "展开知识与记忆分类"
        )

    def _open_memory(self, kind: str) -> None:
        self._memory_button.setChecked(True)
        if kind == "knowledge":
            self._pages.setCurrentWidget(self._knowledge_page)
        else:
            self._pages.setCurrentWidget(self._memory_page)
            if self._ready:
                self._runtime.list_memories()
        self._resources.clearSelection()
        for index in range(self._memory_resources.topLevelItemCount()):
            item = self._memory_resources.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == kind:
                self._memory_resources.setCurrentItem(item)

    def _build_memory_subpage(
        self,
        title: str,
        description: str,
        sections: tuple[tuple[str, str], ...],
    ) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        back = QPushButton("返回对话")
        back.clicked.connect(self._show_chat)
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 18px; font-weight: 600; color: #202124;")
        layout.addWidget(heading)
        subtitle = QLabel(description)
        subtitle.setObjectName("subtle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        for name, detail in sections:
            card = QLabel(f"{name}\n\n{detail}")
            card.setWordWrap(True)
            card.setStyleSheet(
                "background: white; color: #414854; border: 1px solid #e1e4e8;"
                "border-radius: 10px; padding: 16px;"
            )
            layout.addWidget(card)
        layout.addStretch()
        return page

    def _build_welcome(self) -> QWidget:
        welcome = QWidget()
        layout = QVBoxLayout(welcome)
        layout.setContentsMargins(0, 28, 0, 12)
        layout.addStretch(1)
        title = QLabel("客官，您是打尖还是住店？")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        title.setStyleSheet("font-size: 25px; font-weight: 650; color: #242a33;")
        subtitle = QLabel("直接提问，或使用 SEC 命令管理会话和扩展能力。")
        subtitle.setObjectName("subtle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        suggestions = QHBoxLayout()
        suggestions.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        for label, command in (
            ("查看可用命令", "/help"),
            ("重新加载资源", "/reload"),
        ):
            button = QPushButton(label)
            button.setIcon(control_icon(command.lstrip("/")))
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
        key = "⌘" if sys.platform == "darwin" else "Ctrl"
        shift = "⇧" if sys.platform == "darwin" else "Shift"
        alt = "⌥" if sys.platform == "darwin" else "Alt"
        self._input.setPlaceholderText(
            f"输入 / 浏览命令，Enter 换行\n\n"
            f"{key}+Enter 发送 · {key}+{shift}+Enter 引导 · {key}+{alt}+Enter 追加"
        )
        self._input.setFixedHeight(70)
        self._input.submitted.connect(lambda: self._submit_shortcut("replace"))
        self._input.steer_requested.connect(lambda: self._submit_shortcut("steer"))
        self._input.followup_requested.connect(lambda: self._submit_shortcut("followup"))
        layout.addWidget(self._input)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(16)
        help_button = ComposerButton("help")
        help_button.setToolTip("查看可用命令")
        help_button.clicked.connect(lambda: self._submit_command("/help"))
        self._agent_button = ComposerButton("agent")
        self._agent_button.setEnabled(False)
        self._agent_button.setToolTip("选择当前 Agent；运行期间的选择会在下一次 Trace 生效")
        self._agent_button.setAccessibleName("智能体选择")
        self._agent_button.clicked.connect(lambda: self._open_tools("agent"))
        self._mode_button = ComposerButton("mode · 普通")
        mode_button = self._mode_button
        mode_button.setToolTip("切换扩展能力权限模式")
        mode_button.setStyleSheet(
            "QPushButton::menu-indicator { image: none; width: 0; }"
        )
        mode_menu = self._control_menu()
        self._mode_actions = {}
        mode_group = QActionGroup(mode_menu)
        mode_group.setExclusive(True)
        for mode, label in (
            ("normal", "普通 · 每次审批"), ("plan", "规划 · 只读"),
            ("auto", "自动 · 减少审批"),
        ):
            action = mode_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == "normal")
            mode_group.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=mode: self._runtime.configure_permission(value)
            )
            self._mode_actions[mode] = action
        mode_button.setMenu(mode_menu)
        mode_button.setEnabled(False)
        reload_button = ComposerButton("reload")
        reload_button.setToolTip("重新加载智能体、技能、提示词、MCP 与工具插件")
        reload_button.clicked.connect(lambda: self._submit_command("/reload"))
        for button, name in (
            (help_button, "help"), (self._agent_button, "agent"),
            (mode_button, "mode"), (reload_button, "reload"),
        ):
            button.setIcon(control_icon(name, retain_disabled_color=name == "agent"))
            button.setIconSize(QSize(16, 16))
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(36)
            button.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)
            toolbar.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        toolbar.addStretch(1)
        self._send = QPushButton("发送")
        self._send.setIcon(control_icon("send"))
        self._send.setToolTip(f"{key}+Enter 发送")
        self._send.setObjectName("primary")
        self._send.setEnabled(False)
        self._send.clicked.connect(self._primary_action)
        self._input.textChanged.connect(self._update_send_button)
        send_group = QWidget()
        send_layout = QHBoxLayout(send_group)
        send_layout.setContentsMargins(0, 0, 0, 0)
        send_layout.setSpacing(0)
        self._send.setStyleSheet(
            "QPushButton#primary { border-top-right-radius: 0; border-bottom-right-radius: 0; }"
        )
        send_layout.addWidget(self._send)
        self._send_options = QPushButton("▾")
        self._send_options.setObjectName("primary")
        self._send_options.setAccessibleName("选择发送模式")
        self._send_options.setToolTip("选择发送模式")
        self._send_options.setEnabled(False)
        self._send_options.setStyleSheet(
            "QPushButton#primary { border: 1px solid #252b35;"
            "border-top-left-radius: 0; border-bottom-left-radius: 0;"
            "border-left: 1px solid #59616d; padding: 9px 10px; text-align: center; }"
            "QPushButton#primary:hover { border-color: #3a4350; border-left-color: #59616d; }"
            "QPushButton::menu-indicator { image: none; width: 0; }"
        )
        send_menu = self._control_menu()
        self._send_actions = {}
        send_modes = QActionGroup(send_menu)
        send_modes.setExclusive(True)
        for mode, label in (
            ("replace", "停止当前后发送"), ("steer", "steer · 引导当前任务"),
            ("followup", "followup · 排队追加"),
        ):
            action = send_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mode == "replace")
            send_modes.addAction(action)
            action.triggered.connect(
                lambda _checked=False, value=mode: self._set_send_mode(value)
            )
            self._send_actions[mode] = action
        self._send_options.setMenu(send_menu)
        self._set_send_mode("replace")
        send_layout.addWidget(self._send_options)
        for button in (self._send, self._send_options):
            button.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)
            button.setFixedHeight(40)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        toolbar.addWidget(send_group)
        layout.addLayout(toolbar)
        return composer

    def _control_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: white; color: #202124; border: 1px solid #dfe3ea;"
            "border-radius: 8px; padding: 6px; font-size: 12px; }"
            "QMenu::item { padding: 9px 16px; border-radius: 6px; }"
            "QMenu::item:selected { background: #edf2fa; color: #202124; }"
        )
        return menu

    def _set_send_mode(self, mode: str) -> None:
        self._send_mode = mode
        self._send_actions[mode].setChecked(True)
        self._update_send_button()
        key = "⌘" if sys.platform == "darwin" else "Ctrl"
        shift = "⇧" if sys.platform == "darwin" else "Shift"
        alt = "⌥" if sys.platform == "darwin" else "Alt"
        self._input.setPlaceholderText(
            f"输入 / 浏览命令，Enter 换行\n\n"
            f"{key}+Enter 发送 · {key}+{shift}+Enter 引导 · {key}+{alt}+Enter 追加"
        )

    def _update_send_button(self) -> None:
        stopping = self._run_active and not self._input.toPlainText().strip()
        label = "停止" if stopping else {
            "replace": "发送", "steer": "引导", "followup": "追加",
        }[self._send_mode]
        self._send.setText(label)
        self._send.setIcon(control_icon("stop" if stopping else "send"))
        self._send.setToolTip(
            "停止当前任务" if stopping else self._send_actions[self._send_mode].text()
        )
        if self._send.property("stopping") != stopping:
            self._send.setProperty("stopping", stopping)
            self._send.style().unpolish(self._send)
            self._send.style().polish(self._send)
            self._send.update()

    def _primary_action(self) -> None:
        if self._run_active and not self._input.toPlainText().strip():
            self._runtime.cancel_run()
        else:
            self._send_prompt()

    def _submit_shortcut(self, mode: str) -> None:
        self._set_send_mode(mode)
        self._send_prompt()

    def _connect_runtime(self) -> None:
        self._runtime.ready.connect(self._runtime_ready)
        self._runtime.startup_failed.connect(self._show_error)
        self._runtime.text_delta.connect(self._append_delta)
        self._runtime.status_changed.connect(self._update_status)
        self._runtime.approval_requested.connect(self._approve_tool)
        self._runtime.extension_ui_requested.connect(self._handle_extension_ui)
        self._runtime.command_result.connect(self._show_command_result)
        self._runtime.command_completions.connect(self._set_command_completions)
        self._runtime.session_changed.connect(self._set_session_id)
        self._runtime.session_history.connect(self._set_session_history)
        self._transcript.message_action.connect(self._message_action)
        self._runtime.session_timing.connect(self._set_session_timing)
        self._runtime.sessions_updated.connect(self._set_sessions)
        self._runtime.resources_updated.connect(self._set_resources)
        self._runtime.configuration_result.connect(self._tool_page.show_result)
        self._runtime.configuration_result.connect(self._on_page_result)
        self._tool_page.agent_requested.connect(self._runtime.select_agent)
        self._tool_page.reload_requested.connect(self._runtime.reload_resources)
        self._runtime.assistant_started.connect(self._assistant_started)
        self._runtime.exit_requested.connect(self.close)
        self._runtime.run_failed.connect(self._show_error)
        self._runtime.run_finished.connect(self._run_finished)
        self._runtime.traces_updated.connect(self._trace_page.refresh_traces)
        self._runtime.trace_updated.connect(self._trace_page.refresh_trace)
        self._runtime.scheduled_tasks_updated.connect(self._scheduled_page.set_jobs)
        self._runtime.scheduled_task_result.connect(self._on_page_result)
        self._runtime.memories_updated.connect(self._memory_page.set_data)
        self._runtime.memory_result.connect(self._on_page_result)
        self._runtime.memory_result.connect(self._memory_page.set_result)
        self._memory_page.add_requested.connect(self._runtime.add_memory)
        self._memory_page.update_requested.connect(self._runtime.update_memory)
        self._memory_page.delete_requested.connect(self._runtime.delete_memory)
        self._memory_page.procedural_save_requested.connect(self._runtime.save_procedural)

    def _runtime_ready(self, workspace: str) -> None:
        self._ready = True
        self._workspace.setText(workspace)
        self._workspace.setToolTip(workspace)
        self._update_status("就绪")
        self._send.setEnabled(True)
        self._send_options.setEnabled(True)
        self._mode_button.setEnabled(True)
        self._agent_button.setEnabled(True)
        self._tool_page.set_ready()
        self._new_chat.setEnabled(True)
        self._memory_page.setEnabled(True)
        self._scheduled_page.setEnabled(True)
        self._runtime.list_memories()
        if self._pages.currentWidget() is self._scheduled_page:
            self._runtime.list_scheduled_tasks()
        self._input.setFocus()

    def _send_prompt(self) -> None:
        prompt = self._input.toPlainText().strip()
        if not prompt:
            return
        if prompt in {"/help", "/help full"}:
            self._input.clear()
            self._show_commands()
            return
        if self._send_mode != "replace":
            self._send_control(self._send_mode)
            return
        self._input.clear()
        if not prompt.startswith("/"):
            self._messages.append(("user", prompt))
            self._append_user_message(prompt)
            self._welcome.hide()
        self._runtime.submit_message(prompt, "replace")

    def _submit_command(self, command: str) -> None:
        if command in {"/help", "/help full"}:
            self._show_commands()
            return
        if not self._ready:
            return
        self._show_chat()
        self._runtime.submit_input(command)

    def _show_commands(self) -> None:
        self._pages.setCurrentWidget(self._commands_page)
        self._resources.clearSelection()
        self._commands_page.search.setFocus()

    def _insert_command(self, command: str) -> None:
        cursor = self._input.textCursor()
        cursor.insertText(command + " ")
        self._input.setTextCursor(cursor)
        self._show_chat()
        self._input.setFocus()

    def _show_chat(self) -> None:
        self._pages.setCurrentWidget(self._chat_page)
        self._resources.clearSelection()

    def _show_scheduled_tasks(self) -> None:
        self._pages.setCurrentWidget(self._scheduled_page)
        self._resources.clearSelection()
        if self._ready:
            self._runtime.list_scheduled_tasks()

    def _show_trace(self) -> None:
        self._pages.setCurrentWidget(self._trace_page)
        self._resources.clearSelection()
        self._trace_page.refresh_traces(
            self._runtime.trace_store.list_traces(session_id=self._session_id)
            if self._session_id is not None
            else [],
            session_id=self._session_id,
        )

    def _toggle_trace(self, checked: bool) -> None:
        if checked:
            self._show_trace()
        else:
            self._show_chat()

    def _sync_trace_button(self, _index: int) -> None:
        active = self._pages.currentWidget() is self._trace_page
        self._trace_button.setChecked(active)
        self._trace_button.setAccessibleName(
            "关闭执行 Trace" if active else "打开执行 Trace"
        )
        self._trace_button.setToolTip(
            "观测 · 返回对话" if active
            else "观测 · 查看当前与历史 Trace"
        )

    def _toggle_tools(self, expanded: bool) -> None:
        self._resources.setVisible(expanded)
        self._tools_button.setToolTip("收起扩展能力分类" if expanded else "展开扩展能力分类")

    def _open_tools(self, kind: ToolKind) -> None:
        self._tools_button.setChecked(True)
        self._tool_page.show_category(kind)
        self._pages.setCurrentWidget(self._tool_page)
        for index in range(self._resources.topLevelItemCount()):
            item = self._resources.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == kind:
                self._resources.setCurrentItem(item)

    def _send_control(self, command: str) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        if not self._run_active:
            self._show_toast("引导和追加需要正在运行的任务，输入已保留。", "warning")
            return
        self._runtime.submit_control(command, text)
        self._input.clear()

    def _append_user_message(self, text: str) -> None:
        self._transcript.add_message("user", text)

    def _assistant_started(self) -> None:
        if self._assistant_bubble is not None:
            self._assistant_bubble.set_end_time(time.time())
        self._active_assistant = True
        self._assistant_index = len(self._messages)
        self._messages.append(("assistant", ""))
        self._welcome.hide()
        self._assistant_bubble = self._transcript.add_message("assistant", "")
        self._assistant_bubble.message_index = self._assistant_index

    def _append_delta(self, text: str) -> None:
        if (
            not self._active_assistant
            or self._assistant_index is None
            or self._assistant_bubble is None
        ):
            return
        role, previous = self._messages[self._assistant_index]
        self._messages[self._assistant_index] = (role, previous + text)
        self._assistant_bubble.set_text(previous + text)

    def _set_session_history(self, messages: tuple[tuple[str, str], ...]) -> None:
        timestamps = self._history_timestamps
        self._history_timestamps = ()
        self._messages = list(messages)
        self._active_assistant = False
        self._assistant_index = None
        self._assistant_bubble = None
        self._update_session_title()
        self._transcript.begin_refresh()
        self._transcript.clear()
        if not messages:
            self._transcript.end_refresh()
            self._welcome.show()
            return
        self._welcome.hide()
        last_assistant: dict[int, int] = {}
        last_activity: dict[int, int] = {}
        turn = 0
        for index, (role, _text) in enumerate(messages):
            if role == "user":
                turn += 1
            elif role == "assistant":
                last_assistant[turn] = index
            elif role in {"activity", "process"}:
                last_activity[turn] = index
        turn = 0
        retry_ordinal = 0
        for index, (role, text) in enumerate(messages):
            if role == "user":
                turn += 1
            elif role == "assistant" and (
                index != last_assistant.get(turn) or index < last_activity.get(turn, -1)
            ):
                role = "process"
            timestamp = timestamps[index] if index < len(timestamps) else None
            bubble = self._transcript.add_message(role, text, timestamp=timestamp)
            if role == "assistant":
                bubble.message_index = index
                bubble.retry_index = retry_ordinal
                retry_ordinal += 1
        self._transcript.end_refresh()
        self._apply_persisted_feedback()

    def _set_session_timing(self, timestamps: tuple[float, ...]) -> None:
        self._history_timestamps = timestamps

    def _show_command_result(self, message: str, display_kind: str) -> None:
        if not message:
            return
        self._transcript.add_message("notice", message)
        self._welcome.hide()

    def _show_toast(self, message: str, kind: str = "info") -> None:
        self._toasts.show_message(message, kind)

    def _on_page_result(self, message: str, display_kind: str) -> None:
        # 记忆/定时任务/配置等子页面已在页面内内联展示结果，这里统一用 toast 做全局提示。
        self._show_toast(message, display_kind)

    def _set_command_completions(
        self,
        items: tuple[tuple[str, str], ...],
    ) -> None:
        self._input.set_commands(items)
        self._commands_page.set_commands(items)

    def _set_session_id(self, session_id: str) -> None:
        self._show_chat()
        if session_id != self._session_id:
            self._transcript.reset_expansion()
        self._session_id = session_id
        self._title.setEnabled(True)
        self._update_session_title()
        self._session_caption.setText(f"SessionID: {session_id}")
        self._session_caption.setToolTip(session_id)
        self._select_active_session()
        self._trace_page.refresh_traces(
            self._runtime.trace_store.list_traces(session_id=session_id),
            session_id=session_id,
        )

    def _set_sessions(
        self,
        sessions: tuple[tuple[str, str | None, float | None], ...],
    ) -> None:
        self._all_sessions = sessions
        self._session_names = {
            session_id: name for session_id, name, _updated_at in sessions if name
        }
        self._render_sessions()
        self._update_session_title()
        self._select_active_session()

    def _filter_sessions(self, _text: str | None = None) -> None:
        self._render_sessions()
        self._select_active_session()

    def _render_sessions(self) -> None:
        query = self._session_search.text().strip().casefold()
        self._sessions.clear()
        for session_id, name, updated_at in getattr(self, "_all_sessions", ()):
            title = name or f"对话 {session_id[:8]}"
            if query and query not in f"{title} {session_id}".casefold():
                continue
            if updated_at is not None:
                date = datetime.fromtimestamp(updated_at).strftime("%y-%m-%d %H:%M")
            else:
                date = ""
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, session_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, name or "")
            item.setData(Qt.ItemDataRole.UserRole + 2, date)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{title} {date}".strip())
            item.setToolTip(f"{title}\n{date}\n{session_id}")
            self._sessions.addItem(item)

    def _update_session_title(self) -> None:
        name = self._session_names.get(self._session_id or "")
        self._title.setText(name or ("新对话" if not self._messages else "对话"))

    def _rename_current_session(self) -> None:
        if self._session_id is not None:
            self._prompt_session_name(
                self._session_id, self._session_names.get(self._session_id, ""),
            )

    def _select_active_session(self) -> None:
        for index in range(self._sessions.count()):
            item = self._sessions.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == self._session_id:
                self._sessions.setCurrentItem(item)
                return
        self._sessions.clearSelection()

    def _select_session(self, item: QListWidgetItem) -> None:
        self._show_chat()
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id and session_id != self._session_id:
            self._runtime.session_action("open", session_id)

    def _session_menu(self, position: QPoint) -> None:
        item = self._sessions.itemAt(position)
        if item is None:
            return
        menu = self._build_session_menu(item)
        menu.exec(self._sessions.viewport().mapToGlobal(position))

    def _build_session_menu(self, item: QListWidgetItem) -> QMenu:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self._sessions)
        menu.setStyleSheet(
            "QMenu { background: white; color: #202124; border: 1px solid #dfe3ea;"
            "border-radius: 8px; padding: 6px; font-size: 12px; }"
            "QMenu::item { padding: 9px 14px; border-radius: 6px; }"
            "QMenu::item:selected { background: #edf2fa; color: #202124; }"
            "QMenu::item:disabled { color: #9299a3; }"
            "QMenu::separator { height: 1px; background: #e1e4e8; margin: 5px 8px; }"
        )
        for label, action in (
            ("打开会话", "open"),
            ("分支并打开", "fork"),
            ("完整克隆并打开", "clone"),
            ("导出会话…", "export"),
        ):
            entry = menu.addAction(label)
            entry.triggered.connect(
                lambda _checked=False, value=action: self._session_action(value, session_id)
            )
        rename = menu.addAction("修改名称")
        name = item.data(Qt.ItemDataRole.UserRole + 1)
        rename.triggered.connect(lambda: self._prompt_session_name(session_id, name))
        copy = menu.addAction("复制 Session ID")
        copy.triggered.connect(lambda: QApplication.clipboard().setText(session_id))
        menu.addSeparator()
        delete = menu.addAction("删除会话…")
        delete.triggered.connect(lambda: self._session_action("delete", session_id))
        return menu

    def _session_action(self, action: str, session_id: str) -> None:
        path = None
        if action == "delete":
            if not confirm_delete(self, session_id):
                return
        elif action == "export":
            path = export_session(self, session_id)
            if not path:
                return
        self._runtime.session_action(action, session_id, path)

    def _rename_session(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        self._prompt_session_name(session_id, item.data(Qt.ItemDataRole.UserRole + 1))

    def _prompt_session_name(self, session_id: str, current_name: str) -> None:
        name = rename_session(self, current_name)
        if name is None:
            return
        if not name.strip():
            self._show_toast("会话名称不能为空。", "warning")
            return
        self._runtime.rename_session(session_id, name.strip())

    def _set_resources(self, inventory: ToolInventory) -> None:
        self._inventory = inventory
        label = {"normal": "普通", "plan": "规划", "auto": "自动"}[inventory.permission_mode]
        self._mode_button.setText(f"mode · {label}")
        for mode, action in self._mode_actions.items():
            action.setChecked(mode == inventory.permission_mode)
        self._mode_button.setEnabled(self._send.isEnabled() and not inventory.busy)
        for index, (kind, _title) in enumerate(TOOL_CATEGORIES):
            count = str(sum(entry.kind == kind for entry in inventory.entries))
            item = self._resources.topLevelItem(index)
            item.setData(1, Qt.ItemDataRole.UserRole, count)
            self._resource_counts[kind].setText(count)
            item.setToolTip(
                0,
                "MCP 服务状态由 Sec Client 管理"
                if kind == "mcp"
                else f"共 {count} 项（含已停用）",
            )
        self._tool_page.set_inventory(inventory)

    def _update_status(self, status: str) -> None:
        self._status.setText(status)
        if status == "就绪":
            color = "#22a06b"
        elif status == "已完成":
            color = "#3b82f6"
        elif status == "正在运行" or status.startswith("工具运行中"):
            color = "#e8a317"
        elif status.startswith("等待工具审批"):
            color = "#8b5cf6"
        elif status in {"运行失败", "发生错误"}:
            color = "#dc3545"
        else:
            color = "#9ca3af"
        self._status_dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        self._status_dot.setToolTip(status)
        self._run_active = status == "正在运行" or status.startswith(("工具运行中", "等待工具审批"))
        self._tool_page.set_busy(self._run_active)
        self._mode_button.setEnabled(self._send.isEnabled() and not self._run_active)
        self._update_send_button()

    def _run_finished(self) -> None:
        if self._assistant_bubble is not None:
            self._assistant_bubble.set_end_time(time.time())
        self._active_assistant = False
        self._assistant_index = None
        self._assistant_bubble = None
        self._send.setEnabled(True)
        self._run_active = False
        self._update_send_button()
        self._mode_button.setEnabled(True)
        self._tool_page.set_busy(False)
        self._input.setFocus()

    def _message_action(self, action: str, bubble: MessageBubble) -> None:
        if action == "copy":
            if bubble.text:
                QApplication.clipboard().setText(bubble.text)
                self._show_toast("已复制到剪贴板。", "success")
        elif action == "fork":
            if self._session_id:
                self._runtime.fork_session(
                    self._session_id,
                    self._session_names.get(self._session_id, ""),
                )
        elif action == "share":
            if self._session_id:
                self._session_action("export", self._session_id)
        elif action == "retry":
            self._retry_message(bubble)
        elif action == "mark":
            self._persist_mark(bubble)
        elif action in {"like", "dislike"}:
            self._persist_rating(bubble)

    def _feedback_marks_key(self, session_id: str) -> str:
        return f"feedback/{session_id}/marks"

    def _feedback_ratings_key(self, session_id: str) -> str:
        return f"feedback/{session_id}/ratings"

    def _load_marks(self, session_id: str) -> set[int]:
        raw = self._settings.value(self._feedback_marks_key(session_id), [])
        return {int(item) for item in (raw or [])}

    def _save_marks(self, session_id: str, marks: set[int]) -> None:
        self._settings.setValue(self._feedback_marks_key(session_id), sorted(marks))

    def _load_ratings(self, session_id: str) -> dict[int, str]:
        raw = self._settings.value(self._feedback_ratings_key(session_id), {})
        return {int(key): value for key, value in (raw or {}).items()}

    def _save_ratings(self, session_id: str, ratings: dict[int, str]) -> None:
        self._settings.setValue(
            self._feedback_ratings_key(session_id),
            {str(key): value for key, value in ratings.items()},
        )

    def _persist_mark(self, bubble: MessageBubble) -> None:
        session_id = self._session_id
        index = bubble.message_index
        if session_id is None or index is None:
            return
        marks = self._load_marks(session_id)
        marked = bubble.action_checked("mark")
        if marked:
            marks.add(index)
        else:
            marks.discard(index)
        self._save_marks(session_id, marks)
        self._transcript.set_bubble_marked(bubble, marked)
        self._show_toast(
            "已标记该回复。" if marked else "已取消标记。", "success",
        )

    def _persist_rating(self, bubble: MessageBubble) -> None:
        session_id = self._session_id
        index = bubble.message_index
        if session_id is None or index is None:
            return
        ratings = self._load_ratings(session_id)
        if bubble.action_checked("like"):
            ratings[index] = "like"
        elif bubble.action_checked("dislike"):
            ratings[index] = "dislike"
        else:
            ratings.pop(index, None)
        self._save_ratings(session_id, ratings)

    def _apply_persisted_feedback(self) -> None:
        session_id = self._session_id
        if not session_id:
            return
        marks = self._load_marks(session_id)
        ratings = self._load_ratings(session_id)
        for bubble in self._transcript.bubbles:
            if bubble.role != "assistant" or bubble.message_index is None:
                continue
            index = bubble.message_index
            marked = index in marks
            bubble.set_action_checked("mark", marked)
            bubble.set_action_checked("like", ratings.get(index) == "like")
            bubble.set_action_checked("dislike", ratings.get(index) == "dislike")
            self._transcript.set_bubble_marked(bubble, marked)

    def _retry_message(self, bubble: MessageBubble) -> None:
        if self._run_active:
            self._show_toast("请等待当前任务完成后再重试。", "warning")
            return
        retry_index = bubble.retry_index
        if retry_index is None:
            return
        self._welcome.hide()
        self._runtime.retry_message(retry_index)

    def _approve_tool(self, tool_call_id: str, tool_name: str, arguments: str) -> None:
        self._show_chat()
        if self._ball.isVisible():
            self.show_focus_mode()
        self._welcome.hide()
        self._transcript.add_approval(tool_call_id, tool_name, arguments)

    def _handle_extension_ui(self, request_id: str, request: dict) -> None:
        self._show_chat()
        if self._ball.isVisible():
            self.show_focus_mode()
        method = request.get("method")
        title = request.get("title", "扩展")
        cancelled: dict = {
            "type": "extension_ui_response",
            "id": request_id,
            "cancelled": True,
        }
        if method == "select":
            options = request.get("options", [])
            value, ok = QInputDialog.getItem(
                self, title, "请选择：", options, 0, False
            )
            response = (
                {"type": "extension_ui_response", "id": request_id, "value": value}
                if ok
                else cancelled
            )
        elif method == "confirm":
            confirmed = (
                QMessageBox.question(
                    self,
                    title,
                    request.get("message", ""),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.Yes
            )
            response = {
                "type": "extension_ui_response",
                "id": request_id,
                "confirmed": confirmed,
            }
        elif method == "input":
            value, ok = QInputDialog.getText(
                self, title, request.get("placeholder", "")
            )
            response = (
                {"type": "extension_ui_response", "id": request_id, "value": value}
                if ok
                else cancelled
            )
        elif method == "editor":
            value, ok = QInputDialog.getMultiLineText(
                self, title, request.get("prefill", "")
            )
            response = (
                {"type": "extension_ui_response", "id": request_id, "value": value}
                if ok
                else cancelled
            )
        else:
            response = cancelled
        self._runtime.resolve_ui(request_id, response)

    def _show_error(self, message: str) -> None:
        if self._ball.isVisible():
            self.show_focus_mode()
        self._update_status("发生错误")
        show_error(self, message)

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_toasts"):
            self._toasts.setGeometry(self.rect())
            self._toasts.raise_()
