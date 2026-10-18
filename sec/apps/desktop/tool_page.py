"""Resource previews and controls backed by the desktop runtime."""

from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QStyle, QVBoxLayout, QWidget,
)

ToolKind = Literal["prompt", "skill", "extension", "mcp"]
TOOL_CATEGORIES: tuple[tuple[ToolKind, str], ...] = (
    ("prompt", "智能体"), ("skill", "技能"), ("extension", "插件"), ("mcp", "MCP"),
)


@dataclass(frozen=True)
class ToolEntry:
    kind: ToolKind
    name: str
    description: str
    content: str
    enabled: bool


@dataclass(frozen=True)
class ToolInventory:
    entries: tuple[ToolEntry, ...] = ()
    permission_mode: str = "normal"
    busy: bool = False


class PermissionModeComboBox(QComboBox):
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#68707c" if self.isEnabled() else "#a5abb4"), 1.5))
        center = QPointF(self.width() - 16, self.height() / 2)
        painter.drawPolyline(QPolygonF([
            center + QPointF(-4, -2), center + QPointF(0, 2), center + QPointF(4, -2),
        ]))


class ToolPage(QWidget):
    back_requested = Signal()
    activation_requested = Signal(str, str, bool)
    permission_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._inventory = ToolInventory()
        self._kind: ToolKind = "prompt"
        self._pending = False
        self._ready = False
        self.setStyleSheet(
            "QLabel { color: #202124; }"
            "QLabel#subtle { color: #8a9099; font-size: 11px; }"
            "QListWidget { background: #f0f2f5; border: 0; border-radius: 10px; padding: 6px; }"
            "QListWidget::item { padding: 10px 8px; border-radius: 6px; color: #202124; }"
            "QListWidget::item:selected { background: #dfe7f2; color: #202124; }"
            "QPlainTextEdit { background: white; color: #202124; border: 1px solid #e1e4e8;"
            "border-radius: 10px; padding: 12px; }"
            "QComboBox { padding: 7px 32px 7px 12px; background: white; color: #202124;"
            "border: 1px solid #dfe3ea; border-radius: 8px; font-size: 12px;"
            "min-height: 18px; combobox-popup: 0; }"
            "QComboBox:hover { background: #f0f2f5; border-color: #cbd2dc; }"
            "QComboBox:focus, QComboBox:on { border-color: #a8b8cf; background: #edf2fa; }"
            "QComboBox:disabled { background: #f0f2f5; color: #9299a3; border-color: #e1e4e8; }"
            "QComboBox::drop-down { border: 0; width: 28px; background: transparent; }"
            "QComboBox::down-arrow { image: none; }"
        )
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        back = QPushButton("返回对话")
        back.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        back.clicked.connect(self.back_requested)
        heading.addWidget(back)
        heading.addStretch()
        self._title = QLabel()
        self._title.setObjectName("sessionTitle")
        heading.addWidget(self._title)
        layout.addLayout(heading)
        self._explanation = QLabel()
        self._explanation.setWordWrap(True)
        layout.addWidget(self._explanation)
        splitter = QSplitter()
        self._list = QListWidget()
        self._list.setMinimumWidth(160)
        self._list.setMaximumWidth(240)
        self._list.currentItemChanged.connect(self._select)
        splitter.addWidget(self._list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self._enabled = QCheckBox("启用此资源")
        self._enabled.clicked.connect(self._toggle)
        detail_layout.addWidget(self._enabled)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("选择左侧资源查看内容")
        detail_layout.addWidget(self._preview)
        splitter.addWidget(detail)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        permissions = QHBoxLayout()
        permissions.addWidget(QLabel("工具执行权限（全局）"))
        self._mode = PermissionModeComboBox()
        self._mode.setAccessibleName("工具执行权限")
        self._mode.setView(QListView(self._mode))
        self._mode.view().setSpacing(2)
        self._mode.view().setStyleSheet(
            "QListView { background: white; color: #202124; border: 1px solid #dfe3ea;"
            "border-radius: 8px; padding: 6px; outline: 0; font-size: 12px; }"
            "QListView::item { padding: 9px 12px; border-radius: 6px; }"
            "QListView::item:hover, QListView::item:selected { background: #edf2fa; color: #202124; }"
        )
        for label, mode in (
            ("普通 · 按策略审批", "normal"),
            ("规划 · 只读", "plan"),
            ("自动 · 减少审批", "auto"),
        ):
            self._mode.addItem(label, mode)
            self._mode.setItemData(
                self._mode.count() - 1, QSize(220, 36), Qt.ItemDataRole.SizeHintRole,
            )
        self._mode.setMaxVisibleItems(self._mode.count())
        self._mode.activated.connect(self._change_mode)
        permissions.addWidget(self._mode)
        permissions.addStretch()
        layout.addLayout(permissions)
        warning = QLabel("启用不等于免审批。插件可执行本机代码，仅启用可信来源；运行任务期间不可修改配置。")
        warning.setWordWrap(True)
        warning.setObjectName("subtle")
        layout.addWidget(warning)
        self._feedback = QLabel()
        self._feedback.setWordWrap(True)
        self._feedback.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._feedback)
        self.show_category("prompt")

    def show_category(self, kind: ToolKind) -> None:
        self._kind = kind
        self._title.setText(dict(TOOL_CATEGORIES)[kind])
        self._feedback.clear()
        self._explanation.setText(
            "MCP 尚未接入连接、工具发现与调用；此处仅保留入口，暂无可激活服务。"
            if kind == "mcp" else
            "浏览资源内容并配置激活状态。智能体当前沿用提示词模板能力，与技能通过对应快捷指令调用，不自动注入每轮对话。"
        )
        self.set_inventory(self._inventory)

    def set_inventory(self, inventory: ToolInventory) -> None:
        current = self._list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole).name if current else None
        self._inventory = inventory
        self._pending = False
        self._list.clear()
        for entry in inventory.entries:
            if entry.kind != self._kind:
                continue
            item = QListWidgetItem(f"{entry.name}  {'已启用' if entry.enabled else '已停用'}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry.description)
            self._list.addItem(item)
            if entry.name == selected:
                self._list.setCurrentItem(item)
        if self._list.currentItem() is None and self._list.count():
            self._list.setCurrentRow(0)
        if not self._list.count():
            self._preview.setPlainText("暂无可用资源" if self._kind != "mcp" else "MCP 功能预留")
            self._enabled.setChecked(False)
        self._mode.setCurrentIndex(self._mode.findData(inventory.permission_mode))
        self._update_controls()

    def set_ready(self) -> None:
        self._ready = True
        self._update_controls()

    def set_busy(self, busy: bool) -> None:
        self._inventory = ToolInventory(
            self._inventory.entries, self._inventory.permission_mode, busy,
        )
        self._update_controls()

    def show_result(self, message: str, kind: str) -> None:
        self._feedback.setStyleSheet("color: #b42318;" if kind == "error" else "color: #25734c;")
        self._feedback.setText(message)

    def _select(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is not None:
            entry: ToolEntry = current.data(Qt.ItemDataRole.UserRole)
            self._preview.setPlainText(f"{entry.name}\n{entry.description}\n\n{entry.content}")
            self._enabled.setChecked(entry.enabled)
        self._update_controls()

    def _update_controls(self) -> None:
        editable = self._ready and not self._inventory.busy and not self._pending
        self._enabled.setEnabled(editable and self._list.currentItem() is not None)
        self._mode.setEnabled(editable)

    def _toggle(self, checked: bool) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        entry: ToolEntry = item.data(Qt.ItemDataRole.UserRole)
        self._enabled.setChecked(entry.enabled)
        self._pending = True
        self._update_controls()
        self.activation_requested.emit(entry.kind, entry.name, checked)

    def _change_mode(self, index: int) -> None:
        mode = self._mode.itemData(index)
        self._mode.setCurrentIndex(self._mode.findData(self._inventory.permission_mode))
        self._pending = True
        self._update_controls()
        self.permission_requested.emit(mode)
