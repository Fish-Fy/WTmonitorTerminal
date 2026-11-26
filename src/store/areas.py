"""Tactical area geometry shared by editing, persistence, and share codes."""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from uuid import uuid4
from ..i18n import tr


AREA_TYPES = {
    "sniper": "狙击区",
    "engage": "交战区",
    "scout": "侦察区",
    "rally": "出生区（强制点亮）",
    "custom": "自定义区",
}
AREA_SHAPES = {"circle": "圆形", "polygon": "多边形"}
PRECISION = 10000
MAX_AREAS = 128
MAX_VERTICES = 64
MAX_CIRCLE_RADIUS = 8 * PRECISION  # Eight short edges, in quantized units.


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return all(min(x, y) <= z <= max(x, y) for x, y, z in zip(a, b, p))


def _intersects(a, b, c, d):
    abc, abd = _cross(a, b, c), _cross(a, b, d)
    cda, cdb = _cross(c, d, a), _cross(c, d, b)
    return (abc * abd < 0 and cda * cdb < 0
            or abc == 0 and _on_segment(a, b, c)
            or abd == 0 and _on_segment(a, b, d)
            or cda == 0 and _on_segment(c, d, a)
            or cdb == 0 and _on_segment(c, d, b))


def _validate_polygon(vertices):
    if len(set(vertices)) != len(vertices):
        raise ValueError(tr("多边形存在重复顶点"))
    count = len(vertices)
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    for i, (a, b) in enumerate(edges):
        c = vertices[(i + 2) % count]
        if _cross(a, b, c) == 0 and (_on_segment(a, b, c) or _on_segment(b, c, a)):
            raise ValueError(tr("多边形相邻边重叠"))
        for j in range(i + 2, count):
            if (i, j) != (0, count - 1) and _intersects(a, b, *edges[j]):
                raise ValueError(tr("多边形边界自交"))
    if not sum(a[0] * b[1] - b[0] * a[1] for a, b in edges):
        raise ValueError(tr("多边形面积为零"))


def _quantize(value, *, packed, low=0, high=PRECISION):
    if packed:
        if type(value) is int and low <= value <= high:
            return value
    elif type(value) in (int, float) and low / PRECISION <= value <= high / PRECISION:
        return round(value * PRECISION)
    raise ValueError(tr("区域坐标或半径超出范围"))


def _circle_geometry(geometry, *, packed=False, allow_extended=True):
    extra = MAX_CIRCLE_RADIUS if allow_extended else 0
    x, y = (_quantize(value, packed=packed, low=-extra, high=PRECISION + extra)
            for value in geometry[:2])
    radius = _quantize(geometry[2], packed=packed,
                       high=MAX_CIRCLE_RADIUS if allow_extended else PRECISION)
    if radius == 0:
        raise ValueError(tr("圆形区域半径太小"))
    # Without a bottom image we only know that both axes are at least the short edge.
    dx, dy = (max(0, -value, value - PRECISION) for value in (x, y))
    if dx * dx + dy * dy >= radius * radius:
        raise ValueError(tr("圆形区域在地图内没有可见部分"))
    return x, y, radius


def circle_through_points(a, b, c, width, height):
    """Fit a circle in map pixels, then return its quantized normalized geometry."""
    if a == b or b == c or a == c:
        raise ValueError(tr("圆周点不能重复"))
    # Translate to the first point and scale each axis by its image dimension.
    ux, uy = (b[0] - a[0]) * width, (b[1] - a[1]) * height
    vx, vy = (c[0] - a[0]) * width, (c[1] - a[1]) * height
    u2, v2 = ux * ux + uy * uy, vx * vx + vy * vy
    cross = ux * vy - uy * vx
    if abs(cross) <= 1e-12 * max(u2, v2):
        raise ValueError(tr("三个点不能在同一直线上"))
    ox = (u2 * vy - v2 * uy) / (2 * cross)
    oy = (ux * v2 - vx * u2) / (2 * cross)
    x, y = a[0] + ox / width, a[1] + oy / height
    radius = hypot(ox, oy) / min(width, height)
    if radius > MAX_CIRCLE_RADIUS / PRECISION:
        raise ValueError(tr("这三个点画出的圆太大，请换一个圆周点"))
    circle = _normalized("circle", _circle_geometry((x, y, radius)))
    cx, cy, r = circle
    dx = max(0, -cx, cx - 1) * width
    dy = max(0, -cy, cy - 1) * height
    if hypot(dx, dy) >= r * min(width, height):
        raise ValueError(tr("圆形区域在地图内没有可见部分"))
    return circle


def _geometry(kind, shape, geometry, *, packed=False, allow_extended=True):
    if not isinstance(kind, str) or kind not in AREA_TYPES:
        raise ValueError(tr("区域类型无效"))
    if not isinstance(geometry, (list, tuple)):
        raise ValueError(tr("区域边界格式无效"))
    if shape == "circle" and len(geometry) == 3:
        return _circle_geometry(geometry, packed=packed, allow_extended=allow_extended)
    if shape != "polygon":
        raise ValueError(tr("区域形状或边界格式无效"))
    if not 3 <= len(geometry) <= MAX_VERTICES:
        raise ValueError(tr('多边形需要 3 至 {0} 个顶点', MAX_VERTICES))
    vertices = []
    for point in geometry:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(tr("多边形顶点格式无效"))
        vertices.append(tuple(_quantize(value, packed=packed) for value in point))
    _validate_polygon(vertices)
    return tuple(vertices)


def _normalized(shape, geometry):
    if shape == "circle":
        return tuple(value / PRECISION for value in geometry)
    return tuple((x / PRECISION, y / PRECISION) for x, y in geometry)


@dataclass(frozen=True)
class Area:
    id: str
    type: str
    shape: str
    # Circle: (cx, cy, radius / image short side), center may be off-map.
    # Polygon: ((x, y), ...).
    geometry: tuple

    @classmethod
    def new(cls, kind, shape, geometry, *, area_id=None):
        grid = _geometry(kind, shape, geometry)
        return cls(area_id or uuid4().hex[:12], kind, shape, _normalized(shape, grid))

    @classmethod
    def from_dict(cls, record):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError(tr("区域标识缺失"))
        return cls.new(record.get("type"), record.get("shape"), record.get("geometry"),
                       area_id=record["id"])

    def to_dict(self):
        return {"id": self.id, "type": self.type, "shape": self.shape, "geometry": self.geometry}

    @classmethod
    def from_share(cls, record, *, allow_extended=False):
        if not isinstance(record, list) or len(record) != 3:
            raise ValueError(tr("分享码中的区域格式无效"))
        kind, shape, geometry = record
        grid = _geometry(kind, shape, geometry, packed=True, allow_extended=allow_extended)
        return cls(uuid4().hex[:12], kind, shape, _normalized(shape, grid))

    def to_share(self):
        return [self.type, self.shape, _geometry(self.type, self.shape, self.geometry)]
