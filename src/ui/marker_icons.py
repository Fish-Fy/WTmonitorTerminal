"""地图标点的图标绘制（纯 Qt 绘制，不碰网络）。

用户 2026-10-12 定稿：
  * 每种类型一个**独特符号**（圆底=类型色 + 白描边 + 白色符号 + 黑描边），不靠颜色猜；
  * 输出 **24×24**（先定 32，随后用户要求"缩小一些"，免得压住地图）；用 4× 超采样(128²)
    绘制再平滑缩放，线宽按比例加粗到 24px 下仍有 ~2px，所以收小后照样清楚；
  * 侦察点使用望远镜，路线点使用双箭头 »。

要加新类型: 在 models.MARKER_TYPES 注册后, 在 _draw_symbol() 加一支即可。
"""
from __future__ import annotations

import math
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QPolygonF

from ..store.models import MARKER_TYPES

PX = 128            # 超采样画布(4×): 缩到目标像素才干净
OUT_PX = 24         # 屏幕上的图标尺寸(用户 2026-10-12 二次定稿: 32 略挡地图, 收到 24)
RING = 12.0         # 白描边宽度(超采样坐标) ≈ 24px 下的 2.25px
STROKE = 10.0       # 符号线宽(超采样) ≈ 24px 下的 1.9px(缩小后更要保证看得见)
INK = QColor(18, 18, 20, 235)          # 符号黑描边
FILL = QColor("#ffffff")


@lru_cache(maxsize=256)
def marker_pixmap(type_: str, out_px: int = OUT_PX) -> QPixmap:
    """按类型画图标; 不认识的类型回落到「自定义」的圆点。out_px 是**最终像素尺寸**。

    带 lru_cache: 地图缩放会反复要同一批 (类型, 尺寸) 组合, 缓存后滚轮缩放几乎零成本。
    QPixmap 是隐式共享的只读值, 多处复用同一个实例是安全的。
    """
    _name, color_hex, _shape = MARKER_TYPES.get(type_, ("自定义", "#8e8e93", "dot"))
    img = QImage(PX, PX, QImage.Format.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(color_hex)))
        p.setPen(QPen(QColor("#ffffff"), RING))
        d = PX - RING - 4
        p.drawEllipse(QPointF(PX / 2, PX / 2), d / 2, d / 2)
        p.setBrush(QBrush(FILL))
        p.setPen(QPen(INK, STROKE, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        _draw_symbol(p, type_, PX / 2, PX / 2, d * 0.30)
    finally:
        p.end()
    return QPixmap.fromImage(img).scaled(
        out_px, out_px, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation)


def _star(cx: float, cy: float, r: float, n: int, inner: float = 0.42) -> list[QPointF]:
    pts = []
    for i in range(n * 2):
        ang = math.pi / n * i - math.pi / 2
        rr = r if i % 2 == 0 else r * inner
        pts.append(QPointF(cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


def _draw_symbol(p: QPainter, key: str, cx: float, cy: float, r: float) -> None:
    """r = 符号外接半径(超采样坐标)。线条都偏粗, 以保证缩到 32px 后仍清楚。"""
    if key == "sniper":            # 十字准星 ⊕
        p.drawEllipse(QPointF(cx, cy), r * 0.60, r * 0.60)
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            p.drawLine(QPointF(cx + dx * r * 0.26, cy + dy * r * 0.26),
                       QPointF(cx + dx * r, cy + dy * r))

    elif key == "engage":          # 爆点(8 角)
        p.drawPolygon(QPolygonF(_star(cx, cy, r, 8, inner=0.48)))

    elif key == "intense":         # 交火激烈(16 角锯齿)
        p.drawPolygon(QPolygonF(_star(cx, cy, r, 16, inner=0.55)))

    elif key == "scout":           # 望远镜: 两个镜筒 + 中间横梁(32px 下是清晰的 ∞ 形)
        rr = r * 0.58
        p.drawEllipse(QPointF(cx - r * 0.46, cy + r * 0.06), rr, rr)
        p.drawEllipse(QPointF(cx + r * 0.46, cy + r * 0.06), rr, rr)
        p.drawRoundedRect(QRectF(cx - r * 0.50, cy - r * 0.30, r * 1.00, r * 0.46),
                          r * 0.16, r * 0.16)

    elif key == "rally":           # 旗子 ⚑
        p.drawLine(QPointF(cx - r * 0.44, cy - r), QPointF(cx - r * 0.44, cy + r))
        p.drawPolygon(QPolygonF([QPointF(cx - r * 0.44, cy - r),
                                 QPointF(cx + r * 0.86, cy - r * 0.56),
                                 QPointF(cx - r * 0.44, cy - r * 0.12)]))

    elif key == "walk":            # 路线点：双箭头 »
        for dx in (-0.42, 0.20):
            p.drawLine(QPointF(cx + (dx - 0.26) * r, cy - r * 0.72),
                       QPointF(cx + (dx + 0.22) * r, cy))
            p.drawLine(QPointF(cx + (dx + 0.22) * r, cy),
                       QPointF(cx + (dx - 0.26) * r, cy + r * 0.72))

    elif key == "ambush":          # 伏击(旧类型, 菜单里已隐藏但旧方案要能显示): 闪电
        p.drawPolygon(QPolygonF([
            QPointF(cx + r * 0.15, cy - r), QPointF(cx - r * 0.55, cy + r * 0.05),
            QPointF(cx + r * 0.05, cy + r * 0.05), QPointF(cx - r * 0.15, cy + r),
            QPointF(cx + r * 0.55, cy - r * 0.05), QPointF(cx - r * 0.05, cy - r * 0.05)]))

    elif key == "artillery":       # 炮击落点(旧类型保留画法)
        p.drawEllipse(QPointF(cx, cy - r * 0.10), r * 0.52, r * 0.52)
        p.drawLine(QPointF(cx, cy - r * 0.10), QPointF(cx, cy + r))
        for dx in (-0.30, 0.30):
            p.drawLine(QPointF(cx + dx * r, cy + r * 0.58), QPointF(cx, cy + r))

    elif key == "flank":           # 迂回(旧类型保留画法): 弧 + 箭头
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 30 * 16, 290 * 16)
        ang = math.radians(30)
        tip = QPointF(cx + r * math.cos(ang), cy + r * math.sin(ang))
        back = math.radians(6)
        b1 = QPointF(cx + (r - 10) * math.cos(back), cy + (r - 10) * math.sin(back))
        b2 = QPointF(cx + (r + 10) * math.cos(back), cy + (r + 10) * math.sin(back))
        p.drawPolygon(QPolygonF([tip, b1, b2]))

    elif key == "supply":          # 补给(旧类型保留画法): 箱子
        p.drawRect(QRectF(cx - r * 0.76, cy - r * 0.52, r * 1.52, r * 1.04))
        p.drawArc(QRectF(cx - r * 0.30, cy - r * 0.84, r * 0.60, r * 0.60), 0, 180 * 16)
        p.drawLine(QPointF(cx, cy - r * 0.52), QPointF(cx, cy + r * 0.52))

    else:                          # 自定义: 圆点(放大一号, 32px 下别太弱)
        p.drawEllipse(QPointF(cx, cy), r * 0.42, r * 0.42)
