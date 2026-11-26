"""火控指定点：固定屏幕尺寸的空心四角框。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsItemGroup, QGraphicsPathItem


def is_fire_control(obj) -> bool:
    """8111 实测 SPI 的 type/icon 均为 point_of_interest。"""
    return not obj.is_segment and not obj.is_player and any(
        (value or "").strip().lower() == "point_of_interest"
        for value in (obj.type, obj.icon)
    )


class FireControlMarker(QGraphicsItemGroup):
    def __init__(self, red: bool = True):
        super().__init__()
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setData(0, "fire_control")
        path = QPainterPath()
        for x, y in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            path.moveTo(x * 10, y * 18)
            path.lineTo(x * 18, y * 18)
            path.lineTo(x * 18, y * 10)
        for width in (3.0, 1.4):
            item = QGraphicsPathItem(path)
            pen = QPen(QColor("#101010"), width)
            pen.setCapStyle(Qt.FlatCap)
            pen.setJoinStyle(Qt.MiterJoin)
            item.setPen(pen)
            item.setBrush(Qt.NoBrush)
            self.addToGroup(item)
        self._ink = item
        self.set_red(red)

    def set_red(self, red: bool) -> None:
        pen = self._ink.pen()
        pen.setColor(QColor("#d93832" if red else "#101010"))
        self._ink.setPen(pen)
