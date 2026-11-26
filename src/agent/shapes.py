"""地图图元的尺寸与朝向（纯函数，不碰 Qt）。

**尺寸单位是屏幕像素**，不是场景单位 —— 这是 M 轮踩的大坑：底图实测 2048×2048，
fit 后缩放约 0.46，用"场景像素"给的 7~11px 图元画到屏幕上只剩 3~5px，看着就是"太小"。
游戏自带前端的画法也证实了这一点（index.html）：`x = canvas.width*item['x']` 归一化，
但玩家箭头写死 `w=7.0, l=25.0`、图标 `bold 18pt`，全是**屏幕像素**，不随地图缩放变化。
所以这里全部按屏幕像素给值，由 map_view 用 `1/zoom` 折算成场景单位。

朝向：`/map_obj.json` 的 `dx,dy` 只有极少数对象给（真机 184 个里只有 3 个非零），
地面单位一律是 0 ⇒ 必须用**逐帧位移**当路径切线（见 map_view 的最近邻配对）。
"""
from __future__ import annotations

import math
from typing import Iterable

# ---- 屏幕像素尺寸（照官方前端的量级定）----
PLAYER_LEN = 21.0        # 略小于官方箭头，少遮挡地图细节
PLAYER_HALF_W = 8.5
FRIEND_LEN = 18.0        # 官方单位图标 bold 18pt ≈ 24px, 取略小以免盖住地图
ENEMY_LEN = 14.0
ZONE_DIAM = 26.0         # 占领点图标 ≈ 18pt 字形的大小, 再放大一点便于看
ZONE_LABEL_PX = 14.0     # 占领点字母的**屏幕**字号(跟着 zone_diameter 一起按 1/zoom 折算)
BASE_DOT_R = 3.0
CAPTURE_LINE_W = 3.5
MATCH_MAX_DIST = 0.06    # 归一化距离; 超过就当重生/换人, 不产生朝向


def unit_len(side: str) -> float:
    """友方略大(用户要求看得清), 敌方本轮不动。"""
    if side == "friend":
        return FRIEND_LEN
    if side == "player":
        return PLAYER_LEN
    return ENEMY_LEN


def player_len() -> float:
    return PLAYER_LEN


def zone_diameter() -> float:
    return ZONE_DIAM


def heading_deg(dx, dy) -> float | None:
    """方向向量 -> 屏幕旋转角（Qt setRotation 的度数，0=朝上/北）。"""
    try:
        x = float(dx)
        y = float(dy)
    except (TypeError, ValueError):
        return None
    if x == 0.0 and y == 0.0:
        return None
    return math.degrees(math.atan2(x, -y)) % 360.0


def tangent_deg(p0: tuple[float, float] | None, p1: tuple[float, float] | None) -> float | None:
    if not p0 or not p1:
        return None
    return heading_deg(p1[0] - p0[0], p1[1] - p0[1])


def nearest_prev(cur: tuple[float, float], prev: Iterable[tuple[float, float, str, str]],
                 kind: str, side: str, max_dist: float = MATCH_MAX_DIST
                 ) -> tuple[float, float] | None:
    """在上一帧里找同一类型/阵营、离得最近的那个点（没有稳定 id，只能这样配对）。"""
    best: tuple[float, float] | None = None
    best_d = float("inf")
    for px, py, t, s in prev or ():
        if t != kind or s != side:
            continue
        d = math.hypot(cur[0] - px, cur[1] - py)
        if d < best_d:
            best_d, best = d, (px, py)
    if best is None or best_d > max_dist or best_d <= 1e-6:
        return None                      # 没配上/太远(重生)/原地不动 => 不定向
    return best


def motion_deg(cur: tuple[float, float], prev: Iterable[tuple[float, float, str, str]],
               kind: str, side: str, max_dist: float = MATCH_MAX_DIST) -> float | None:
    """用"上一帧同类型最近点 -> 当前点"的位移当路径切线。"""
    p0 = nearest_prev(cur, prev, kind, side, max_dist)
    return tangent_deg(p0, cur)


def arrow_dart(length: float, half_w: float | None = None) -> list[tuple[float, float]]:
    """朝 -y 的箭头（尖端 (0,-length/2)）。半宽默认按官方比例。"""
    h = float(length) / 2.0
    w = float(half_w) if half_w is not None else h * 0.68
    return [(0.0, -h), (w, h * 0.78), (0.0, h * 0.3), (-w, h * 0.78)]
