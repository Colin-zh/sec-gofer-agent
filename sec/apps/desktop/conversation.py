"""Role-aware chat messages shared by history and streaming views."""

from datetime import datetime
from html import escape
from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTextBrowser,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sec.apps.desktop.appearance import control_icon, toggle_icon


class MessageBubble(QFrame):
    action_requested = Signal(str)

    def __init__(
        self, role: str, text: str, parent: QWidget, timestamp: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.text = text
        self.message_index: int | None = None
        self.retry_index: int | None = None
        self.end_time: float | None = None
        self._action_buttons: dict[str, QPushButton] = {}
        self._action_bar: QWidget | None = None
        self._end_time_label: QLabel | None = None
        self.setObjectName("userBubble" if role == "user" else "agentMessage")
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{"
            f"background: {'#e9edf3' if role == 'user' else 'transparent'};"
            "border: 0; border-radius: 14px; color: #000; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        self.body = QTextBrowser(self)
        self.body.setStyleSheet(
            "QTextBrowser { background: transparent; color: #000; border: 0; padding: 0; }"
        )
        self.body.document().setDefaultStyleSheet(
            "body, p, pre, code, a, h1, h2, h3, li, blockquote { color: #000; }"
        )
        self.body.setOpenLinks(False)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.body)
        if role == "assistant":
            self._build_action_row(layout)
        self.set_text(text)
        if role == "assistant" and timestamp is not None:
            self.set_end_time(timestamp)

    def _build_action_row(self, layout: QVBoxLayout) -> None:
        bar = QWidget(self)
        bar.setStyleSheet("QWidget { background: transparent; }")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(2)
        toggle_icons = {
            "mark": "mark-active",
            "like": "like-active",
            "dislike": "dislike-active",
        }
        for name, tip, checkable in (
            ("copy", "复制", False),
            ("mark", "标记", True),
            ("like", "点赞", True),
            ("dislike", "点踩", True),
            ("fork", "分支", False),
            ("retry", "重试", False),
            ("share", "分享", False),
        ):
            button = QPushButton()
            if name in toggle_icons:
                button.setIcon(toggle_icon(name, toggle_icons[name]))
            else:
                button.setIcon(control_icon(name))
            button.setIconSize(QSize(16, 16))
            button.setFixedSize(24, 24)
            button.setCheckable(checkable)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton { background: transparent; border: 0; border-radius: 6px;"
                "padding: 0; }"
                "QPushButton:hover { background: #e6e9ed; }"
            )
            button.clicked.connect(self._action_clicked)
            self._action_buttons[name] = button
            row.addWidget(button)
        row.addStretch(1)
        self._end_time_label = QLabel("")
        self._end_time_label.setStyleSheet(
            "color: #9299a3; font-size: 11px; background: transparent;"
        )
        row.addWidget(self._end_time_label)
        layout.addWidget(bar)
        self._action_bar = bar
        # 操作栏在模型响应完成前隐藏，完成后随结束时间一起展示。
        bar.setVisible(False)

    def _action_clicked(self) -> None:
        sender = self.sender()
        for name, button in self._action_buttons.items():
            if button is sender:
                self._emit_action(name)
                return

    def _emit_action(self, name: str) -> None:
        if name == "like":
            dislike = self._action_buttons.get("dislike")
            if dislike is not None:
                dislike.setChecked(False)
        elif name == "dislike":
            like = self._action_buttons.get("like")
            if like is not None:
                like.setChecked(False)
        self.action_requested.emit(name)

    def set_end_time(self, timestamp: float | None) -> None:
        if timestamp is None or self._end_time_label is None:
            return
        self.end_time = timestamp
        formatted = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        self._end_time_label.setText(formatted)
        self._end_time_label.setToolTip(formatted)
        if self._action_bar is not None:
            self._action_bar.setVisible(True)

    def action_checked(self, name: str) -> bool:
        button = self._action_buttons.get(name)
        return bool(button is not None and button.isChecked())

    def set_action_checked(self, name: str, checked: bool) -> None:
        button = self._action_buttons.get(name)
        if button is not None:
            button.setChecked(checked)

    def set_text(self, text: str) -> None:
        self.text = text
        if self.role in {"assistant", "process"}:
            self.body.setMarkdown(text)
        else:
            self.body.setPlainText(text)
        self._fit_height()

    def _fit_height(self) -> None:
        document = self.body.document()
        document.setTextWidth(max(1, self.body.viewport().width()))
        self.body.setFixedHeight(max(24, int(document.size().height()) + 4))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_height()


class ApprovalCard(MessageBubble):
    decision_requested = Signal(str, bool)

    def __init__(self, call_id: str, tool_name: str, arguments: str, parent: QWidget) -> None:
        super().__init__(
            "notice",
            f"工具审批 · {tool_name}\n\n{arguments}\n\n"
            "工具在当前用户权限下运行，并非沙箱。请确认后选择允许或拒绝。",
            parent,
        )
        self.setObjectName("approvalCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QFrame#approvalCard { background: #edf2fa; border: 1px solid #dfe3ea;"
            "border-radius: 12px; color: #202124; }"
        )
        actions = QHBoxLayout()
        actions.addStretch()
        self.reject = QPushButton("拒绝")
        self.allow = QPushButton("允许本次")
        self.allow.setObjectName("primary")
        for button, approved in ((self.reject, False), (self.allow, True)):
            button.clicked.connect(
                lambda _checked=False, value=approved: self._decide(call_id, value)
            )
            actions.addWidget(button)
        self.layout().addLayout(actions)

    def _decide(self, call_id: str, approved: bool) -> None:
        self.reject.setEnabled(False)
        self.allow.setEnabled(False)
        self.decision_requested.emit(call_id, approved)


class ActivityBubble(MessageBubble):
    expansion_changing = Signal(object)

    def __init__(self, text: str, parent: QWidget, timestamp: float | None = None) -> None:
        self.timestamp = timestamp
        self.expansion_key: tuple[str, int] | None = None
        super().__init__("process", text, parent)
        font = self.body.font()
        font.setPointSizeF(max(7, font.pointSizeF() - 2))
        self.body.setFont(font)
        self.body.document().setDefaultFont(font)
        self.setObjectName("activityCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.layout().setContentsMargins(6, 3, 6, 3)
        self.setStyleSheet(
            "QFrame#activityCard { background: transparent; border: 0; color: #68707c; }"
        )
        self.toggle = QPushButton()
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(
            "QPushButton { color: #68707c; text-align: left; border: 0;"
            "background: transparent; padding: 2px 0; font-size: 11px; }"
        )
        self.toggle.toggled.connect(self._set_expanded)
        self.layout().insertWidget(0, self.toggle)
        self._set_expanded(False)

    def _set_expanded(self, expanded: bool) -> None:
        summary = self.text.splitlines()[0] if self.text else "工具记录"
        self.expansion_changing.emit(self.toggle)
        clock = (
            datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S") + "  "
            if self.timestamp is not None else ""
        )
        self.toggle.setText(f"{'▾' if expanded else '▸'} {clock}{summary[:100]}")
        if self.timestamp is not None:
            self.toggle.setToolTip(datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S"))
        self.body.setVisible(expanded)
        if expanded:
            self._fit_height()


class ProcessGroup(QFrame):
    expansion_changing = Signal(object)

    def __init__(self, parent: QWidget, started_at: float | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.count = 0
        self.turn = 0
        self.started_at = started_at
        self.finished_at: float | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 0, 0, 0)
        self._layout.setSpacing(4)
        self.toggle = QPushButton()
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(
            "QPushButton { color: #858b95; font-size: 11px; padding: 4px 0;"
            "background: transparent; border: 0; text-align: left; }"
        )
        self._layout.addWidget(self.toggle)
        self.details = QFrame()
        self.details.setStyleSheet("QFrame { border: 0; border-left: 1px solid #dfe3ea; }")
        self.steps = QVBoxLayout(self.details)
        self.steps.setContentsMargins(12, 2, 0, 2)
        self.steps.setSpacing(4)
        self._layout.addWidget(self.details)
        self.toggle.toggled.connect(self._expand)
        self._expand(False)

    def add_step(self, widget: QWidget) -> None:
        self.steps.addWidget(widget)
        self.count += 1
        self._label()

    def _label(self) -> None:
        duration = ""
        if self.started_at is not None and self.finished_at is not None:
            seconds = max(0, int(self.finished_at - self.started_at))
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            elapsed = (
                f"{hours}时{minutes}分{seconds}秒" if hours
                else f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"
            )
            duration = f" · 耗时 {elapsed}"
        self.toggle.setText(
            f"{'▾' if self.toggle.isChecked() else '▸'} 处理过程 · {self.count} 条记录{duration}"
        )

    def update_time(self, timestamp: float | None) -> None:
        if timestamp is not None:
            self.finished_at = timestamp
            self._label()

    def _expand(self, expanded: bool) -> None:
        self.expansion_changing.emit(self.toggle)
        self.details.setVisible(expanded)
        self._label()


class Conversation(QScrollArea):
    approval_decided = Signal(str, bool)
    message_action = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QScrollArea, QWidget#messages { background: transparent; }")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content.setObjectName("messages")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(32, 12, 0, 12)
        self._layout.setSpacing(16)
        self._layout.addStretch(1)
        self.setWidget(self._content)
        self.bubbles: list[MessageBubble] = []
        self._activity_expansion: dict[tuple[str, int], bool] = {}
        self._activity_counts: dict[str, int] = {}
        self.process_groups: list[ProcessGroup] = []
        self._group: ProcessGroup | None = None
        self._turn = 0
        self._turn_started_at: float | None = None
        self._group_expansion: dict[int, bool] = {}
        self._follow_tail = True
        self._restoring = False
        self._anchor: tuple[QWidget, int] | None = None
        self._user_targets: list[tuple[MessageBubble, QPushButton]] = []
        self._nav_targets: list[tuple[MessageBubble, QPushButton]] = []
        self._navigation = QScrollArea(self.viewport())
        self._navigation.setWidgetResizable(True)
        self._navigation.setFrameShape(QFrame.Shape.NoFrame)
        self._navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._navigation.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._navigation.setStyleSheet(
            "QScrollArea, QWidget#inputNavigationRail { background: transparent; border: 0; }"
        )
        self._navigation.viewport().setAutoFillBackground(False)
        self._navigation.setAccessibleName("会话导航")
        rail = QWidget()
        rail.setObjectName("inputNavigationRail")
        rail.setAutoFillBackground(False)
        self._navigation_layout = QVBoxLayout(rail)
        self._navigation_layout.setContentsMargins(2, 4, 2, 4)
        self._navigation_layout.setSpacing(0)
        self._navigation.setWidget(rail)
        self._bottom_button = QPushButton(self.viewport())
        self._bottom_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self._bottom_button.setFixedSize(34, 34)
        self._bottom_button.setToolTip("回到底部")
        self._bottom_button.setAccessibleName("回到底部")
        self._bottom_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bottom_button.setStyleSheet(
            "QPushButton { background: white; border: 1px solid #dfe3ea;"
            "border-radius: 17px; padding: 0; text-align: center; }"
            "QPushButton:hover { background: #edf2fa; }"
        )
        self._bottom_button.clicked.connect(self.scroll_to_bottom)
        self._update_navigation()
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)

    def _scroll_changed(self, value: int) -> None:
        if not self._restoring and self._anchor is None:
            self._follow_tail = self.verticalScrollBar().maximum() - value <= 24
        self._update_navigation()

    def _range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow_tail and not self._restoring and self._anchor is None:
            self.verticalScrollBar().setValue(maximum)
        self._update_navigation()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_navigation"):
            self._update_navigation()

    def scroll_to_bottom(self) -> None:
        self._anchor = None
        self._follow_tail = True
        if self._restoring:
            self._saved_follow = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self._update_navigation()

    def _jump_to_user(self, bubble: MessageBubble) -> None:
        self._anchor = None
        self._layout.activate()
        target = max(0, bubble.mapTo(self._content, QPoint()).y() - 12)
        self.verticalScrollBar().setValue(target)
        self._follow_tail = self.verticalScrollBar().maximum() - self.verticalScrollBar().value() <= 24
        if self._restoring:
            self._saved_scroll = self.verticalScrollBar().value()
            self._saved_follow = self._follow_tail
        self._update_navigation()

    def _jump_to_marker(self) -> None:
        marker = self.sender()
        for bubble, button in self._nav_targets:
            if button is marker:
                self._jump_to_user(bubble)
                return

    def _update_navigation(self) -> None:
        targets = self._nav_targets
        height = min(len(targets) * 14 + 8, max(40, self.viewport().height() // 2))
        self._navigation.setGeometry(0, max(0, (self.viewport().height() - height) // 2), 26, height)
        self._navigation.setVisible(bool(targets))
        self._navigation.raise_()
        bar = self.verticalScrollBar()
        active = 0
        for index, (bubble, _button) in enumerate(targets):
            if bubble.mapTo(self._content, QPoint()).y() <= bar.value() + 24:
                active = index
        for index, (_bubble, button) in enumerate(targets):
            button.setChecked(index == active)
        self._bottom_button.move(
            max(0, (self.viewport().width() - 34) // 2),
            max(0, self.viewport().height() - 46),
        )
        self._bottom_button.setVisible(bar.maximum() - bar.value() > 24)
        self._bottom_button.raise_()

    def _rebuild_navigation(self) -> None:
        self._nav_targets = list(self._user_targets)
        for _ in range(self._navigation_layout.count()):
            self._navigation_layout.takeAt(0)
        for _bubble, button in self._nav_targets:
            self._navigation_layout.addWidget(button)
        self._update_navigation()

    def set_bubble_marked(self, bubble: MessageBubble, marked: bool) -> None:
        user_bubble = self._user_bubble_for(bubble)
        if user_bubble is None:
            return
        marker = self._marker_for_user(user_bubble)
        if marker is not None:
            marker.setText("●" if marked else "—")
            marker.setStyleSheet(self._user_marker_style(marked))

    def _user_bubble_for(self, assistant: MessageBubble) -> MessageBubble | None:
        try:
            index = self.bubbles.index(assistant)
        except ValueError:
            return None
        for bubble in reversed(self.bubbles[:index]):
            if bubble.role == "user":
                return bubble
        return None

    def _marker_for_user(self, user_bubble: MessageBubble) -> QPushButton | None:
        for target, marker in self._user_targets:
            if target is user_bubble:
                return marker
        return None

    def _preserve_toggle(self, widget: QWidget) -> None:
        if not widget.isVisible() or self._restoring:
            return
        self._anchor = (widget, widget.mapTo(self.viewport(), QPoint()).y())
        self._anchor_passes = 0
        QTimer.singleShot(0, self, self._restore_anchor)

    def _restore_anchor(self) -> None:
        if self._anchor is None:
            return
        widget, previous_y = self._anchor
        bar = self.verticalScrollBar()
        self._layout.activate()
        if widget.isVisible():
            bar.setValue(bar.value() + widget.mapTo(self.viewport(), QPoint()).y() - previous_y)
        self._anchor_passes += 1
        if self._anchor_passes < 3:
            QTimer.singleShot(0, self, self._restore_anchor)
            return
        self._anchor = None
        self._follow_tail = bar.maximum() - bar.value() <= 24

    def begin_refresh(self) -> None:
        self._saved_scroll = self.verticalScrollBar().value()
        self._saved_follow = self._follow_tail
        self._restoring = True

    def end_refresh(self) -> None:
        QTimer.singleShot(0, self, self._finish_refresh)

    def _finish_refresh(self) -> None:
        self._layout.activate()
        self._content.adjustSize()
        bar = self.verticalScrollBar()
        self._follow_tail = self._saved_follow
        bar.setValue(bar.maximum() if self._saved_follow else self._saved_scroll)
        self._restoring = False

    def add_message(self, role: str, text: str, *, timestamp: float | None = None) -> MessageBubble:
        row = QWidget(self._content)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            self._group = None
            self._turn += 1
            self._turn_started_at = timestamp
        if role in {"activity", "process"}:
            bubble = ActivityBubble(text, row, timestamp)
            occurrence = self._activity_counts.get(text, 0)
            self._activity_counts[text] = occurrence + 1
            key = (text, occurrence)
            bubble.expansion_key = key
            bubble.toggle.setChecked(self._activity_expansion.get(key, False))
            bubble.toggle.toggled.connect(self._activity_toggled)
            bubble.expansion_changing.connect(self._preserve_toggle)
            if self._group is None:
                self._group = ProcessGroup(
                    self._content,
                    self._turn_started_at if self._turn_started_at is not None else timestamp,
                )
                self._group.turn = self._turn
                self.process_groups.append(self._group)
                self._group.toggle.setChecked(self._group_expansion.get(self._turn, False))
                self._group.expansion_changing.connect(self._preserve_toggle)
                self._group.toggle.toggled.connect(self._group_toggled)
                self._layout.insertWidget(self._layout.count() - 1, self._group)
            self._group.add_step(bubble)
            self._group.update_time(timestamp)
            row.deleteLater()
            self.bubbles.append(bubble)
            QTimer.singleShot(0, bubble, bubble._fit_height)
            return bubble
        else:
            bubble = MessageBubble(role, text, row, timestamp=timestamp)
            if role == "assistant" and self._group is not None:
                self._group.update_time(timestamp)
        if role == "assistant":
            bubble.action_requested.connect(self._message_action_requested)
        if role == "user":
            layout.addStretch(1)
            layout.addWidget(bubble, 4)
        else:
            layout.addWidget(bubble, 1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self.bubbles.append(bubble)
        if role == "user":
            self._add_user_marker(bubble, text)
        QTimer.singleShot(0, bubble, bubble._fit_height)
        return bubble

    def _message_action_requested(self, action: str) -> None:
        bubble = self.sender()
        if isinstance(bubble, MessageBubble):
            self.message_action.emit(action, bubble)

    def _activity_toggled(self, checked: bool) -> None:
        toggle = self.sender()
        bubble = toggle.parent() if isinstance(toggle, QPushButton) else None
        if isinstance(bubble, ActivityBubble) and bubble.expansion_key is not None:
            self._activity_expansion[bubble.expansion_key] = checked

    def _group_toggled(self, checked: bool) -> None:
        toggle = self.sender()
        group = toggle.parent() if isinstance(toggle, QPushButton) else None
        if isinstance(group, ProcessGroup):
            self._group_expansion[group.turn] = checked

    def _user_marker_style(self, marked: bool) -> str:
        color = "#22a06b" if marked else "#bcc2cb"
        hover = "#1a7f52" if marked else "#858b95"
        checked = "#0f5c3a" if marked else "#000000"
        size = " font-size: 10px;" if marked else ""
        return (
            "QPushButton { background: transparent; border: 0; padding: 0;"
            f"color: {color};{size} text-align: center; }}"
            f"QPushButton:hover {{ color: {hover}; background: transparent; }}"
            f"QPushButton:checked {{ color: {checked}; font-weight: 700; }}"
        )

    def _add_user_marker(self, bubble: MessageBubble, text: str) -> None:
        marker = QPushButton("—")
        marker.setFixedSize(22, 14)
        marker.setCheckable(True)
        marker.setCursor(Qt.CursorShape.PointingHandCursor)
        number = len(self._user_targets) + 1
        preview = " ".join(text.split())[:240]
        marker.setToolTip(f"<b>第 {number} 条输入</b><br>{escape(preview)}")
        marker.setAccessibleName(f"定位第 {number} 条用户输入")
        marker.setStyleSheet(self._user_marker_style(False))
        marker.clicked.connect(self._jump_to_marker)
        self._navigation_layout.addWidget(marker)
        self._user_targets.append((bubble, marker))
        self._rebuild_navigation()

    def clear(self) -> None:
        self._user_targets.clear()
        self._nav_targets.clear()
        while self._navigation_layout.count():
            item = self._navigation_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._update_navigation()
        self.bubbles.clear()
        self._activity_counts.clear()
        self.process_groups.clear()
        self._group = None
        self._turn = 0
        self._turn_started_at = None
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def reset_expansion(self) -> None:
        self._activity_expansion.clear()
        self._group_expansion.clear()
        self._follow_tail = True

    def add_approval(self, call_id: str, tool_name: str, arguments: str) -> ApprovalCard:
        card = ApprovalCard(call_id, tool_name, arguments, self._content)
        card.decision_requested.connect(self.approval_decided)
        self._layout.insertWidget(self._layout.count() - 1, card)
        QTimer.singleShot(0, card, card._fit_height)
        return card
