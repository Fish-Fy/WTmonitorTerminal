"""Directional links shared by the tactical map and the import preview."""
import math
from collections import defaultdict, deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QGraphicsPathItem

from ..i18n import tr
from ..store.models import MARKER_TYPES


def visible_link_pairs(links):
    """Pair enabled opposite directions without changing the stored links."""
    groups = []
    pending = defaultdict(deque)
    for link in links:
        if not link.enabled:
            continue
        opposite = (link.to_id, link.from_id)
        if link.from_id != link.to_id and pending[opposite]:
            pending[opposite].popleft()[1] = link
        else:
            group = [link, None]
            groups.append(group)
            pending[(link.from_id, link.to_id)].append(group)
    return groups


class SchemePathItem(QGraphicsPathItem):
    def __init__(self, link, start, end, source_type, target_type, reverse_link=None):
        super().__init__()
        self._link = link
        self._start = start
        self._end = end
        self._bounds = (min(start.x(), end.x()), min(start.y(), end.y()),
                        max(start.x(), end.x()), max(start.y(), end.y()))
        self._fill_opacity = 1.0
        self.reverse_item = None
        if reverse_link is not None:
            self.reverse_item = SchemePathItem(reverse_link, end, start, target_type, source_type)
            self.reverse_item.setParentItem(self)
            self.reverse_item._pair_item = self
            self.setToolTip(tr("双向连接"))
            self.reverse_item.setToolTip(tr("双向连接"))
        if "walk" in (source_type, target_type):
            self._style = "route"
            self.setBrush(QColor(MARKER_TYPES["walk"][1]))
        elif source_type == "sniper" and target_type in {"engage", "intense"}:
            self._style = "attack"
            self.setBrush(QColor(MARKER_TYPES["sniper"][1]))
        else:
            self._style = "standard"
            self.setBrush(QColor("#111111"))

    def distance_to(self, point, *, max_distance=math.inf):
        """Distance to the continuous link, including the gaps between glyphs."""
        left, top, right, bottom = self._bounds
        if (not left - max_distance <= point.x() <= right + max_distance
                or not top - max_distance <= point.y() <= bottom + max_distance):
            return math.inf
        if self.path().isEmpty() and (self.reverse_item is None or self.reverse_item.path().isEmpty()):
            return math.inf
        delta = self._end - self._start
        offset = point - self._start
        length_squared = QPointF.dotProduct(delta, delta)
        if not length_squared:
            return math.inf
        fraction = max(0.0, min(1.0, QPointF.dotProduct(offset, delta) / length_squared))
        nearest = self._start + delta * fraction
        return math.hypot(point.x() - nearest.x(), point.y() - nearest.y())

    def set_fill_opacity(self, opacity):
        if opacity == self._fill_opacity:
            return
        self._fill_opacity = opacity
        self.update()
        if self.reverse_item is not None:
            self.reverse_item.set_fill_opacity(opacity)

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemSelectedHasChanged and self.reverse_item is not None:
            self.reverse_item.update()
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        pair = getattr(self, "_pair_item", self)
        selected = pair.isSelected()
        if not selected and self._fill_opacity == 1.0:
            super().paint(painter, option, widget)
            return
        pen = self.pen()
        if selected:
            pen.setColor(QColor("#57e9ff"))
        brush = self.brush()
        color = brush.color()
        color.setAlphaF(color.alphaF() * self._fill_opacity)
        brush.setColor(color)
        painter.save()
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawPath(self.path())
        painter.restore()

    def layout(self, zoom, marker_radius):
        """Lay out the glyphs in screen pixels, then map them onto the scene."""
        if self.reverse_item is None:
            self._layout_direction(self._start, self._end, zoom, marker_radius, False)
        else:
            midpoint = (self._start + self._end) / 2
            self._layout_direction(midpoint, self._end, zoom, marker_radius, True)
            self.reverse_item._layout_direction(midpoint, self._start, zoom, marker_radius, True)

    def _layout_direction(self, start, end, zoom, marker_radius, from_middle):
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        gap = 0 if from_middle else marker_radius + 2
        length = math.hypot(dx, dy) * zoom - gap - (marker_radius + 2)
        if length < 2:
            self.setPath(QPainterPath())
            return

        if self._style == "route":
            outline = QColor("#142029")
            path = _chevrons(length)
        else:
            outline = QColor("#fff8ef")
            path = _attack_arrow(length) if self._style == "attack" else _small_arrow(length)

        outline_width = 1.35 if self._style == "standard" else 1.7
        pen = QPen(outline, outline_width / zoom)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)
        transform = QTransform()
        transform.translate(start.x(), start.y())
        transform.rotate(math.degrees(math.atan2(dy, dx)))
        transform.scale(1 / zoom, 1 / zoom)
        transform.translate(gap, 0)
        self.setPath(transform.map(path))


def _chevrons(length):
    path = QPainterPath()
    count = min(80, max(1, math.floor(length / 27)))
    step = length / count
    scale = min(1, step / 21)
    # A pair of angled strokes reads as a double chevron along the route.
    for index in range(count):
        center = step * (index + 0.5)
        for offset in (-4.0, 4.0):
            x = center + offset * scale
            _polygon(path, ((x - 4.2 * scale, -5 * scale), (x - 1 * scale, -5 * scale),
                            (x + 3.5 * scale, 0), (x - 1 * scale, 5 * scale),
                            (x - 4.2 * scale, 5 * scale), (x + .2 * scale, 0)))
    return path


def _attack_arrow(length):
    path = QPainterPath()
    size = min(1, length / 28)
    shoulder = length - 16 * size
    path.moveTo(0, -2.3 * size)
    path.cubicTo(shoulder * .45, -2.6 * size, shoulder * .74, -4.8 * size, shoulder, -5.1 * size)
    path.lineTo(shoulder - 3 * size, -10.5 * size)
    path.lineTo(length, 0)
    path.lineTo(shoulder - 3 * size, 10.5 * size)
    path.lineTo(shoulder, 5.1 * size)
    path.cubicTo(shoulder * .74, 4.8 * size, shoulder * .45, 2.6 * size, 0, 2.3 * size)
    path.closeSubpath()
    return path


def _small_arrow(length):
    path = QPainterPath()
    size = min(1, length / 14)
    shoulder = length - 8 * size
    _polygon(path, ((0, -1.7 * size), (shoulder, -1.7 * size), (shoulder, -4 * size),
                    (length, 0), (shoulder, 4 * size), (shoulder, 1.7 * size), (0, 1.7 * size)))
    return path


def _polygon(path, points):
    path.moveTo(*points[0])
    for x, y in points[1:]:
        path.lineTo(x, y)
    path.closeSubpath()
