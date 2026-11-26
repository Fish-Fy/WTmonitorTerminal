"""可活动区域的自适应数学（纯函数，不碰 Qt，方便单测）。

用户要求：每次换图都把"可活动区域"自动对齐 —— 视图必须是**正方形**，且视口内一个
边长为可调比例的同心正方形包住当前任务的出生点和占领区。
个人标点、移动单位、机场及其它任务目标不参与范围计算。
默认 80%，即两侧合计留 20% 余量。
"""
from __future__ import annotations

from typing import Iterable
import math

from .mapobj import MapObj


def mission_activity_points(objs: Iterable[MapObj]) -> list[tuple[float, float]]:
    """只使用本帧出生点/占领区；不合并同底图的历史任务、方案或缓存。

    出生点按 respawn_base 前缀识别，占领区兼容 type/icon 两种表示。
    极远处及无效坐标不属于这张底图，不能用来扩大视野。
    """
    points: set[tuple[float, float]] = set()

    def keep(x, y):
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError, OverflowError):
            return
        if math.isfinite(x) and math.isfinite(y) and -0.05 <= x <= 1.05 and -0.05 <= y <= 1.05:
            points.add((x, y))

    for obj in objs:
        kind, icon = obj.type.lower(), obj.icon.lower()
        if not (kind.startswith("respawn_base") or icon.startswith("respawn_base")
                or kind == "capture_zone" or icon == "capture_zone"):
            continue
        if obj.is_segment:
            keep(obj.sx, obj.sy)
            keep(obj.ex, obj.ey)
        else:
            keep(obj.x, obj.y)
    return sorted(points)


def activity_bbox(points: Iterable[tuple[float, float]] | None) -> tuple[float, float, float, float] | None:
    """所有元素的包围盒，**扩成正方形**（取较长的一边），无点返回 None。"""
    xs: list[float] = []
    ys: list[float] = []
    for p in points or ():
        try:
            x, y = p
            xs.append(float(x)); ys.append(float(y))
        except (TypeError, ValueError):
            continue
    if not xs:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    side = max(w, h)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (cx - side / 2.0, cy - side / 2.0, cx + side / 2.0, cy + side / 2.0)


MIN_SIDE = 0.02       # 可视正方形的最小边长(归一化): 只有一个点/所有点重合时不能给出 0
DEFAULT_ACTIVITY_FILL = 0.80
ACTIVITY_FILL_RANGE = (0.30, 1.0)
DEFAULT_FOLLOW_ZOOM = 1.0
FOLLOW_ZOOM_RANGE = (1.0, 10.0)


def fit_square(bbox: tuple[float, float, float, float], ratio: float = DEFAULT_ACTIVITY_FILL
               ) -> tuple[float, float, float]:
    """返回 (中心x, 中心y, 可视正方形边长)，使 bbox 刚好是可视区的 `ratio` 倍。

    边长有下限: 真机某些帧只剩一个单位(或所有单位重合在同一格), 包围盒退化成点 ⇒
    这里若返回 0, map_view._apply_fit 的 `square/(side*img_w)` 就除零崩掉。
    """
    if not (0.0 < ratio <= 1.0):
        raise ValueError(f"ratio 必须在 (0, 1] 内, 收到 {ratio!r}")
    x0, y0, x1, y1 = bbox
    side = max(max(x1 - x0, y1 - y0) / ratio, MIN_SIDE)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0, side)
