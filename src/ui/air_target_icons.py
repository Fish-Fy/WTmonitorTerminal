"""地图目标、载具及方向提示的矢量符号。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainterPath, QPen, QBrush, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsItemGroup, QGraphicsPathItem


def target_kind(obj) -> str | None:
    """只处理明确给出类别的目标，不把普通地面载具当成碉堡。"""
    for value in (obj.type, obj.icon):
        kind = (value or "").strip().lower()
        if kind in ("bombing_point", "defending_point", "pillbox"):
            return kind
        if kind.replace("_", "").replace("-", "") in ("lightpillbox", "heavypillbox"):
            return "pillbox"
    return None


def ground_symbol_kind(obj) -> str | None:
    """8111 的火炮/防空类别是地面目标，不能落入友方飞机箭头分支。"""
    if (obj.type or "").strip().lower() != "ground_model":
        return None
    kind = (obj.icon or "").strip().lower()
    if kind == "player":
        return None
    return {"artillery": "artillery", "tankdestroyer": "tank_destroyer",
            "spaa": "spaa", "sam": "sam", "airdefence": "spaa",
            "lighttank": "light_tank", "mediumtank": "medium_tank",
            "heavytank": "heavy_tank", "ifv": "ifv", "wheeled": "wheeled"
            }.get(kind.replace("_", "").replace("-", ""), "ground_unknown")


def _path_item(group, path, fill=None, stroke=None, width=1.0):
    item = QGraphicsPathItem(path)
    item.setBrush(QBrush(QColor(fill)) if fill else Qt.NoBrush)
    pen = QPen(QColor(stroke), width) if stroke else QPen(Qt.NoPen)
    pen.setJoinStyle(Qt.MiterJoin)
    pen.setCapStyle(Qt.FlatCap)
    item.setPen(pen)
    group.addToGroup(item)
    return item


def make_heading_ring(heading: float) -> QGraphicsItemGroup:
    """绘制亮色尖端和分段蓝弧，屏幕大小固定。"""
    group = QGraphicsItemGroup()
    group.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    group.setAcceptedMouseButtons(Qt.NoButton)
    group.setData(0, "teammate_heading")
    radius = 12.0
    bounds = QRectF(-radius, -radius, 2 * radius, 2 * radius)
    rear = QPainterPath()
    for start, sweep in ((150, 54), (218, 55), (287, 47), (348, 42)):
        rear.arcMoveTo(bounds, start)
        rear.arcTo(bounds, start, sweep)
    # 尖端占圆心角 120°，两边与圆相切。交点距圆心为 r / cos(60°)。
    tip = QPainterPath(rear.currentPosition())
    tip.lineTo(0, -2 * radius)
    tip.lineTo(rear.pointAtPercent(0))
    # 直接旋转绘制路径，避免 addToGroup 保留子项场景朝向而抵消组的旋转。
    rotation = QTransform().rotate(heading)
    for path, color, edge_width, line_width in ((rear, "#779ec0", 2.75, 1.15),
                                                (tip, "#e3f3ff", 3.3, 1.8)):
        rotated = rotation.map(path)
        for ink, width in (("#101c2b", edge_width), (color, line_width)):
            item = _path_item(group, rotated, stroke=ink, width=width)
            pen = item.pen()
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            item.setPen(pen)
            item.setAcceptedMouseButtons(Qt.NoButton)
    return group


def make_spawn_item(color: str, *, large: bool = False) -> QGraphicsItemGroup:
    """侧面坦克出生点，炮管朝左；局内 18px，公开任务的基地 36px。"""
    group = QGraphicsItemGroup()
    group.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    group.setData(0, "spawn")
    points = [(-22, -7), (-5, -7), (-3, -9), (3, -9), (3, -10),
              (7, -10), (7, -8), (11, -7), (12, -2), (20, -2),
              (22, 3), (18, 8), (-12, 8), (-18, 4), (-18, -1),
              (-9, -1), (-8, -4), (-22, -4)]
    silhouette = QPainterPath()
    silhouette.moveTo(*points[0])
    for point in points[1:]:
        silhouette.lineTo(*point)
    silhouette.closeSubpath()
    scale = (36 if large else 18) / 44
    silhouette = QTransform.fromScale(scale, scale).map(silhouette)
    _path_item(group, silhouette, fill=color, stroke="#101010", width=.9)
    return group


def make_target_item(kind: str, color: str) -> QGraphicsItemGroup:
    """几何以屏幕像素定义，地图缩放时固定目标符号保持大小和朝向。"""
    group = QGraphicsItemGroup()
    group.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
    group.setData(0, kind)
    if kind == "bombing_point":
        # 两层圆环、四段准星和中心点；没有网页图标的下垂尖角。
        ink = QPainterPath()
        ink.addEllipse(QPointF(0, 0), 10.5, 10.5)
        ink.addEllipse(QPointF(0, 0), 5.5, 5.5)
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            ink.moveTo(dx * 3.5, dy * 3.5)
            ink.lineTo(dx * 12, dy * 12)
        _path_item(group, ink, stroke="#101010", width=3.2)
        _path_item(group, ink, stroke=color, width=1.6)
        dot = QPainterPath()
        dot.addRect(QRectF(-1, -1, 2, 2))
        _path_item(group, dot, fill=color)
    elif kind == "defending_point":
        # 8111 未提供各战区的受损比例，统一全蓝，不伪造参考截图中的受损进度。
        ring = QPainterPath()
        ring.addEllipse(QPointF(0, 0), 10.5, 10.5)
        _path_item(group, ring, stroke="#101010", width=3)
        _path_item(group, ring, stroke=color, width=1.6)
        shield = QPainterPath()
        shield.moveTo(0, -7)
        shield.cubicTo(-2, -5.5, -4.5, -5.5, -6, -5.5)
        shield.lineTo(-5, 2)
        shield.cubicTo(-4.5, 4, -2, 6.7, 0, 8)
        shield.cubicTo(2, 6.7, 4.5, 4, 5, 2)
        shield.lineTo(6, -5.5)
        shield.cubicTo(4.5, -5.5, 2, -5.5, 0, -7)
        shield.closeSubpath()
        _path_item(group, shield, fill=color, stroke="#101010", width=.8)
    elif kind == "pillbox":
        # 两侧高垛口、中央低垛口和完整底座，固定设施不绘制载具箭头。
        castle = QPainterPath()
        points = [(-7, 5), (-7, 1), (-5, 1), (-5, -6), (-2.5, -6),
                  (-2.5, -1), (-1, -1), (-1, -4), (1, -4), (1, -1),
                  (2.5, -1), (2.5, -6), (5, -6), (5, 1), (7, 1), (7, 5)]
        castle.moveTo(*points[0])
        for point in points[1:]:
            castle.lineTo(*point)
        castle.closeSubpath()
        _path_item(group, castle, fill=color, stroke="#000000", width=1)
    elif kind in ("wheeled", "ground_unknown"):
        # 当前空战任务的 Wheeled 是小圆点。未知地面类也不冒充飞机或玩家。
        dot = QPainterPath()
        radius = 2.7 if kind == "wheeled" else 3.5
        dot.addEllipse(QPointF(0, 0), radius, radius)
        _path_item(group, dot, fill=color, stroke="#000000", width=1)
    elif kind == "ifv":
        body = QPainterPath()
        body.addRect(QRectF(-5.5, -2.5, 11, 5))
        _path_item(group, body, fill=color, stroke="#000000", width=1)
        turret = QPainterPath()
        turret.addEllipse(QPointF(0, 0), 2.3, 2.3)
        _path_item(group, turret, fill=color, stroke="#000000", width=1)
    elif kind in ("artillery", "tank_destroyer", "spaa", "sam", "light_tank", "medium_tank", "heavy_tank"):
        # 2026-10-30 用户提供的游戏内五类符号：黑描边的平面轮廓，敌我只变填色。
        if kind in ("spaa", "sam"):
            points = [(-6.5,5),(-6.5,1),(-5.5,1),(-5.5,-5),(-1.5,-5),
                      (-1.5,1),(1.5,1),(1.5,-5),(5.5,-5),(5.5,1),(6.5,1),(6.5,5)]
        elif kind == "light_tank":
            points = [(-6.5,-2.5),(6.5,-2.5),(6.5,2.5),(-6.5,2.5)]
        elif kind == "medium_tank":
            points = [(-6.5,-4),(6.5,-4),(6.5,4),(3.5,4),(3.5,0),
                      (-3.5,0),(-3.5,4),(-6.5,4)]
        elif kind == "heavy_tank":
            points = [(-6.5,-2),(-3.5,-2),(-3.5,-5),(3.5,-5),(3.5,-2),
                      (6.5,-2),(6.5,5),(3.5,5),(3.5,2),(-3.5,2),(-3.5,5),(-6.5,5)]
        else:  # 火炮和坦克歼击车共用斜向炮管的轮廓。
            points = [(-6.5,5),(-6.5,0),(-.5,-6),(3.5,-6),(-1.5,0),(6.5,0),(6.5,5)]
        symbol = QPainterPath()
        symbol.moveTo(*points[0])
        for point in points[1:]:
            symbol.lineTo(*point)
        symbol.closeSubpath()
        _path_item(group, symbol, fill=color, stroke="#000000", width=1)
    else:
        raise ValueError(f"未知空战目标图标: {kind}")
    return group
