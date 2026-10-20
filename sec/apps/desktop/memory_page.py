"""长期记忆管理页：胶囊形分段控件 + 三层记忆的查看与增删改。

分段控件在标题/副标题下方，三个并列选项（语义 / 情景 / 程序），选中项
白底圆角小胶囊；下方 QStackedWidget 切换三层内容。语义记忆支持新增/编辑/
删除，情景记忆为只读列表（对话自动索引），程序记忆读写本地 USER.md。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

_ORIGIN_LABELS = {"user": "用户", "assistant": "助手", "explicit": "手动添加"}
_KIND_LABELS = {"message": "消息", "tool_call": "工具调用", "tool_result": "工具结果"}

_DIALOG_STYLE = """
QDialog, QWidget { background: #f7f8fa; color: #202124; }
QLabel { background: transparent; color: #202124; }
QLineEdit, QPlainTextEdit {
    background: #fff; color: #202124; border: 1px solid #dfe3ea;
    border-radius: 8px; padding: 7px;
    selection-background-color: #dfe7f2; selection-color: #202124;
}
QLineEdit:focus { border-color: #a8b8cf; }
QPushButton {
    background: #fff; color: #202124; border: 1px solid #dfe3ea;
    border-radius: 8px; padding: 7px 16px; min-height: 20px;
}
QPushButton:hover, QPushButton:focus, QPushButton:default {
    background: #edf2fa; border-color: #a8b8cf;
}
QPushButton#deleteMemory { color: #b42318; border-color: #e4b7b3; }
"""


def _format_time(timestamp: float) -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%y-%m-%d %H:%M")


def _origin_label(origin: str | None) -> str:
    return _ORIGIN_LABELS.get(origin or "", origin or "未知")


def _kind_label(kind: str | None) -> str:
    return _KIND_LABELS.get(kind or "", kind or "记录")


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _empty_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("subtle")
    label.setWordWrap(True)
    return label


class MemoryPage(QWidget):
    """长期记忆三层视图；操作通过信号桥接到 SecRuntime。"""

    back_requested = Signal()
    add_requested = Signal(str, str)
    update_requested = Signal(str, str, str)
    delete_requested = Signal(str)
    procedural_save_requested = Signal(str)

    _SEGMENTS = (
        ("semantic", "语义记忆"),
        ("episodic", "情景记忆"),
        ("procedural", "程序记忆"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._semantic: tuple = ()
        self._episodic: tuple = ()
        self._available = True
        self._procedural_dirty = False
        self.setStyleSheet(
            "QLabel { color: #202124; background: transparent; }"
            "QLabel#subtle { color: #8a9099; font-size: 11px; }"
            "QLabel#badge { background: #e8eef7; color: #3b5b8c;"
            "border-radius: 8px; padding: 2px 8px; font-size: 11px; }"
            "QFrame#segmented { background: #eef0f3; border: 1px solid #e4e7ec;"
            "border-radius: 18px; }"
            "QPushButton#segment { background: transparent; color: #5b6472;"
            "border: 0; border-radius: 14px; padding: 7px 16px; text-align: center;"
            "font-weight: 500; }"
            "QPushButton#segment:hover { color: #202124; }"
            "QPushButton#segment:checked { background: white; color: #202124;"
            "font-weight: 600; border: 1px solid #e1e4e8; }"
            "QFrame#memoryCard { background: white; border: 1px solid #e1e4e8;"
            "border-radius: 10px; }"
            "QFrame#addCard { background: #eef1f5; border: 1px solid #e1e4e8;"
            "border-radius: 10px; }"
            "QLineEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QPlainTextEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QScrollArea, QWidget#listContent { background: transparent; border: 0; }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        header = QHBoxLayout()
        back = QPushButton("返回对话")
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addStretch()
        title = QLabel("长期记忆")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(title)
        layout.addLayout(header)

        subtitle = QLabel(
            "按三层架构组织的长期记忆：语义记忆（知道什么）、情景记忆（经历过什么）、"
            "程序性记忆（应该怎么做）。"
        )
        subtitle.setObjectName("subtle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self._banner = QLabel("长期记忆未启用，无法读取或修改。")
        self._banner.setObjectName("subtle")
        self._banner.hide()
        layout.addWidget(self._banner)

        self._result = QLabel("")
        self._result.setWordWrap(True)
        self._result.hide()
        layout.addWidget(self._result)

        layout.addWidget(self._build_segmented(), 0, Qt.AlignmentFlag.AlignLeft)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_semantic_page())
        self._stack.addWidget(self._build_episodic_page())
        self._stack.addWidget(self._build_procedural_page())
        layout.addWidget(self._stack, 1)

        self._seg_buttons["semantic"].setChecked(True)
        self._stack.setCurrentIndex(0)

    def _build_segmented(self) -> QFrame:
        container = QFrame()
        container.setObjectName("segmented")
        container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(container)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)
        self._seg_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, label in self._SEGMENTS:
            button = QPushButton(label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, segment=key: self._select(segment)
            )
            group.addButton(button)
            self._seg_buttons[key] = button
            row.addWidget(button)
        return container

    def _select(self, segment: str) -> None:
        index = next(i for i, (key, _) in enumerate(self._SEGMENTS) if key == segment)
        self._stack.setCurrentIndex(index)

    def _build_semantic_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        form = QFrame()
        form.setObjectName("addCard")
        row = QHBoxLayout(form)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(8)
        self._subject_input = QLineEdit()
        self._subject_input.setPlaceholderText("主题（可选）")
        self._content_input = QLineEdit()
        self._content_input.setPlaceholderText("记忆内容")
        self._add_button = QPushButton("添加")
        self._add_button.setObjectName("primary")
        self._add_button.clicked.connect(self._submit_add)
        row.addWidget(self._subject_input, 1)
        row.addWidget(self._content_input, 2)
        row.addWidget(self._add_button)
        layout.addWidget(form)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._semantic_content = QWidget()
        self._semantic_content.setObjectName("listContent")
        self._semantic_cards = QVBoxLayout(self._semantic_content)
        self._semantic_cards.setContentsMargins(0, 0, 8, 0)
        self._semantic_cards.setSpacing(10)
        scroll.setWidget(self._semantic_content)
        layout.addWidget(scroll, 1)
        return page

    def _build_episodic_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        note = QLabel("情景记忆由对话自动索引，暂不支持手动编辑。")
        note.setObjectName("subtle")
        note.setWordWrap(True)
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._episodic_content = QWidget()
        self._episodic_content.setObjectName("listContent")
        self._episodic_cards = QVBoxLayout(self._episodic_content)
        self._episodic_cards.setContentsMargins(0, 0, 8, 0)
        self._episodic_cards.setSpacing(10)
        scroll.setWidget(self._episodic_content)
        layout.addWidget(scroll, 1)
        return page

    def _build_procedural_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._procedural_path = QLabel("")
        self._procedural_path.setObjectName("subtle")
        self._procedural_path.setWordWrap(True)
        layout.addWidget(self._procedural_path)

        self._procedural_edit = QPlainTextEdit()
        self._procedural_edit.setPlaceholderText(
            "在此编写程序性记忆（USER.md）：用户偏好、执行规则、可复用方法等。"
        )
        self._procedural_edit.textChanged.connect(self._mark_procedural_dirty)
        layout.addWidget(self._procedural_edit, 1)

        save_row = QHBoxLayout()
        save_row.addStretch()
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self._submit_procedural)
        save_row.addWidget(save)
        layout.addLayout(save_row)
        return page

    def set_data(self, payload: dict) -> None:
        self._available = bool(payload.get("available", True))
        self._semantic = payload.get("semantic", ())
        self._episodic = payload.get("episodic", ())
        procedural = payload.get("procedural") or {}
        path = procedural.get("path", "")
        content = procedural.get("content", "")
        self._procedural_path.setText(f"文件：{path or '未配置'}")
        self._banner.setVisible(not self._available)
        self._add_button.setEnabled(self._available)
        if not self._procedural_dirty:
            self._procedural_edit.blockSignals(True)
            self._procedural_edit.setPlainText(content)
            self._procedural_edit.blockSignals(False)
        self._render_semantic()
        self._render_episodic()

    def set_result(self, message: str, kind: str) -> None:
        """在页内展示操作反馈（成功/失败），避免落到被隐藏的对话面板。"""
        if not message:
            return
        color = "#b42318" if kind == "error" else "#1a7f37"
        self._result.setStyleSheet(
            f"color: {color}; font-size: 12px; background: transparent;"
        )
        self._result.setText(message)
        self._result.show()

    def _mark_procedural_dirty(self) -> None:
        self._procedural_dirty = True

    def _submit_add(self) -> None:
        subject = self._subject_input.text().strip()
        content = self._content_input.text().strip()
        if not content:
            return
        self.add_requested.emit(subject, content)
        self._subject_input.clear()
        self._content_input.clear()

    def _submit_procedural(self) -> None:
        self._procedural_dirty = False
        self.procedural_save_requested.emit(self._procedural_edit.toPlainText())

    def _render_semantic(self) -> None:
        _clear_layout(self._semantic_cards)
        if not self._semantic:
            self._semantic_cards.addWidget(
                _empty_label("暂无语义记忆。可在上方手动添加，或由对话自动抽取。")
            )
        for entry in self._semantic:
            memory_id, subject, content, origin, updated_at = entry
            card = QFrame()
            card.setObjectName("memoryCard")
            body = QVBoxLayout(card)
            body.setContentsMargins(14, 10, 14, 10)
            body.setSpacing(6)
            row = QHBoxLayout()
            title = QLabel(subject or "（无主题）")
            title.setStyleSheet("font-weight: 600; color: #202124;")
            row.addWidget(title, 1)
            edit = QPushButton("编辑")
            edit.clicked.connect(
                lambda _checked=False, e=entry: self._edit_entry(e)
            )
            delete = QPushButton("删除")
            delete.clicked.connect(
                lambda _checked=False, e=entry: self._delete_entry(e)
            )
            row.addWidget(edit)
            row.addWidget(delete)
            body.addLayout(row)
            text = QLabel(content)
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.PlainText)
            body.addWidget(text)
            meta = QLabel(
                f"来源：{_origin_label(origin)} · 更新：{_format_time(updated_at)}"
            )
            meta.setObjectName("subtle")
            body.addWidget(meta)
            self._semantic_cards.addWidget(card)
        self._semantic_cards.addStretch()

    def _render_episodic(self) -> None:
        _clear_layout(self._episodic_cards)
        if not self._episodic:
            self._episodic_cards.addWidget(
                _empty_label("暂无情景记忆。完成一轮对话后会自动索引。")
            )
        for record in self._episodic:
            _record_id, kind, text, timestamp, session_id = record
            card = QFrame()
            card.setObjectName("memoryCard")
            body = QVBoxLayout(card)
            body.setContentsMargins(14, 10, 14, 10)
            body.setSpacing(6)
            badge_row = QHBoxLayout()
            badge = QLabel(_kind_label(kind))
            badge.setObjectName("badge")
            badge_row.addWidget(badge)
            badge_row.addStretch()
            body.addLayout(badge_row)
            content = QLabel(text)
            content.setWordWrap(True)
            content.setTextFormat(Qt.TextFormat.PlainText)
            body.addWidget(content)
            meta = QLabel(f"会话 {session_id} · {_format_time(timestamp)}")
            meta.setObjectName("subtle")
            body.addWidget(meta)
            self._episodic_cards.addWidget(card)
        self._episodic_cards.addStretch()

    def _edit_entry(self, entry: tuple) -> None:
        memory_id, subject, content, _origin, _updated_at = entry
        result = _edit_memory_dialog(self, subject or "", content)
        if result is None:
            return
        new_subject, new_content = result
        if not new_content:
            return
        self.update_requested.emit(memory_id, new_subject, new_content)

    def _delete_entry(self, entry: tuple) -> None:
        memory_id, subject, content, _origin, _updated_at = entry
        label = (subject or content or memory_id)[:40]
        if _confirm_delete_memory(self, label):
            self.delete_requested.emit(memory_id)


def _edit_memory_dialog(
    parent: QWidget, subject: str, content: str,
) -> tuple[str, str] | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle("编辑记忆")
    dialog.setStyleSheet(_DIALOG_STYLE)
    dialog.setMinimumWidth(420)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)
    subject_field = QLineEdit(subject, dialog)
    subject_field.setPlaceholderText("主题（可选）")
    content_field = QLineEdit(content, dialog)
    content_field.setPlaceholderText("记忆内容")
    layout.addWidget(subject_field)
    layout.addWidget(content_field)
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        parent=dialog,
    )
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    layout.addWidget(box)
    content_field.setFocus()
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return subject_field.text().strip(), content_field.text().strip()
    finally:
        dialog.deleteLater()


def _confirm_delete_memory(parent: QWidget, label: str) -> bool:
    dialog = QDialog(parent)
    dialog.setWindowTitle("删除记忆")
    dialog.setStyleSheet(_DIALOG_STYLE)
    dialog.setMinimumWidth(360)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(16)
    message = QLabel(f"确定要删除「{label}」吗？\n此操作无法撤销。")
    message.setTextFormat(Qt.TextFormat.PlainText)
    message.setWordWrap(True)
    layout.addWidget(message)
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel,
        parent=dialog,
    )
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    delete = box.button(QDialogButtonBox.StandardButton.Yes)
    delete.setText("删除")
    delete.setObjectName("deleteMemory")
    delete.setAutoDefault(False)
    delete.setDefault(False)
    cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
    cancel.setText("取消")
    cancel.setDefault(True)
    cancel.setFocus()
    layout.addWidget(box)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
