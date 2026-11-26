"""Shared drawing for tactical areas on the map and in share previews."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from ..i18n import tr
from ..store.areas import AREA_TYPES
from ..store.models import MARKER_TYPES


def area_path(shape, geometry, width, height):
    path = QPainterPath()
    if shape == "circle":
        x, y, radius = geometry
        radius *= min(width, height)
        path.addEllipse(QPointF(x * width, y * height), radius, radius)
    elif geometry:
        x, y = geometry[0]
        path.moveTo(x * width, y * height)
        for x, y in geometry[1:]:
            path.lineTo(x * width, y * height)
        if len(geometry) >= 3:
            path.closeSubpath()
    return path


class AreaItem(QGraphicsPathItem):
    def __init__(self, area, width, height, *, selectable=False):
        super().__init__()
        self.area = area
        self.setZValue(1)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, selectable)
        self.setToolTip(tr(AREA_TYPES[area.type]))
        self.layout_on_map(width, height)

    def layout_on_map(self, width, height):
        self._map_rect = QRectF(0, 0, width, height)
        self._outline = area_path(self.area.shape, self.area.geometry, width, height)
        map_path = QPainterPath()
        map_path.addRect(self._map_rect)
        self.setPath(self._outline.intersected(map_path))

    def paint(self, painter, option, widget=None):
        painter.save()
        painter.setClipRect(self._map_rect, Qt.ClipOperation.IntersectClip)
        pen, brush = area_style(self.area.type, highlighted=self.isSelected())
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawPath(self._outline)
        painter.restore()


def area_style(kind, *, highlighted=False):
    color = QColor(MARKER_TYPES[kind][1])
    pen = QPen(color, 3 if highlighted else 2,
               Qt.PenStyle.DashLine if kind == "rally" else Qt.PenStyle.SolidLine)
    pen.setCosmetic(True)
    color.setAlpha(66 if highlighted else 36)
    return pen, QBrush(color)
