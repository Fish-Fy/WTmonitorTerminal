"""One in-progress circle or polygon in the tactical map view."""
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem, QGraphicsView

from ..i18n import tr
from ..store.areas import AREA_TYPES, MAX_AREAS, MAX_VERTICES, Area, circle_through_points
from .area_items import area_path, area_style


class AreaEditor:
    def __init__(self, view, kind, mode, existing):
        self.view = view
        self.kind = kind
        self.three_points = mode == "circle_three_points"
        self.shape = "circle" if self.three_points else mode
        self.existing = existing
        self.scheme = view.current_scheme()
        self.vertices = []
        self.circle = None
        self.pressed = None
        self.center = None
        self.preview = QGraphicsPathItem()
        self.preview.setZValue(1.5)
        pen, brush = area_style(kind, highlighted=True)
        self.preview.setPen(pen)
        self.preview.setBrush(brush)
        self.preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        view.scene().addItem(self.preview)
        self.point_items = []
        if self.three_points:
            for _ in range(3):
                dot = QGraphicsEllipseItem(-3, -3, 6, 6, self.preview)
                dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                dot.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                dot.setPen(QPen(QColor("#ffffff"), 1))
                dot.setBrush(QBrush(pen.color()))
                dot.hide()
                self.point_items.append(dot)
        self.mouse_tracking = view.viewport().hasMouseTracking()
        view.viewport().setMouseTracking(True)
        view.setDragMode(QGraphicsView.DragMode.NoDrag)
        view.setFocus(Qt.FocusReason.MouseFocusReason)
        if self.three_points:
            self._hint_next_point()
        else:
            instructions = (tr("拖拽画圆 · Esc 取消") if self.shape == "circle" else
                            tr("点击添加顶点 · Enter 保存 · Backspace 撤回 · Esc 取消"))
            view._hint(f"{tr(AREA_TYPES[kind])} · {instructions}")

    def _hint_next_point(self, error=None):
        prefix = error or tr(AREA_TYPES[self.kind])
        self.view._hint(tr('{0} · 点击第 {1} 个圆周点 · Backspace 撤回 · Esc 取消', prefix, len(self.vertices) + 1))

    def _circle_through(self, point):
        return circle_through_points(*self.vertices, point, self.view._img_w, self.view._img_h)

    def close(self):
        self.view.scene().removeItem(self.preview)
        self.view.viewport().setMouseTracking(self.mouse_tracking)
        self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def _map_point(self, viewport_pos):
        p = self.view.mapToScene(viewport_pos)
        return p.x() / self.view._img_w, p.y() / self.view._img_h

    @staticmethod
    def _inside(point):
        return all(0 <= value <= 1 for value in point)

    def press(self, position):
        self.view.setFocus(Qt.FocusReason.MouseFocusReason)
        point = self._map_point(position)
        self.pressed = position if self._inside(point) else None
        if self.shape == "circle" and not self.three_points and self.pressed is not None:
            self.center = point
            self.move(position)

    def move(self, position):
        point = self._map_point(position)
        if self.shape == "circle" and not self.three_points and self.pressed is not None:
            cx, cy = self.center
            w, h = self.view._img_w, self.view._img_h
            radius = math.hypot((point[0] - cx) * w, (point[1] - cy) * h) / min(w, h)
            self.circle = cx, cy, radius
        self.redraw(point if self._inside(point) else None)

    def release(self, position):
        if self.pressed is None:
            return
        self.move(position)
        pressed, self.pressed = self.pressed, None
        if self.shape == "circle" and not self.three_points:
            self.commit()
            return
        if (position - pressed).manhattanLength() <= 4:
            point = self._map_point(position)
            if self.three_points:
                self._add_circle_point(point)
                return
            if self._inside(point) and len(self.vertices) < MAX_VERTICES:
                self.vertices.append(point)
            elif len(self.vertices) == MAX_VERTICES:
                self.view._hint(tr('最多 {0} 个顶点 · Enter 保存 · Backspace 撤回', MAX_VERTICES))
        self.redraw()

    def _add_circle_point(self, point):
        if not self._inside(point) or len(self.vertices) == 3:
            self.redraw()
            return
        if point in self.vertices:
            self._hint_next_point(tr("圆周点不能重复"))
            self.redraw()
            return
        if len(self.vertices) == 2:
            try:
                self.circle = self._circle_through(point)
            except ValueError as exc:
                self._hint_next_point(str(exc))
                self.redraw()
                return
        self.vertices.append(point)
        self.redraw()
        if self.circle is None:
            self._hint_next_point()
        else:
            self.commit()

    def redraw(self, cursor=None):
        geometry = self.circle if self.shape == "circle" else self.vertices
        if self.shape == "polygon" and geometry and cursor is not None:
            geometry = [*geometry, cursor]
        if self.three_points and len(self.vertices) == 2 and cursor is not None:
            try:
                geometry = self._circle_through(cursor)
            except ValueError:
                pass  # A third point that cannot be saved has no circle preview.
        w, h = self.view._img_w, self.view._img_h
        path = area_path(self.shape, geometry, w, h) if geometry else QPainterPath()
        if self.shape == "circle" and geometry:
            frame = QPainterPath()
            frame.addRect(QRectF(0, 0, w, h))
            path = path.intersected(frame)
        self.preview.setPath(path)
        for i, dot in enumerate(self.point_items):
            if i < len(self.vertices):
                x, y = self.vertices[i]
                dot.setPos(x * w, y * h)
                dot.show()
            else:
                dot.hide()

    def undo(self):
        if (self.three_points or self.shape == "polygon") and self.vertices:
            self.vertices.pop()
            if self.three_points:
                self.circle = None
                self._hint_next_point()
            self.redraw()

    def commit(self):
        if self.pressed is not None or self.view.current_scheme() is not self.scheme:
            return
        if self.three_points and self.circle is None:
            self._hint_next_point(tr("需要三个圆周点"))
            return
        try:
            if self.existing is None and len(self.scheme.areas) >= MAX_AREAS:
                raise ValueError(tr('每套方案最多保存 {0} 个区域', MAX_AREAS))
            if self.shape == "circle" and not self.three_points and self.circle and self.circle[2] > 1:
                raise ValueError(tr("区域坐标或半径超出范围"))
            area = Area.new(self.kind, self.shape,
                            self.circle if self.shape == "circle" else self.vertices,
                            area_id=self.existing.id if self.existing else None)
        except ValueError as exc:
            edit = (tr("Backspace 撤回") if self.three_points else
                    tr("重新拖拽") if self.shape == "circle" else tr("Backspace 撤回或继续添加顶点"))
            self.view._hint(tr('{0} · {1} · Esc 取消', exc, edit))
            return
        if self.view._save_area_change(self.existing, area):
            self.view.cancel_area_edit()
            self.view._hint(tr("区域已保存"), 3000)
        elif self.view._area_editor is self:
            undo = tr(" · Backspace 撤回") if self.three_points else ""
            self.view._hint(tr('区域未保存 · Enter 重试{0} · Esc 取消', undo))
