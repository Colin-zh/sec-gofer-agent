"""Render recent Session names and timestamps independently."""

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)


class SessionItemDelegate(QStyledItemDelegate):
    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex,
    ) -> None:
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text = ""
        style = styled.widget.style() if styled.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)
        bounds = option.rect.adjusted(10, 0, -10, 0)
        timestamp = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        small_font = option.font
        small_font.setPointSizeF(max(8, small_font.pointSizeF() - 2))
        date_width = QFontMetrics(small_font).horizontalAdvance(timestamp)
        title_bounds = bounds.adjusted(0, 0, -(date_width + 12) if timestamp else 0, 0)
        painter.save()
        painter.setFont(option.font)
        painter.setPen(QColor("#202124"))
        painter.drawText(
            title_bounds,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(option.font).elidedText(
                title, Qt.TextElideMode.ElideRight, max(0, title_bounds.width()),
            ),
        )
        painter.setFont(small_font)
        painter.setPen(QColor("#9299a3"))
        painter.drawText(
            bounds,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            timestamp,
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        size.setHeight(max(38, size.height()))
        return size
