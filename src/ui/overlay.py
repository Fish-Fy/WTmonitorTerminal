"""地图视口内的叠加层：姿态仪 + 按需勾选的仪表读数 + 在线徽标。

它是 QGraphicsView.viewport() 的**子控件**（不是场景图元），所以：
  * 不随地图缩放/平移而变形 —— 永远贴在视口右上角(姿态仪)与左上角(仪表栈)；
  * 对鼠标完全透明（WA_TransparentForMouseEvents），不挡住地图的拖拽/右键。

姿态数值先经 telemetry.approach* 一阶平滑再画：5~10Hz 的数据在 30fps 下才不会一跳一跳；
罗盘用最短弧插值（358°→2° 走 0° 近路）。数据断流超过 stale_after 秒 → 整块转灰并标"无信号"，
绝不冻结最后一次的读数冒充实时。
"""
from __future__ import annotations

import math
import time
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..i18n import tr
from ..agent.telemetry import Attitude, StateSample, approach, approach_angle
from ..agent.fuel import FuelEndurance, format_endurance

# 叠加项: key -> (标签, 单位, 取值器)
_GAUGES: tuple[tuple[str, str, str], ...] = (
    ("ias", "指示空速", " km/h"),
    ("tas", "真空速", " km/h"),
    ("mach", "马赫", ""),
    ("alt", "高度", " m"),
    ("vario", "升降率", " m/s"),
    ("heading", "航向", " °"),
    ("aoa", "攻角", "°"),
    ("aos", "侧滑", "°"),
    ("ny", "过载", " G"),
    ("roll_rate", "滚转率", " °/s"),
    ("throttle", "油门", " %"),
    ("fuel", "剩余燃油", " kg"),
)
ITEM_LABELS: dict[str, str] = {"attitude": "姿态仪", "conn": "在线徽标",
                               **{k: lab for k, lab, _u in _GAUGES}}
# 整组载具状态显示只用于飞机，包括连接徽标。
AIR_ONLY: frozenset[str] = frozenset(ITEM_LABELS)
_MARGIN = 8
_PX_PER_DEG = 2.2       # 姿态仪里 1° 俯仰占多少像素


def attitude_rect(view_w: int, view_h: int, size: int = 148, margin: int = _MARGIN) -> QRect:
    """姿态仪放在视口**右上角**（与地图缩放无关，纯视口坐标）。"""
    s = int(max(72, min(size, view_h - 2 * margin) if view_h > 2 * margin else size))
    return QRect(int(view_w) - margin - s, int(margin), s, s)


STRIP_MIN = 60      # 下方余量至少这么高才把仪表排进条里


def hud_item_rects(view_w: int, view_h: int, strip_h: int, items, size: int = 148,
                   margin: int = _MARGIN, gap: int = 6) -> dict[str, QRect]:
    """统一计算真正绘制的卡片矩形：底部空白优先，放不下则角落分列。

    姿态仪独占正方形，数字卡片按飞行/导航/操纵/动力顺序自动换行。
    不再给每个数字虚占一个姿态仪的宽度，也不在计算后额外平移文字。
    """
    keys = [k for k in ["attitude", *[g[0] for g in _GAUGES], "conn"] if k in (items or ())]
    width, height = max(0, int(view_w) - 2 * margin), max(0, int(view_h) - 2 * margin)
    if not keys or width < 1 or height < 1:
        return {}
    values = [k for k in keys if k != "attitude"]
    out = {}
    strip = min(max(0, int(strip_h)), int(view_h))
    area = QRect(margin, margin, width, height)
    if strip >= STRIP_MIN:
        sh = strip - 2 * margin
        side = min(int(size), sh, width) if "attitude" in keys else 0
        dx = side + gap if side else 0
        cols = max(1, (width - dx + gap) // (140 + gap))
        rows = math.ceil(len(values) / cols)
        if (width - dx >= 110 and rows * 46 + max(0, rows - 1) * gap <= sh) or not values:
            top = int(view_h) - strip + margin
            if side:
                out["attitude"] = QRect(margin, top, side, side)
            area = QRect(margin + dx, top, width - dx, sh)
            return _hud_grid(out, area, values, cols, gap)
    if "attitude" in keys:
        side = min(int(size), width, height, 96 if height < 300 and values else int(size))
        out["attitude"] = QRect(int(view_w) - margin - side, margin, side, side)
        left_width = width - side - gap
        if left_width >= 110:
            area.setWidth(left_width)
        else:
            area = QRect(margin, margin + side + gap, width, max(0, height - side - gap))
    max_cols = max(1, (area.width() + gap) // (110 + gap))
    max_rows = max(1, (area.height() + gap) // (46 + gap))
    cols = min(max_cols, max(2 if len(values) > 6 else 1, math.ceil(len(values) / max_rows)))
    return _hud_grid(out, area, values, cols, gap)


def _hud_grid(out, area, keys, cols, gap):
    if not keys or area.width() <= 0 or area.height() <= 0:
        return out
    rows = math.ceil(len(keys) / cols)
    cell_w = min(180, max(1, (area.width() - gap * (cols - 1)) // cols))
    cell_h = min(46, max(1, (area.height() - gap * (rows - 1)) // rows))
    for i, key in enumerate(keys):
        out[key] = QRect(area.x() + (i % cols) * (cell_w + gap),
                         area.y() + (i // cols) * (cell_h + gap), cell_w, cell_h)
    return out


class MapOverlay(QWidget):
    def __init__(self, cfg, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        at = getattr(cfg, "attitude", None)
        self.size_px = int(getattr(at, "size", 148))
        self.tau = float(getattr(at, "smooth_tau", 0.08))
        self.stale_after = float(getattr(at, "stale_after", 1.5))
        self.invert_pitch = bool(getattr(at, "invert_pitch", True))
        self.invert_roll = bool(getattr(at, "invert_roll", True))
        self.aircraft_tilt = False

        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        self._clock: Callable[[], float] = time.monotonic
        self._att: Attitude | None = None
        self._st: StateSample | None = None
        self._fuel_endurance = FuelEndurance()
        self._online = False
        self.flight_active = False
        self._last_rx = 0.0
        self._tick_t = self._clock()
        self._shown_pitch = 0.0
        self._shown_roll = 0.0
        self._shown_heading = 0.0

        # 默认只开姿态仪 —— 其余按需勾选（用户要求：空间留给地图）
        self.items: set[str] = set(self._default_items(at))
        self._strip_h = 0            # 地图下方留给仪表的余量高度(由 MapView 上报)

        self._timer = QTimer(self)
        self._timer.setInterval(33)            # ~30fps 只跑插值, 不发网络请求
        self._timer.timeout.connect(self._on_timer)
        self.hide()

    @staticmethod
    def _default_items(at) -> Iterable[str]:
        show = getattr(at, "show", None)
        if not show:
            return {"attitude"}
        return {str(x) for x in show if str(x) in ITEM_LABELS}

    # ------------------------------------------------------------- 外部接口
    def set_aircraft_tilt(self, enabled: bool) -> None:
        self.aircraft_tilt = bool(enabled)
        self.update()

    def set_flight_active(self, active: bool) -> None:
        active = bool(active)
        if active != self.flight_active:
            self._fuel_endurance.reset()
            self._att = None
            self._st = None
            self._last_rx = 0.0
            self._shown_pitch = self._shown_roll = self._shown_heading = 0.0
        self.flight_active = active
        self.setVisible(active)
        self.update()

    def set_clock(self, fn: Callable[[], float]) -> None:
        """测试注入假时钟。"""
        self._clock = fn
        self._tick_t = fn()

    def set_online(self, online: bool) -> None:
        self._online = bool(online)
        if not online:
            self._fuel_endurance.reset()
        self.update()

    def set_strip(self, height: int) -> None:
        """地图下方余量高度(px)。>=STRIP_MIN 时仪表排进那条里（见 hud_item_rects）。"""
        h = int(max(0, height or 0))
        if h != self._strip_h:
            self._strip_h = h
            self.update()

    def set_attitude(self, att: Attitude | None) -> None:
        if att is not None and att.valid:
            self._att = att
            self._last_rx = self._clock()
        elif att is None:
            self._att = None
        self.update()

    def set_telemetry(self, st: StateSample | None) -> None:
        if st is not None and st.valid:
            self._st = st
            self._last_rx = self._clock()
            self._fuel_endurance.update(st.fuel_kg, self._last_rx, st.fuel0_kg)
        else:
            self._fuel_endurance.reset()
        self.update()

    def toggle(self, item: str) -> None:
        if item not in ITEM_LABELS:
            return                       # 不认识的项: 忽略, 不炸也不显示
        if item in self.items:
            self.items.discard(item)
        else:
            self.items.add(item)
        self.update()

    def is_on(self, item: str) -> bool:
        return item in self.items

    @property
    def stale(self) -> bool:
        if self._att is None:
            return True
        dt = self._clock() - self._last_rx
        # 断流太久、或时钟出现回跳(换过时间源): 一律宁可显示"无信号", 不拿旧读数冒充实时
        return dt > self.stale_after or dt < 0

    def tick(self) -> None:
        """推进一帧插值（测试里手动调，平时由内部 QTimer 调）。"""
        now = self._clock()
        dt = max(0.0, now - self._tick_t)
        self._tick_t = now
        if self._att is not None and self._att.valid:
            self._shown_pitch = approach(self._shown_pitch, self._att.pitch, dt, self.tau)
            self._shown_roll = approach(self._shown_roll, self._att.roll, dt, self.tau)
            self._shown_heading = approach_angle(self._shown_heading, self._att.heading, dt, self.tau) \
                if self._shown_heading is not None else self._att.heading
        self.update()

    def showEvent(self, e) -> None:
        self._tick_t = self._clock()
        self._timer.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def _on_timer(self) -> None:
        self.tick()

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, _e) -> None:
        if not self.flight_active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        rects = hud_item_rects(w, h, self._strip_h, self.items, self.size_px, _MARGIN)
        att_rect = rects.get("attitude")
        if "attitude" in self.items and w > 2 * _MARGIN and att_rect is not None:
            self._paint_attitude(p, att_rect)
        for key, label, unit in _GAUGES:
            if key in rects:
                value = self._gauge_value(key)
                label = tr(label)
                if key == "fuel":
                    self._paint_card(p, rects[key], label, f"{value}{unit}" if value else "—",
                                     companion=(tr("预计续航"), self.fuel_time_text()),
                                     fresh=self._fuel_endurance.fresh(self._clock()))
                else:
                    self._paint_card(p, rects[key], label, f"{value}{unit}" if value else "—")
        if "conn" in rects:
            self._paint_card(p, rects["conn"], tr("连接状态"), tr("在线") if self._online else tr("离线"))
        p.end()

    def _panel(self, p: QPainter, r: QRect, alpha: int = 130) -> None:
        path = QPainterPath()
        path.addRoundedRect(float(r.x()), float(r.y()), float(r.width()), float(r.height()), 7, 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(path, QColor(8, 10, 14, alpha))

    def _paint_attitude(self, p: QPainter, r: QRect) -> None:
        cx, cy = r.center().x(), r.center().y()
        self._panel(p, r)
        stale = self.stale
        body = r.adjusted(6, 6, -6, -32)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(float(body.x()), float(body.y()), float(body.width()), float(body.height()), 5, 5)
        p.setClipPath(clip)
        p.translate(cx, body.center().y())
        if not self.aircraft_tilt:
            p.rotate(-self._shown_roll)         # 原样式：天地线朝滚转反方向转
        shift = self._shown_pitch * _PX_PER_DEG  # 抬头: 天地线下移
        wide = body.width() * 2
        sky = QColor(60, 120, 200) if not stale else QColor(70, 74, 80)
        gnd = QColor(120, 82, 40) if not stale else QColor(52, 50, 48)
        p.fillRect(QRectF(-wide / 2, shift - wide, wide, wide), sky)
        p.fillRect(QRectF(-wide / 2, shift, wide, wide), gnd)
        pen = QPen(QColor(245, 245, 245) if not stale else QColor(140, 140, 140), 1.6)
        p.setPen(pen)
        p.drawLine(QPointF(-wide / 2, shift), QPointF(wide / 2, shift))
        f = QFont("Segoe UI")
        f.setPointSizeF(max(6.0, body.height() / 22))
        p.setFont(f)
        for tick in (-60, -50, -40, -30, -20, -10, 10, 20, 30, 40, 50, 60):
            y = shift - tick * _PX_PER_DEG
            if abs(y) > body.height() / 2:
                continue
            half = 16 if tick % 20 == 0 else 9
            p.drawLine(QPointF(-half, y), QPointF(half, y))
            if tick % 20 == 0:
                p.drawText(QRect(int(-half - 26), int(y - 7), 22, 14),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(abs(tick)))
        p.restore()
        # 新样式只改变滚转参照，俯仰位移仍由上面的天地线/刻度表示。
        sym = QColor(255, 214, 10) if not stale else QColor(150, 150, 150)
        sp = QPen(sym, 2.4)
        sp.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(sp)
        mid = body.center().y()
        p.save()
        p.setClipPath(clip)
        p.translate(cx, mid)
        if self.aircraft_tilt:
            p.rotate(self._shown_roll)
        p.drawLine(-26, 0, -8, 0)
        p.drawLine(8, 0, 26, 0)
        p.drawPoint(0, 0)
        p.restore()
        # 边框 + 读数
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.drawRoundedRect(float(body.x()), float(body.y()), float(body.width()), float(body.height()), 5, 5)
        p.setPen(QColor(240, 240, 240) if not stale else QColor(150, 150, 150))
        small = QFont("Segoe UI")
        small.setPixelSize(12 if r.width() >= 120 else 10)
        p.setFont(small)
        footer = QRect(r.x() + 2, r.bottom() - 28, r.width() - 4, 26)
        if stale:
            p.drawText(footer, Qt.AlignmentFlag.AlignCenter, tr("姿态 无信号"))
        else:
            p.drawText(footer, Qt.AlignmentFlag.AlignCenter,
                       f"P{self._shown_pitch:+.1f}° R{self._shown_roll:+.1f}°\nH{self._shown_heading:03.0f}°")

    def gauge_lines(self) -> list[str]:
        """当前勾选的仪表行(已排版成显示文本)。供绘制与测试共用。"""
        out: list[str] = []
        for k, lab, unit in _GAUGES:
            if k not in self.items:
                continue
            v = self._gauge_value(k)
            line = f"{lab} {v}{unit}" if v else f"{lab} —"
            if k == "fuel":
                line += tr(' · 预计续航 {0}', self.fuel_time_text())
            out.append(line)
        return out

    def fuel_time_text(self) -> str:
        return format_endurance(self._fuel_endurance.seconds_left(self._clock()))

    def _paint_card(self, p: QPainter, box: QRect, label: str, value: str,
                    *, companion: tuple[str, str] | None = None, fresh: bool | None = None) -> None:
        self._panel(p, box)
        p.save()
        p.setClipRect(box)
        if companion is not None:
            # 在同一张燃油卡片内并排展示，不新增开关或挤占其他仪表的位置。
            left = QRect(box.x(), box.y(), box.width() // 2, box.height())
            right = QRect(left.right() + 1, box.y(), box.width() - left.width(), box.height())
            self._paint_card_text(p, left, label, value, fresh, compact=True)
            self._paint_card_text(p, right, *companion, fresh, compact=True)
        else:
            self._paint_card_text(p, box, label, value, fresh)
        p.restore()

    def _paint_card_text(self, p: QPainter, box: QRect, label: str, value: str,
                         fresh: bool | None, *, compact: bool = False) -> None:
        small = QFont("Microsoft YaHei UI")
        small.setPixelSize(max(9, min(11, box.height() // 3)))
        p.setFont(small)
        p.setPen(QColor(175, 184, 195))
        pad = 5 if compact else 8
        top = box.adjusted(pad, 3, -pad, 0)
        p.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   QFontMetrics(small).elidedText(label, Qt.TextElideMode.ElideRight, top.width()))
        large = QFont("Segoe UI")
        large.setPixelSize(max(10, min(16, box.height() // 2 - 3)))
        large.setBold(True)
        bottom = box.adjusted(pad, 0, -pad, -3)
        if compact:
            while large.pixelSize() > 8 and QFontMetrics(large).horizontalAdvance(value) > bottom.width():
                large.setPixelSize(large.pixelSize() - 1)
        p.setFont(large)
        fresh = not self.stale if fresh is None else fresh
        p.setPen(QColor(255, 235, 170) if fresh else QColor(160, 160, 160))
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                   QFontMetrics(large).elidedText(value, Qt.TextElideMode.ElideRight, bottom.width()))

    def _gauge_value(self, key: str) -> str:
        st, at = self._st, self._att
        if key == "aoa":
            v = (st.aoa_deg if st else None)
            return f"{v:+.1f}" if v is not None else ""
        if key == "aos":
            v = (st.aos_deg if st else None)
            return f"{v:+.1f}" if v is not None else ""
        if key == "ias":
            v = (st.ias_kmh if st else None)
            return f"{v:.0f}" if v is not None else ""
        if key == "tas":
            v = (st.tas_kmh if st else None)
            return f"{v:.0f}" if v is not None else ""
        if key == "mach":
            v = (st.mach if st else None)
            return f"{v:.2f}" if v is not None else ""
        if key == "alt":
            v = (st.altitude_m if st else None)
            return f"{v:.0f}" if v is not None else ""
        if key == "vario":
            v = (at.vario if at else (st.vy_ms if st else None))
            return f"{v:+.1f}" if v is not None else ""
        if key == "ny":
            v = (st.ny if st else None)
            return f"{v:+.2f}" if v is not None else ""
        if key == "roll_rate":
            v = (st.roll_rate_degs if st else None)
            return f"{v:+.0f}" if v is not None else ""
        if key == "throttle":
            v = (st.throttle_pct if st else None)
            return f"{v:.0f}" if v is not None else ""
        if key == "fuel":
            v = st.fuel_kg if st and self._fuel_endurance.fresh(self._clock()) else None
            return f"{v:.0f}" if v is not None else ""
        if key == "heading":
            return f"{self._shown_heading:03.0f}" if at else ""
        return ""

