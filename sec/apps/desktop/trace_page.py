"""Trace browser for persisted desktop Traces."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
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
    "trace": "Trace",
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
    """Resizable event tree and detail tabs for current and historical Traces."""

    def __init__(self, store: TraceStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._selected_trace_id: str | None = None
        self._loaded_trace_id: str | None = None
        self._span_by_id: dict[str, dict[str, Any]] = {}
        self._span_items: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        heading = QLabel("Trace / 调用树")
        heading.setObjectName("traceTitle")
        toolbar.addWidget(heading)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("本会话 Trace"))
        self._traces = _trace_combo()
        self._traces.setMinimumWidth(280)
        self._traces.setMinimumContentsLength(30)
        self._traces.setPlaceholderText("当前 Session 暂无 Trace")
        self._traces.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._traces.setAccessibleName("选择当前会话的 Trace")
        self._traces.setToolTip("仅显示当前 Session 下的 Trace")
        self._traces.currentIndexChanged.connect(self._trace_selection_changed)
        toolbar.addWidget(self._traces)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self._splitter = splitter
        tree_pane = QWidget()
        tree_layout = QVBoxLayout(tree_pane)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_toolbar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索 Span、类型或状态")
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("搜索执行树")
        self._search.textChanged.connect(self._filter_tree)
        tree_toolbar.addWidget(self._search, 1)
        self._tree_toggle = QPushButton()
        self._tree_toggle.setObjectName("traceTreeToggle")
        self._tree_toggle.setCheckable(True)
        self._tree_toggle.setFixedSize(32, 32)
        self._tree_toggle.setIconSize(QSize(16, 16))
        self._tree_toggle.clicked.connect(self._toggle_tree)
        tree_toolbar.addWidget(self._tree_toggle)
        tree_layout.addLayout(tree_toolbar)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["执行树", "耗时", "状态"])
        self._tree.setAccessibleName("Trace 执行树")
        self._tree.setIndentation(16)
        self._tree.setColumnWidth(0, 215)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.currentItemChanged.connect(self._span_selection_changed)
        tree_layout.addWidget(self._tree, 1)
        self._tree_hint = QLabel()
        self._tree_hint.setObjectName("traceMuted")
        tree_layout.addWidget(self._tree_hint)
        splitter.addWidget(tree_pane)

        detail_pane = QWidget()
        detail_layout = QVBoxLayout(detail_pane)
        detail_layout.setContentsMargins(12, 0, 0, 0)
        detail_layout.setSpacing(12)
        detail_header = QHBoxLayout()
        self._span_title = QLabel()
        self._span_title.setObjectName("traceTitle")
        self._span_title.setWordWrap(True)
        self._span_title.setTextFormat(Qt.TextFormat.PlainText)
        self._span_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_header.addWidget(self._span_title, 1)
        self._previous = QPushButton("↑")
        self._previous.setFixedSize(32, 32)
        self._previous.setAccessibleName("上一个 Span")
        self._previous.setToolTip("上一个可见 Span")
        self._previous.clicked.connect(self._previous_span)
        self._next = QPushButton("↓")
        self._next.setFixedSize(32, 32)
        self._next.setAccessibleName("下一个 Span")
        self._next.setToolTip("下一个可见 Span")
        self._next.clicked.connect(self._next_span)
        detail_header.addWidget(self._previous)
        detail_header.addWidget(self._next)
        self._tree.itemExpanded.connect(self._update_navigation)
        self._tree.itemCollapsed.connect(self._update_navigation)
        detail_layout.addLayout(detail_header)
        self._metadata = QWidget()
        self._metadata.setObjectName("traceMetadata")
        metadata_layout = QGridLayout(self._metadata)
        metadata_layout.setContentsMargins(12, 10, 12, 10)
        metadata_layout.setHorizontalSpacing(18)
        metadata_layout.setVerticalSpacing(8)
        self._metadata_fields: dict[str, QLabel] = {}
        self._metadata_captions: dict[str, QLabel] = {}
        for index, (key, caption) in enumerate((
            ("status", "状态"), ("duration", "耗时"),
            ("started", "开始时间"), ("approval", "审批等待"),
            ("tokens", "Tokens"), ("cache", "缓存"),
        )):
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(3)
            label = QLabel(caption)
            label.setObjectName("traceMuted")
            value = QLabel()
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field_layout.addWidget(label)
            field_layout.addWidget(value)
            metadata_layout.addWidget(field, index // 2, index % 2)
            self._metadata_fields[key] = value
            self._metadata_captions[key] = label
        metadata_layout.setColumnStretch(0, 1)
        metadata_layout.setColumnStretch(1, 1)
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
            "QPushButton#traceTreeToggle { padding: 0; text-align: center; }"
            "QPushButton#traceTreeToggle:checked { background: #dfe7f2; }"
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
            "QWidget#traceMetadata { background: #f0f2f5; border-radius: 10px; }"
            "QScrollArea, QWidget#tracePreview { background: transparent; border: 0; }"
            "QLineEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QSplitter::handle { background: #e1e4e8; width: 1px; }"
        )
        self._show_empty()

    def refresh_traces(
        self,
        traces: list[dict[str, Any]],
        preferred_trace_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        selected = preferred_trace_id or self._selected_trace_id
        self._traces.blockSignals(True)
        self._traces.clear()
        visible_traces = [
            trace for trace in traces
            if session_id is None or trace["session_id"] == session_id
        ]
        for trace in visible_traces:
            started = datetime.fromtimestamp(trace["started_at"]).astimezone()
            status = _STATUS_NAMES.get(trace["status"], trace["status"])
            title = (
                f"{started:%m-%d %H:%M:%S}  {status}  "
                f"{_duration(trace['elapsed_ms'])}  #{trace['trace_id'][:8]}"
            )
            self._traces.addItem(title, trace["trace_id"])
            self._traces.setItemData(
                self._traces.count() - 1,
                f"Trace ID: {trace['trace_id']}\nSession ID: {trace['session_id']}",
                Qt.ItemDataRole.ToolTipRole,
            )
        target = next(
            (
                index
                for index in range(self._traces.count())
                if self._traces.itemData(index) == selected
            ),
            0 if self._traces.count() else -1,
        )
        self._traces.setCurrentIndex(target)
        self._traces.blockSignals(False)
        trace_id = self._traces.currentData()
        self._selected_trace_id = trace_id if isinstance(trace_id, str) else None
        if self._selected_trace_id is None:
            self._show_empty()
        else:
            self._load_trace()

    def refresh_trace(self, trace_id: str) -> None:
        if self._selected_trace_id == trace_id:
            self._load_trace()

    def resizeEvent(self, event: Any) -> None:
        self._splitter.setOrientation(
            Qt.Orientation.Vertical
            if self.width() < 720
            else Qt.Orientation.Horizontal
        )
        super().resizeEvent(event)

    def _trace_selection_changed(self, _index: int) -> None:
        trace_id = self._traces.currentData()
        self._selected_trace_id = trace_id if isinstance(trace_id, str) else None
        if self._selected_trace_id is None:
            self._show_empty()
        else:
            self._load_trace()

    def _load_trace(self) -> None:
        trace_id = self._selected_trace_id
        if trace_id is None:
            self._show_empty()
            return
        old_span_id = self._selected_span_id()
        same_trace = self._loaded_trace_id == trace_id
        expanded = {key for key, item in self._span_items.items() if item.isExpanded()}
        scroll = self._tree.verticalScrollBar().value()
        roots = self._store.get_tree(trace_id)
        self._tree.blockSignals(True)
        self._tree.clear()
        self._span_by_id.clear()
        self._span_items.clear()
        for root in roots:
            self._add_span(None, root)
        if same_trace:
            for key in expanded:
                if key in self._span_items:
                    self._span_items[key].setExpanded(True)
        else:
            self._tree.expandAll()
        self._loaded_trace_id = trace_id
        self._filter_tree()
        selected = self._find_span_item(old_span_id) or self._tree.topLevelItem(0)
        if selected is not None:
            self._tree.setCurrentItem(selected)
        self._tree.blockSignals(False)
        if selected is not None:
            self._show_span(self._span_by_id[selected.data(0, Qt.ItemDataRole.UserRole)])
            self._tree.verticalScrollBar().setValue(scroll if same_trace else 0)
        else:
            self._show_empty()

    def _add_span(
        self,
        parent: QTreeWidgetItem | None,
        span: dict[str, Any],
    ) -> None:
        item = QTreeWidgetItem([
            span["label"], _duration(span["elapsed_ms"]),
            _STATUS_NAMES.get(span["status"], span["status"]),
        ])
        item.setData(0, Qt.ItemDataRole.UserRole, span["span_id"])
        item.setIcon(0, control_icon("trace"))
        item.setToolTip(0, f"{span['kind']} · {span['label']}\n{span['span_id']}")
        item.setToolTip(2, span["status"])
        item.setForeground(1, QColor("#687384"))
        item.setForeground(2, QColor(_STATUS_COLORS.get(span["status"], "#687384")))
        self._span_by_id[span["span_id"]] = span
        self._span_items[span["span_id"]] = item
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        for child in span["children"]:
            self._add_span(item, child)

    def _selected_span_id(self) -> str | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.ItemDataRole.UserRole)
        return value if isinstance(value, str) else None

    def _find_span_item(self, span_id: str | None) -> QTreeWidgetItem | None:
        return self._span_items.get(span_id) if span_id is not None else None

    def _filter_tree(self, _text: str | None = None) -> None:
        query = self._search.text().strip().casefold()

        def visit(item: QTreeWidgetItem, ancestor_matches: bool = False) -> bool:
            span = self._span_by_id[item.data(0, Qt.ItemDataRole.UserRole)]
            matches = not query or query in " ".join(
                (
                    span["label"], span["kind"], span["status"], span["span_id"],
                    _KIND_NAMES.get(span["kind"], ""),
                    _STATUS_NAMES.get(span["status"], ""),
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
        count = sum(not item.isHidden() for item in self._span_items.values())
        self._tree_hint.setText(
            f"{count} / {len(self._span_items)} 个 Span" if count else "没有匹配的 Span"
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
        branches = [item for item in self._span_items.values() if item.childCount()]
        collapsed = bool(branches) and not any(item.isExpanded() for item in branches)
        self._tree_toggle.setChecked(collapsed)
        self._tree_toggle.setEnabled(bool(branches))
        self._tree_toggle.setIcon(control_icon(
            "tree-collapsed" if collapsed else "tree-expanded",
        ))
        label = "展开执行树" if collapsed else "折叠执行树"
        self._tree_toggle.setToolTip(label)
        self._tree_toggle.setAccessibleName(label)
        items = self._visible_items()
        current = self._tree.currentItem()
        index = items.index(current) if current in items else -1
        self._previous.setEnabled(index > 0)
        self._next.setEnabled(0 <= index < len(items) - 1)

    def _toggle_tree(self, collapsed: bool) -> None:
        self._tree.blockSignals(True)
        if collapsed:
            self._tree.collapseAll()
        else:
            self._tree.expandAll()
        self._tree.blockSignals(False)
        self._update_navigation()

    def _previous_span(self) -> None:
        self._navigate_span(-1)

    def _next_span(self) -> None:
        self._navigate_span(1)

    def _navigate_span(self, offset: int) -> None:
        items = self._visible_items()
        current = self._tree.currentItem()
        if current in items:
            index = items.index(current) + offset
            if 0 <= index < len(items):
                self._tree.setCurrentItem(items[index])
                self._tree.scrollToItem(items[index])

    def _span_selection_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None or self._selected_trace_id is None:
            return
        span_id = current.data(0, Qt.ItemDataRole.UserRole)
        span = self._span_by_id.get(span_id)
        if span is None:
            self._show_empty()
            return
        self._show_span(span)

    def _show_span(self, span: dict[str, Any]) -> None:
        events = self._store.get_span_events(self._selected_trace_id or "", span["span_id"])
        event_data = [_event_data(event) for event in events]
        self._span_title.setText(span["label"])
        details = span["details"]
        fields = self._metadata_fields
        fields["status"].setText(_STATUS_NAMES.get(span["status"], span["status"]))
        fields["status"].setStyleSheet(
            f"color: {_STATUS_COLORS.get(span['status'], '#687384')}; font-weight: 600;"
        )
        fields["duration"].setText(_duration(span["elapsed_ms"]))
        fields["started"].setText(format_wall_time(span["started_at"]))
        fields["approval"].setText(_duration(span["approval_wait_ms"]))
        fields["approval"].parentWidget().setVisible(span["approval_wait_ms"] is not None)
        usage = details.get("usage")
        has_usage = isinstance(usage, dict)

        def amount(key: str) -> str:
            value = usage.get(key) if has_usage else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{value:,.0f}" if float(value).is_integer() else f"{value:g}"
            return _MISSING

        fields["tokens"].setText(
            f"输入 {amount('input')} · 输出 {amount('output')}"
        )
        fields["tokens"].setToolTip(f"总计 {amount('total_tokens')}")
        fields["cache"].setText(
            f"读取 {amount('cache_read')} · 写入 {amount('cache_write')}"
        )
        for key in ("tokens", "cache"):
            fields[key].parentWidget().setVisible(has_usage)
        self._metadata.setVisible(True)
        self._metadata.setToolTip(f"Trace: {span['trace_id']}\nSpan: {span['span_id']}")
        self._update_navigation()
        overview = [
            f"类型：{span['kind']}",
            f"状态：{span['status']}",
            f"开始时间：{format_wall_time(span['started_at'])}",
            f"结束时间：{format_wall_time(span['ended_at'])}",
            f"执行耗时：{_duration(span['elapsed_ms'])}",
        ]
        if span["kind"] == "approval":
            overview.append(f"审批等待：{_duration(span['approval_wait_ms'])}")
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
            or "当前 Span 尚无事件记录。",
        )
        _set_text(
            self._detail_tabs[2],
            json.dumps(
                {
                    "span": {
                        key: value for key, value in span.items() if key != "children"
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
        self._span_by_id.clear()
        self._span_items.clear()
        self._loaded_trace_id = None
        self._span_title.setText("选择一个 Trace 或 Span 查看详情。")
        self._metadata.hide()
        for value in self._metadata_fields.values():
            value.clear()
        self._tree_hint.setText("尚无本地 Trace")
        self._input_view.set_value(None)
        self._output_view.set_value(None)
        self._update_navigation()
        self._detail_tabs[0].setPlainText("选择一个 Trace 或 Span 查看详情。")
        for tab in self._detail_tabs[1:]:
            tab.setPlainText(_MISSING)
