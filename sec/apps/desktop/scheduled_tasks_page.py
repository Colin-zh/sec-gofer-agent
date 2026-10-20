"""Scheduled tasks management page for Sec Desktop."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%y-%m-%d %H:%M")


class ScheduledTasksPage(QWidget):
    """定时任务列表与添加表单；操作通过信号桥接到 SecRuntime。"""

    back_requested = Signal()
    add_requested = Signal(str, str, str)
    remove_requested = Signal(str)
    toggle_requested = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self._jobs: tuple = ()
        self.setStyleSheet(
            "QLabel { color: #202124; background: transparent; }"
            "QLabel#subtle { color: #8a9099; font-size: 11px; }"
            "QLabel#error { color: #c0392b; font-size: 11px; }"
            "QLineEdit { background: white; color: #202124; padding: 10px 12px;"
            "border: 1px solid #dfe3ea; border-radius: 8px; }"
            "QFrame#jobCard { background: white; border: 1px solid #e1e4e8;"
            "border-radius: 10px; }"
            "QFrame#addCard { background: #eef1f5; border: 1px solid #e1e4e8;"
            "border-radius: 10px; }"
            "QScrollArea, QWidget#jobsContent { background: transparent; border: 0; }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        header = QHBoxLayout()
        back = QPushButton("返回对话")
        back.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        back.clicked.connect(self.back_requested)
        header.addWidget(back)
        header.addStretch()
        title = QLabel("定时任务")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        header.addWidget(title)
        layout.addLayout(header)
        note = QLabel(
            "按 cron 表达式在无人值守下自动运行 Agent 任务，每个任务使用独立会话。"
            "cron 格式：分 时 日 月 周（如 0 9 * * 1-5 表示工作日 09:00）。"
        )
        note.setWordWrap(True)
        note.setObjectName("subtle")
        layout.addWidget(note)
        layout.addWidget(self._build_add_form())
        self._error = QLabel("")
        self._error.setObjectName("error")
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content.setObjectName("jobsContent")
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(0, 0, 8, 0)
        self._cards.setSpacing(10)
        scroll.setWidget(self._content)
        layout.addWidget(scroll, 1)
        self._render()

    def _build_add_form(self) -> QFrame:
        form = QFrame()
        form.setObjectName("addCard")
        row = QHBoxLayout(form)
        row.setContentsMargins(12, 12, 12, 12)
        row.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("名称")
        self._schedule = QLineEdit()
        self._schedule.setPlaceholderText("分 时 日 月 周 · 0 9 * * 1-5")
        self._prompt = QLineEdit()
        self._prompt.setPlaceholderText("任务提示词")
        add = QPushButton("添加")
        add.setObjectName("primary")
        add.clicked.connect(self._submit_add)
        row.addWidget(self._name, 1)
        row.addWidget(self._schedule, 2)
        row.addWidget(self._prompt, 3)
        row.addWidget(add)
        return form

    def _submit_add(self) -> None:
        name = self._name.text().strip()
        schedule = self._schedule.text().strip()
        prompt = self._prompt.text().strip()
        if not name or not schedule or not prompt:
            self.show_error("名称、cron 表达式与任务提示词均不能为空。")
            return
        self.add_requested.emit(name, schedule, prompt)
        self._name.clear()
        self._schedule.clear()
        self._prompt.clear()

    def set_jobs(self, jobs: tuple) -> None:
        self._jobs = jobs
        self._render()

    def show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(bool(message))

    def _render(self) -> None:
        while self._cards.count():
            item = self._cards.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        if not self._jobs:
            empty = QLabel("暂无定时任务。添加后会自动按计划触发。")
            empty.setObjectName("subtle")
            self._cards.addWidget(empty)
        now = datetime.now()
        for job in self._jobs:
            next_run = job.next_run_at(now)
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            card.setObjectName("jobCard")
            body = QVBoxLayout(card)
            body.setContentsMargins(14, 10, 14, 10)
            row = QHBoxLayout()
            enabled = QCheckBox(job.name)
            enabled.setChecked(job.enabled)
            enabled.toggled.connect(
                lambda checked, job_id=job.id: self.toggle_requested.emit(
                    job_id, checked,
                )
            )
            row.addWidget(enabled, 1)
            remove = QPushButton()
            remove.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            remove.setIconSize(QSize(16, 16))
            remove.setFixedSize(32, 32)
            remove.setStyleSheet("QPushButton { padding: 0; text-align: center; }")
            remove.setToolTip("删除定时任务")
            remove.setAccessibleName("删除定时任务")
            remove.clicked.connect(
                lambda _checked=False, job_id=job.id: self.remove_requested.emit(job_id)
            )
            row.addWidget(remove)
            body.addLayout(row)
            detail = QLabel(
                f"cron：{job.schedule}\n下次触发：{_format_time(next_run)}"
                f"\n上次状态：{job.last_status or '尚未运行'}"
            )
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setWordWrap(True)
            body.addWidget(detail)
            self._cards.addWidget(card)
        self._cards.addStretch()
