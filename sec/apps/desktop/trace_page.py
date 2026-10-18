"""Trace browser for persisted desktop Runs."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sec.apps.desktop.appearance import control_icon
from sec.apps.desktop.tool_page import PermissionModeComboBox
from sec.apps.desktop.trace_store import TraceEventRecord, TraceStore, format_wall_time


_MISSING = "未记录"

_COMBO_STYLE = (
    "QComboBox { padding: 7px 32px 7px 12px; background: white; color: #202124;"
    "border: 1px solid #dfe3ea; border-radius: 8px; font-size: 12px;"
    "min-height: 18px; combobox-popup: 0; }"
    "QComboBox:hover { background: #f0f2f5; border-color: #cbd2dc; }"
    "QComboBox:focus, QComboBox:on { border-color: #a8b8cf; background: #edf2fa; }"
    "QComboBox:disabled { background: #f0f2f5; color: #9299a3; border-color: #e1e4e8; }"
    "QComboBox::drop-down { border: 0; width: 28px; background: transparent; }"
    "QComboBox::down-arrow { image: none; }"
)


def _trace_combo() -> QComboBox:
    combo = PermissionModeComboBox()
    combo.setStyleSheet(_COMBO_STYLE)
    combo.setView(QListView(combo))
    combo.view().setSpacing(2)
    combo.view().setStyleSheet(
        "QListView { background: white; color: #202124; border: 1px solid #dfe3ea;"
        "border-radius: 8px; padding: 6px; outline: 0; font-size: 12px; }"
        "QListView::item { padding: 9px 12px; border-radius: 6px; }"
        "QListView::item:hover, QListView::item:selected"
        " { background: #edf2fa; color: #202124; }"
    )
    return combo


def _pretty(value: Any) -> str:
    if value is None:
        return _MISSING
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _duration(value: float | None) -> str:
    if value is None:
        return _MISSING
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.2f} s"


def _event_data(event: TraceEventRecord) -> dict[str, Any]:
    return {
        "type": event.event_type,
        "occurred_at": format_wall_time(event.occurred_at),
        "elapsed_ms": event.elapsed_ms,
        "payload": event.payload,
    }


_STATUS_COLORS = {
    "running": "#a16207",
    "completed": "#15803d",
    "approved": "#15803d",
    "failed": "#dc2626",
    "denied": "#dc2626",
    "cancelled": "#6b7280",
    "interrupted": "#6b7280",
}
_STATUS_NAMES = {
    "running": "执行中",
    "completed": "已完成",
    "approved": "已允许",
    "failed": "失败",
    "denied": "已拒绝",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
_KIND_NAMES = {
    "run": "Run",
    "turn": "轮次",
    "model": "模型",
    "tool": "工具",
    "approval": "审批",
    "retry": "重试",
    "compaction": "上下文压缩",
}


def _set_text(editor: QPlainTextEdit, text: str) -> None:
    if editor.toPlainText() != text:
        scroll = editor.verticalScrollBar().value()
        editor.setPlainText(text)
        editor.verticalScrollBar().setValue(scroll)


class TracePayloadView(QWidget):
    """Expandable JSON or selectable text, using only the stored redacted payload."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = _MISSING
        self._serialized: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel(title))
        header.addStretch()
        self._format = _trace_combo()
        self._format.addItems(["结构", "JSON"])
        self._format.setAccessibleName(f"{title}显示格式")
        self._format.currentIndexChanged.connect(self._update_mode)
        header.addWidget(self._format)
        self._copy = QPushButton("复制")
        self._copy.setAccessibleName(f"复制{title}")
        self._copy.clicked.connect(self._copy_payload)
        header.addWidget(self._copy)
        layout.addLayout(header)
        self._stack = QStackedWidget()
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["字段", "值"])
        self._tree.setColumnWidth(0, 150)
        self._tree.setMinimumHeight(180)
        self._tree.setAccessibleName(f"{title}结构")
        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._editor.setMinimumHeight(180)
        self._editor.setAccessibleName(title)
        self._stack.addWidget(self._tree)
        self._stack.addWidget(self._editor)
        layout.addWidget(self._stack)
        self.set_value(None)

    def set_value(self, value: Any) -> None:
        self._text = _pretty(value)
        structured = isinstance(value, (dict, list))
        self._format.setEnabled(structured)
        _set_text(self._editor, self._text)
        serialized = json.dumps(value, ensure_ascii=False)
        if serialized != self._serialized:
            self._serialized = serialized
            self._tree.clear()
            if structured:
                self._add_value(None, "$", value)
                self._tree.expandToDepth(1)
        self._update_mode()

    def _update_mode(self, _index: int = 0) -> None:
        self._stack.setCurrentIndex(
            0 if self._format.isEnabled() and self._format.currentIndex() == 0 else 1
        )

    def _copy_payload(self) -> None:
        QApplication.clipboard().setText(self._text)

    def _add_value(
        self, parent: QTreeWidgetItem | None, key: str, value: Any,
    ) -> None:
        if isinstance(value, dict):
            summary = f"{{ {len(value)} 个字段 }}"
        elif isinstance(value, list):
            summary = f"[ {len(value)} 项 ]"
        else:
            summary = json.dumps(value, ensure_ascii=False)
        item = QTreeWidgetItem([key, summary])
        item.setToolTip(1, summary)
        item.setForeground(1, QColor("#345c96"))
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        if isinstance(value, dict):
            for child_key, child in value.items():
                self._add_value(item, str(child_key), child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._add_value(item, str(index), child)


class TracePage(QWidget):
    """Resizable event tree and detail tabs for current and historical Runs."""

    def __init__(self, store: TraceStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._selected_run_id: str | None = None
        self._loaded_run_id: str | None = None
        self._node_by_id: dict[str, dict[str, Any]] = {}
        self._items: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        heading = QLabel("轨迹 / Trajectory")
        heading.setObjectName("traceTitle")
        toolbar.addWidget(heading)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("本会话 Run"))
        self._runs = _trace_combo()
        self._runs.setMinimumWidth(280)
        self._runs.setMinimumContentsLength(30)
        self._runs.setPlaceholderText("当前 Session 暂无 Run")
        self._runs.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._runs.setAccessibleName("选择当前会话的 Run")
        self._runs.setToolTip("仅显示当前 Session 下的运行记录")
        self._runs.currentIndexChanged.connect(self._run_selection_changed)
        toolbar.addWidget(self._runs)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self._splitter = splitter
        tree_pane = QWidget()
        tree_layout = QVBoxLayout(tree_pane)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_toolbar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索节点、类型或状态")
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("搜索执行树")
        self._search.textChanged.connect(self._filter_tree)
        tree_toolbar.addWidget(self._search, 1)
        self._expand = QPushButton("展开")
        self._collapse = QPushButton("折叠")
        tree_toolbar.addWidget(self._expand)
        tree_toolbar.addWidget(self._collapse)
        tree_layout.addLayout(tree_toolbar)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["执行树", "耗时", "状态"])
        self._tree.setAccessibleName("Run 执行树")
        self._tree.setIndentation(16)
        self._tree.setColumnWidth(0, 215)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.currentItemChanged.connect(self._node_selection_changed)
        self._expand.clicked.connect(self._tree.expandAll)
        self._collapse.clicked.connect(self._tree.collapseAll)
        tree_layout.addWidget(self._tree, 1)
        self._tree_hint = QLabel()
        self._tree_hint.setObjectName("traceMuted")
        tree_layout.addWidget(self._tree_hint)
        splitter.addWidget(tree_pane)

        detail_pane = QWidget()
        detail_layout = QVBoxLayout(detail_pane)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_header = QHBoxLayout()
        self._node_title = QLabel()
        self._node_title.setObjectName("traceTitle")
        self._node_title.setWordWrap(True)
        self._node_title.setTextFormat(Qt.TextFormat.PlainText)
        self._node_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_header.addWidget(self._node_title, 1)
        self._previous = QPushButton("↑")
        self._previous.setFixedSize(32, 32)
        self._previous.setAccessibleName("上一个节点")
        self._previous.setToolTip("上一个可见节点")
        self._previous.clicked.connect(self._previous_node)
        self._next = QPushButton("↓")
        self._next.setFixedSize(32, 32)
        self._next.setAccessibleName("下一个节点")
        self._next.setToolTip("下一个可见节点")
        self._next.clicked.connect(self._next_node)
        detail_header.addWidget(self._previous)
        detail_header.addWidget(self._next)
        self._tree.itemExpanded.connect(self._update_navigation)
        self._tree.itemCollapsed.connect(self._update_navigation)
        detail_layout.addLayout(detail_header)
        self._metadata = QLabel()
        self._metadata.setObjectName("traceMuted")
        self._metadata.setTextFormat(Qt.TextFormat.PlainText)
        self._metadata.setWordWrap(True)
        self._metadata.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_layout.addWidget(self._metadata)
        self._tabs = QTabWidget()
        self._tabs.setMinimumWidth(220)
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        preview = QWidget()
        preview.setObjectName("tracePreview")
        preview_layout = QVBoxLayout(preview)
        self._input_view = TracePayloadView("输入 / Input")
        self._output_view = TracePayloadView("输出 / Output")
        self._output_view.setObjectName("traceOutput")
        preview_layout.addWidget(self._input_view, 1)
        preview_layout.addWidget(self._output_view, 1)
        preview_scroll.setWidget(preview)
        self._tabs.addTab(preview_scroll, "预览")
        self._detail_tabs: list[QPlainTextEdit] = []
        for title in ("概览", "事件", "原始 JSON"):
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            self._detail_tabs.append(text)
            self._tabs.addTab(text, title)
        detail_layout.addWidget(self._tabs, 1)
        splitter.addWidget(detail_pane)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([370, 560])
        layout.addWidget(splitter, 1)
        self.setStyleSheet(
            "QLabel { color: #202124; background: transparent; }"
            "QPushButton { background: transparent; border: 0; border-radius: 8px;"
            "color: #3b424d; padding: 8px 10px; text-align: center; }"
            "QPushButton:hover { background: #e6e9ed; }"
            "QPushButton:disabled { color: #9299a3; }"
            "QTreeWidget, QPlainTextEdit { background: white; color: #202124;"
            "border: 1px solid #e1e4e8; border-radius: 10px;"
            "selection-background-color: #dfe7f2; selection-color: #202124; }"
            "QTabWidget::pane { border: 0; background: transparent; }"
            "QTabBar::tab { padding: 8px 12px; margin-right: 4px; color: #68707c;"
            "background: transparent; border: 0; border-radius: 8px; }"
            "QTabBar::tab:hover { background: #e6e9ed; }"
            "QTabBar::tab:selected { color: #202124; background: #dfe7f2; }"
            "QTreeWidget { outline: 0; }"
            "QTreeWidget::item { padding: 5px 2px; border-radius: 6px; }"
            "QTreeWidget::item:hover { background: #f0f2f5; }"
            "QTreeWidget::item:selected { background: #dfe7f2; color: #202124; }"
            "QHeaderView::section { background: #f0f2f5; color: #68707c;"
            "border: 0; padding: 6px; font-size: 11px; }"
            "QLabel#traceTitle { font-size: 14px; font-weight: 600; color: #222831; }"
            "QLabel#traceMuted { color: #8a9099; font-size: 11px; }"
            "QScrollArea, QWidget#tracePreview { background: transparent; border: 0; }"
            "QLineEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QSplitter::handle { background: #e1e4e8; width: 1px; }"
        )
        self._show_empty()

    def refresh_runs(
        self,
        runs: list[dict[str, Any]],
        preferred_run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        selected = preferred_run_id or self._selected_run_id
        self._runs.blockSignals(True)
        self._runs.clear()
        visible_runs = [
            run for run in runs
            if session_id is None or run["session_id"] == session_id
        ]
        for run in visible_runs:
            started = datetime.fromtimestamp(run["started_at"]).astimezone()
            status = _STATUS_NAMES.get(run["status"], run["status"])
            title = (
                f"{started:%m-%d %H:%M:%S}  {status}  "
                f"{_duration(run['elapsed_ms'])}  #{run['run_id'][:8]}"
            )
            self._runs.addItem(title, run["run_id"])
            self._runs.setItemData(
                self._runs.count() - 1,
                f"Run ID: {run['run_id']}\nSession ID: {run['session_id']}",
                Qt.ItemDataRole.ToolTipRole,
            )
        target = next(
            (
                index
                for index in range(self._runs.count())
                if self._runs.itemData(index) == selected
            ),
            0 if self._runs.count() else -1,
        )
        self._runs.setCurrentIndex(target)
        self._runs.blockSignals(False)
        run_id = self._runs.currentData()
        self._selected_run_id = run_id if isinstance(run_id, str) else None
        if self._selected_run_id is None:
            self._show_empty()
        else:
            self._load_run()

    def refresh_run(self, run_id: str) -> None:
        if self._selected_run_id == run_id:
            self._load_run()

    def resizeEvent(self, event: Any) -> None:
        self._splitter.setOrientation(
            Qt.Orientation.Vertical
            if self.width() < 720
            else Qt.Orientation.Horizontal
        )
        super().resizeEvent(event)

    def _run_selection_changed(self, _index: int) -> None:
        run_id = self._runs.currentData()
        self._selected_run_id = run_id if isinstance(run_id, str) else None
        if self._selected_run_id is None:
            self._show_empty()
        else:
            self._load_run()

    def _load_run(self) -> None:
        run_id = self._selected_run_id
        if run_id is None:
            self._show_empty()
            return
        old_node_id = self._selected_node_id()
        same_run = self._loaded_run_id == run_id
        expanded = {key for key, item in self._items.items() if item.isExpanded()}
        scroll = self._tree.verticalScrollBar().value()
        roots = self._store.get_tree(run_id)
        self._tree.blockSignals(True)
        self._tree.clear()
        self._node_by_id.clear()
        self._items.clear()
        for root in roots:
            self._add_node(None, root)
        if same_run:
            for key in expanded:
                if key in self._items:
                    self._items[key].setExpanded(True)
        else:
            self._tree.expandAll()
        self._loaded_run_id = run_id
        self._filter_tree()
        selected = self._find_tree_item(old_node_id) or self._tree.topLevelItem(0)
        if selected is not None:
            self._tree.setCurrentItem(selected)
        self._tree.blockSignals(False)
        if selected is not None:
            self._show_node(self._node_by_id[selected.data(0, Qt.ItemDataRole.UserRole)])
            self._tree.verticalScrollBar().setValue(scroll if same_run else 0)
        else:
            self._show_empty()

    def _add_node(
        self,
        parent: QTreeWidgetItem | None,
        node: dict[str, Any],
    ) -> None:
        item = QTreeWidgetItem([
            node["label"], _duration(node["elapsed_ms"]),
            _STATUS_NAMES.get(node["status"], node["status"]),
        ])
        item.setData(0, Qt.ItemDataRole.UserRole, node["node_id"])
        item.setIcon(0, control_icon("trace"))
        item.setToolTip(0, f"{node['kind']} · {node['label']}\n{node['node_id']}")
        item.setToolTip(2, node["status"])
        item.setForeground(1, QColor("#687384"))
        item.setForeground(2, QColor(_STATUS_COLORS.get(node["status"], "#687384")))
        self._node_by_id[node["node_id"]] = node
        self._items[node["node_id"]] = item
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        for child in node["children"]:
            self._add_node(item, child)

    def _selected_node_id(self) -> str | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else None

    def _find_tree_item(self, node_id: str | None) -> QTreeWidgetItem | None:
        return self._items.get(node_id) if node_id is not None else None

    def _filter_tree(self, _text: str | None = None) -> None:
        query = self._search.text().strip().casefold()

        def visit(item: QTreeWidgetItem, ancestor_matches: bool = False) -> bool:
            node = self._node_by_id[item.data(0, Qt.ItemDataRole.UserRole)]
            matches = not query or query in " ".join(
                (
                    node["label"], node["kind"], node["status"], node["node_id"],
                    _KIND_NAMES.get(node["kind"], ""),
                    _STATUS_NAMES.get(node["status"], ""),
                )
            ).casefold()
            children = [
                visit(item.child(index), ancestor_matches or matches)
                for index in range(item.childCount())
            ]
            visible = ancestor_matches or matches or any(children)
            item.setHidden(not visible)
            if query and _text is not None and any(children):
                item.setExpanded(True)
            return visible

        for index in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(index))
        count = sum(not item.isHidden() for item in self._items.values())
        self._tree_hint.setText(
            f"{count} / {len(self._items)} 个节点" if count else "没有匹配的节点"
        )
        self._update_navigation()

    def _visible_items(self) -> list[QTreeWidgetItem]:
        visible: list[QTreeWidgetItem] = []

        def visit(item: QTreeWidgetItem) -> None:
            if item.isHidden():
                return
            visible.append(item)
            if item.isExpanded():
                for index in range(item.childCount()):
                    visit(item.child(index))

        for index in range(self._tree.topLevelItemCount()):
            visit(self._tree.topLevelItem(index))
        return visible

    def _update_navigation(self, _item: QTreeWidgetItem | None = None) -> None:
        items = self._visible_items()
        current = self._tree.currentItem()
        index = items.index(current) if current in items else -1
        self._previous.setEnabled(index > 0)
        self._next.setEnabled(0 <= index < len(items) - 1)

    def _previous_node(self) -> None:
        self._navigate_node(-1)

    def _next_node(self) -> None:
        self._navigate_node(1)

    def _navigate_node(self, offset: int) -> None:
        items = self._visible_items()
        current = self._tree.currentItem()
        if current in items:
            index = items.index(current) + offset
            if 0 <= index < len(items):
                self._tree.setCurrentItem(items[index])
                self._tree.scrollToItem(items[index])

    def _node_selection_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None or self._selected_run_id is None:
            return
        node_id = current.data(0, Qt.ItemDataRole.UserRole)
        node = self._node_by_id.get(node_id)
        if node is None:
            self._show_empty()
            return
        self._show_node(node)

    def _show_node(self, node: dict[str, Any]) -> None:
        events = self._store.get_node_events(self._selected_run_id or "", node["node_id"])
        event_data = [_event_data(event) for event in events]
        self._node_title.setText(node["label"])
        run = self._store.get_run(self._selected_run_id or "")
        details = node["details"]
        metadata = [
            f"{_KIND_NAMES.get(node['kind'], node['kind'])} · "
            f"{_STATUS_NAMES.get(node['status'], node['status'])} · "
            f"耗时 {_duration(node['elapsed_ms'])}",
            f"开始 {format_wall_time(node['started_at'])}",
            f"Session: {run['session_id'] if run else _MISSING}",
        ]
        if details.get("model"):
            metadata.append(f"模型: {details['model']}")
        if details.get("usage") is not None:
            metadata.append(f"Usage: {json.dumps(details['usage'], ensure_ascii=False)}")
        if node["approval_wait_ms"] is not None:
            metadata.append(f"审批等待: {_duration(node['approval_wait_ms'])}")
        self._metadata.setText("\n".join(metadata))
        self._metadata.setToolTip(f"Run: {node['run_id']}\nNode: {node['node_id']}")
        self._update_navigation()
        overview = [
            f"类型：{node['kind']}",
            f"状态：{node['status']}",
            f"开始时间：{format_wall_time(node['started_at'])}",
            f"结束时间：{format_wall_time(node['ended_at'])}",
            f"执行耗时：{_duration(node['elapsed_ms'])}",
        ]
        if node["kind"] == "approval":
            overview.append(f"审批等待：{_duration(node['approval_wait_ms'])}")
        for key, value in details.items():
            if key in {"input", "output", "partial_output"}:
                continue
            overview.append(f"{key}：{_pretty(value)}")
        _set_text(self._detail_tabs[0], "\n".join(overview))
        self._input_view.set_value(details.get("input"))
        output = details.get("output")
        if output is None and "partial_output" in details:
            output = details["partial_output"]
        self._output_view.set_value(output)
        _set_text(
            self._detail_tabs[1],
            "\n\n".join(
                f"{event['occurred_at']} · +{event['elapsed_ms']:.1f} ms · "
                f"{event['type']}\n{_pretty(event['payload'])}"
                for event in event_data
            )
            or "当前节点尚无事件记录。",
        )
        _set_text(
            self._detail_tabs[2],
            json.dumps(
                {
                    "node": {
                        key: value for key, value in node.items() if key != "children"
                    },
                    "events": event_data,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _show_empty(self) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tree.blockSignals(False)
        self._node_by_id.clear()
        self._items.clear()
        self._loaded_run_id = None
        self._node_title.setText("选择一个 Run 或执行树节点查看详情。")
        self._metadata.clear()
        self._tree_hint.setText("尚无本地 Trace")
        self._input_view.set_value(None)
        self._output_view.set_value(None)
        self._update_navigation()
        self._detail_tabs[0].setPlainText("选择一个 Run 或执行树节点查看详情。")
        for tab in self._detail_tabs[1:]:
            tab.setPlainText(_MISSING)
