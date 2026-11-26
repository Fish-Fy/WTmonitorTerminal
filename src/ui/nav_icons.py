"""侧栏入口的手绘线性图标：直接用矢量路径绘制，随 DPI 缩放。"""
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QPushButton, QSizePolicy

from . import theme


class NavButton(QPushButton):
    def __init__(self, symbol: str, label: str, parent=None):
        super().__init__(parent)
        self.symbol = symbol
        self.indicator = False
        self.setProperty("sidebarNav", True)
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setMinimumWidth(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_label(label)

    def sizeHint(self):
        return QSize(46, 40)

    def set_label(self, label):
        self.setAccessibleName(label)
        self.setToolTip(label)

    def set_indicator(self, on):
        if self.indicator != bool(on):
            self.indicator = bool(on)
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = theme.DIM if not self.isEnabled() else theme.ACCENT if self.isChecked() else theme.TEXT
        p.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.translate(self.width() / 2 - 12, self.height() / 2 - 12)
        if self.symbol == "panels":
            p.drawRoundedRect(QRectF(3, 3, 18, 18), 2, 2)
            p.drawLine(14, 3, 14, 21)
            p.drawLine(14, 9, 21, 9)
            p.drawLine(14, 15, 21, 15)
        elif self.symbol == "map":
            p.drawPolyline(QPolygonF([QPointF(3, 5), QPointF(9, 3), QPointF(15, 6),
                                      QPointF(21, 3), QPointF(21, 19), QPointF(15, 22),
                                      QPointF(9, 19), QPointF(3, 21), QPointF(3, 5)]))
            p.drawLine(9, 3, 9, 19)
            p.drawLine(15, 6, 15, 22)
        elif self.symbol == "vehicle":
            p.drawEllipse(QRectF(3, 3, 18, 18))
            p.drawLine(6, 8, 8, 9)
            p.drawLine(12, 5, 12, 7)
            p.drawLine(18, 8, 16, 9)
            p.drawLine(12, 13, 16, 9)
            p.drawEllipse(QRectF(11, 12, 2, 2))
            p.drawLine(8, 17, 16, 17)
        elif self.symbol == "library":
            p.drawRoundedRect(QRectF(3, 3, 16, 15), 2, 2)
            p.drawPolyline(QPolygonF([QPointF(7, 21), QPointF(22, 21), QPointF(22, 7)]))
            p.drawPolyline(QPolygonF([QPointF(5, 15), QPointF(9, 10), QPointF(12, 13),
                                      QPointF(15, 9), QPointF(17, 12)]))
            p.drawEllipse(QRectF(6, 6, 2, 2))
        elif self.symbol == "phone":
            p.drawRoundedRect(QRectF(6, 2, 12, 20), 2, 2)
            p.drawLine(10, 5, 14, 5)
            p.drawLine(11, 19, 13, 19)
        elif self.symbol == "back":
            p.drawLine(5, 12, 20, 12)
            p.drawPolyline(QPolygonF([QPointF(11, 6), QPointF(5, 12), QPointF(11, 18)]))
        if self.indicator:
            p.setPen(QPen(QColor(theme.PANEL), 1.5))
            p.setBrush(QColor(theme.OK))
            p.drawEllipse(QRectF(18, 1, 6, 6))
        p.end()
