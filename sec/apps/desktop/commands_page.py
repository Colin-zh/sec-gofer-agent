"""Searchable command reference, separate from conversation history."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QStyle, QVBoxLayout, QWidget,
)

from sec.apps.desktop.appearance import control_icon


class CommandsPage(QWidget):
    back_requested = Signal()
    command_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._commands: tuple[tuple[str, str], ...] = ()
        self.setStyleSheet(
            "QLabel { color: #202124; background: transparent; }"
            "QLabel#subtle { color: #8a9099; font-size: 11px; }"
            "QLineEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QFrame#commandCard { background: white; border: 1px solid #e1e4e8;"
            "border-radius: 10px; }"
            "QScrollArea, QWidget#commandContent { background: transparent; border: 0; }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        header = QHBoxLayout()
        back = QPushButton("返回对话")
        back.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addStretch()
        title = QLabel("快捷指令")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(title)
        layout.addLayout(header)
        note = QLabel("浏览当前可用指令。点击「填入输入框」后可补充参数，再手动发送，不会直接执行。")
        note.setWordWrap(True)
        note.setObjectName("subtle")
        layout.addWidget(note)
        shortcuts = QLabel("发送：Command/Ctrl+Enter    引导：Command/Ctrl+Shift+Enter    追加：Command/Ctrl+Alt+Enter")
        shortcuts.setWordWrap(True)
        shortcuts.setObjectName("subtle")
        layout.addWidget(shortcuts)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索命令或说明")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render)
        layout.addWidget(self.search)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content.setObjectName("commandContent")
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(0, 0, 8, 0)
        self._cards.setSpacing(10)
        scroll.setWidget(self._content)
        layout.addWidget(scroll, 1)
        self._render()

    def set_commands(self, commands: tuple[tuple[str, str], ...]) -> None:
        self._commands = commands
        self._render()

    def _render(self) -> None:
        while self._cards.count():
            item = self._cards.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        query = self.search.text().strip().casefold()
        groups: dict[str, list[tuple[str, str]]] = {}
        session = {"new", "resume", "name", "session", "tree", "fork", "clone", "export", "import", "copy"}
        control = {"stop", "steer", "followup", "follow", "follow-up", "mode", "trust"}
        for name, description in self._commands:
            if query and query not in f"{name} {description}".casefold():
                continue
            bare = name.lstrip("/")
            category = (
                "技能与提示词" if ":" in bare else "会话管理" if bare in session
                else "运行与权限" if bare in control else "其他可用指令"
            )
            groups.setdefault(category, []).append((name, description))
        for category, commands in groups.items():
            label = QLabel(f"{category} · {len(commands)}")
            label.setObjectName("subtle")
            self._cards.addWidget(label)
            for name, description in commands:
                card = QFrame()
                card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                card.setObjectName("commandCard")
                body = QVBoxLayout(card)
                body.setContentsMargins(14, 10, 14, 10)
                row = QHBoxLayout()
                command = QLabel(name)
                command.setTextFormat(Qt.TextFormat.PlainText)
                command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                command.setStyleSheet("font-family: monospace; font-weight: 600;")
                row.addWidget(command, 1)
                insert = QPushButton()
                insert.setIcon(control_icon("apply"))
                insert.setIconSize(QSize(16, 16))
                insert.setFixedSize(32, 32)
                insert.setStyleSheet("QPushButton { padding: 0; text-align: center; }")
                insert.setToolTip("填入输入框")
                insert.setAccessibleName("填入输入框")
                insert.clicked.connect(
                    lambda _checked=False, value=name: self.command_selected.emit(value)
                )
                row.addWidget(insert)
                body.addLayout(row)
                detail = QLabel(description)
                detail.setTextFormat(Qt.TextFormat.PlainText)
                detail.setWordWrap(True)
                body.addWidget(detail)
                self._cards.addWidget(card)
        if not groups:
            empty = QLabel("没有匹配的指令" if self._commands else "指令正在加载…")
            empty.setObjectName("subtle")
            self._cards.addWidget(empty)
        self._cards.addStretch()
