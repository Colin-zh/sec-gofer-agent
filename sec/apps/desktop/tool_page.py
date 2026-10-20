"""Resource previews and controls backed by the desktop runtime."""

from dataclasses import dataclass
from typing import Literal

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QStyle, QVBoxLayout, QWidget,
)

ToolKind = Literal["agent", "prompt", "tool", "skill", "extension", "mcp"]
TOOL_CATEGORIES: tuple[tuple[ToolKind, str], ...] = (
    ("agent", "智能体"), ("prompt", "提示词库"), ("tool", "工具"),
    ("skill", "技能"), ("mcp", "MCP"), ("extension", "插件"),
)


@dataclass(frozen=True)
class ToolEntry:
    kind: ToolKind
    name: str
    description: str
    content: str
    enabled: bool
    execution_mode: Literal["in_process", "worker"] = "in_process"
    builtin: bool = False
    origin_kind: str | None = None
    origin_name: str | None = None


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
    agent_requested = Signal(str)
    reload_requested = Signal()
    permission_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._inventory = ToolInventory()
        self._kind: ToolKind = "prompt"
        self._pending = False
        self._ready = False
        self._updating = False
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
        actions = QHBoxLayout()
        actions.addStretch()
        self._reload = QPushButton("重新加载")
        self._reload.setToolTip("重新发现工作区资源、Agent 与 MCP 服务；运行中的 Trace 保持原能力快照")
        self._reload.clicked.connect(self.reload_requested)
        actions.addWidget(self._reload)
        layout.addLayout(actions)
        self._explanation = QLabel()
        self._explanation.setWordWrap(True)
        layout.addWidget(self._explanation)
        splitter = QSplitter()
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.setMinimumWidth(160)
        self._list.setMaximumWidth(240)
        self._list.currentItemChanged.connect(self._select)
        splitter.addWidget(self._list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self._enabled = QCheckBox("启用此资源")
        self._enabled.clicked.connect(self._toggle)
        detail_layout.addWidget(self._enabled)
        self._execution_mode = QComboBox()
        self._execution_mode.setAccessibleName("插件执行方式")
        self._execution_mode.addItem("独立 worker · 支持工具代码热更新", "worker")
        self._execution_mode.addItem("进程内 · 支持完整扩展贡献", "in_process")
        self._execution_mode.currentIndexChanged.connect(self._update_controls)
        detail_layout.addWidget(self._execution_mode)
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
        warning = QLabel(
            "启用不等于免审批。插件可执行本机代码，仅启用可信来源。"
            "进程内扩展和全局权限只能在空闲时修改；worker 工具与资源可在安全边界刷新。"
        )
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
            "MCP 服务状态与启用配置由 Sec Client 管理；启用的工具仍须逐项审批。"
            if kind == "mcp" else
            "选择当前 Agent；运行期间的切换会排队，并在下一次 Trace 生效。"
            if kind == "agent" else
            "内置工具固定可用、不可停用；自定义工具来自扩展与 MCP，"
            "通过启停其来源来增减。"
            if kind == "tool" else
            "提示词库只在显式调用时展开；技能目录自动提供给当前 Agent，"
            "匹配的 required 技能会在模型请求前加载，其余按需使用。"
        )
        self.set_inventory(self._inventory)

    def set_inventory(self, inventory: ToolInventory) -> None:
        current = self._list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole).name if current else None
        self._inventory = inventory
        self._pending = False
        self._updating = True
        self._list.clear()
        for entry in inventory.entries:
            if entry.kind != self._kind:
                continue
            if entry.kind == "agent":
                state = (
                    "下一轮生效"
                    if "下一次 Trace" in entry.description
                    else "当前"
                    if entry.enabled
                    else "可选"
                )
            elif entry.kind == "tool" and entry.builtin:
                state = "内置"
            else:
                state = "已启用" if entry.enabled else "已停用"
            item = QListWidgetItem(f"{entry.name}  {state}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry.description)
            self._list.addItem(item)
            if entry.name == selected:
                self._list.setCurrentItem(item)
        if self._list.currentItem() is None and self._list.count():
            self._list.setCurrentRow(0)
        if not self._list.count():
            self._preview.setPlainText(
                "尚无 Agent。"
                if self._kind == "agent"
                else "暂无已发现的 MCP 服务。"
                if self._kind == "mcp"
                else "暂无可用资源"
            )
            self._enabled.setChecked(False)
        self._mode.setCurrentIndex(self._mode.findData(inventory.permission_mode))
        self._updating = False
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
            self._execution_mode.setCurrentIndex(
                self._execution_mode.findData(entry.execution_mode),
            )
            if entry.kind == "agent":
                self._enabled.setChecked(False)
                if not self._updating and self._ready:
                    self._pending = True
                    self.agent_requested.emit(entry.name)
        self._update_controls()

    def _update_controls(self) -> None:
        editable = self._ready and not self._pending
        selected = self._list.currentItem()
        entry = None if selected is None else selected.data(Qt.ItemDataRole.UserRole)
        is_builtin_tool = self._kind == "tool" and entry is not None and entry.builtin
        origin_kind = entry.origin_kind if self._kind == "tool" and entry is not None else None
        resource_editable = (
            editable
            and selected is not None
            and entry.kind != "agent"
            and not is_builtin_tool
            and (
                not self._inventory.busy
                or entry.kind in {"skill", "prompt", "mcp"}
                or origin_kind in {"mcp", "worker"}
                or entry.kind == "extension" and self._execution_mode.currentData() == "worker"
            )
        )
        self._enabled.setVisible(self._kind != "agent" and not is_builtin_tool)
        self._enabled.setEnabled(resource_editable)
        self._execution_mode.setVisible(self._kind == "extension")
        self._execution_mode.setEnabled(editable and entry is not None and not entry.enabled)
        self._reload.setEnabled(self._ready and not self._pending)
        self._mode.setEnabled(editable and not self._inventory.busy)

    def _toggle(self, checked: bool) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        entry: ToolEntry = item.data(Qt.ItemDataRole.UserRole)
        if entry.kind == "agent" or (entry.kind == "tool" and entry.builtin):
            self._enabled.setChecked(False)
            return
        self._enabled.setChecked(entry.enabled)
        self._pending = True
        self._update_controls()
        if entry.kind == "tool":
            self.activation_requested.emit(entry.origin_kind, entry.origin_name, checked)
            return
        kind = (
            "worker" if entry.kind == "extension"
            and self._execution_mode.currentData() == "worker" else entry.kind
        )
        self.activation_requested.emit(kind, entry.name, checked)

    def _change_mode(self, index: int) -> None:
        mode = self._mode.itemData(index)
        self._mode.setCurrentIndex(self._mode.findData(self._inventory.permission_mode))
        self._pending = True
        self._update_controls()
        self.permission_requested.emit(mode)
