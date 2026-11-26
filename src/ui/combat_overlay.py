"""独立置顶透明小窗: 交战车型速览 + 威胁告警横幅(用户 2026-09-29 定稿"浮在游戏画面右侧")。

为什么是独立窗口而不是地图叠加层: 游戏窗口在最前时, 终端主窗口(连同地图叠加层)根本
看不见。本窗口无边框 / 置顶 / 半透明底 / **点击穿透**, 不抢游戏输入 —— 代价是不能拖拽,
位置默认贴屏幕右缘, 要挪就改 config.yaml → combat_overlay.x/y。

连杀、附近队友阵亡和交战车型共用紧凑卡片，各自显示倒计时。交战记录保留多条。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from ..agent.events import FeedItem, display_verb
from ..agent.threatwatch import NearbyDeathNotice, RampageNotice
from ..i18n import tr

_STREAK_AMBER = "#f2c578"
_NEARBY_ORANGE = "#ff9b76"
_DAMAGE_DEALT_GREEN = "#76dfba"
_DAMAGE_TAKEN_RED = "#ff89a4"


class _CombatCard(QFrame):
    _VALUE_WIDTH = 64

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("combatCard")
        self.setStyleSheet("QFrame#combatCard { background: rgba(22, 33, 40, 246); "
                           "border: 1px solid #42535d; border-radius: 7px; }")
        self.summary = ""
        self._duration = 0.0
        self._expires_at = 0.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(7)
        layout.addLayout(row)

        self._accent = QFrame(self)
        self._accent.setFixedWidth(3)
        row.addWidget(self._accent)
        info = QVBoxLayout()
        info.setSpacing(1)
        row.addLayout(info)
        self.title = self._label(13, _STREAK_AMBER, bold=True)
        self.primary = self._label(15, "#f5f6f7", bold=True)
        self.secondary = self._label(12, "#afbdc7")
        self.detail = self._label(11, "#d1b481")
        for label in (self.title, self.primary, self.secondary, self.detail):
            info.addWidget(label)

        self.value = self._label(36, _STREAK_AMBER, bold=True)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.value)

        self.progress = QProgressBar(self)
        self.progress.setAccessibleName(tr("提示剩余时间"))
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        layout.addWidget(self.progress)

    def _label(self, size: int, color: str, *, bold: bool = False) -> QLabel:
        label = QLabel(self)
        label.setTextFormat(Qt.PlainText)
        self._style_label(label, size, color, bold=bold)
        return label

    @staticmethod
    def _style_label(label: QLabel, size: int, color: str, *, bold: bool) -> None:
        weight = "bold" if bold else "normal"
        label.setStyleSheet(f"color: {color}; background: transparent; "
                            f"font-size: {size}px; font-weight: {weight};")
        label.ensurePolished()

    def set_contents(self, *, title: str, primary: str, cue: str, color: str,
                     secondary: str = "", detail: str = "") -> None:
        self._accent.setStyleSheet(f"background: {color}; border-radius: 1px;")
        self._style_label(self.title, 13, color, bold=True)
        self.progress.setStyleSheet("QProgressBar { border: none; border-radius: 1px; background: #32414b; }"
                                    f" QProgressBar::chunk {{ background: {color}; border-radius: 1px; }}")
        fields = ((self.title, title), (self.primary, primary), (self.secondary, secondary), (self.detail, detail))
        text_width = min(152, max(label.fontMetrics().horizontalAdvance(text) for label, text in fields))
        for label, text in fields:
            label.setFixedWidth(text_width)
            label.setText(label.fontMetrics().elidedText(text, Qt.ElideRight, text_width))
            label.setVisible(bool(text))

        size = max(18, 36 - 4 * max(0, len(cue) - 2))
        self._style_label(self.value, size, color, bold=True)
        advance = self.value.fontMetrics().horizontalAdvance(cue)
        if advance + 2 > self._VALUE_WIDTH:
            size = max(12, size * (self._VALUE_WIDTH - 2) // advance)
            self._style_label(self.value, size, color, bold=True)
        self.value.setText(cue)
        value_width = min(self._VALUE_WIDTH, self.value.fontMetrics().horizontalAdvance(cue) + 2)
        self.value.setFixedWidth(value_width)
        row_spacing = 7 * 2
        self.setFixedWidth(18 + 3 + row_spacing + text_width + value_width)

    def start(self, now: float, seconds: float) -> None:
        self._duration = float(seconds)
        self._expires_at = now + self._duration
        self.progress.setValue(self.progress.maximum())

    def update_progress(self, now: float) -> bool:
        remaining = self._expires_at - now
        if remaining <= 0:
            return False
        self.progress.setValue(round(self.progress.maximum() * min(1.0, remaining / self._duration)))
        return True


class CombatOverlay(QWidget):
    """只读悬浮窗。clock 可注入(测试用假时钟驱动过期)。"""

    def __init__(self, ttl: float = 8.0, max_rows: int = 6, clock=None, parent=None):
        super().__init__(parent,
                         Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # 弹出时不抢焦点(游戏不被切出去)
        self.setObjectName("combatOverlay")
        self.setStyleSheet("QWidget#combatOverlay { background: transparent; }")
        self._ttl = float(ttl)
        self._max_rows = max(1, int(max_rows))
        self._clock = clock or time.monotonic
        self._rows: list[_CombatCard] = []
        self._active_alert: NearbyDeathNotice | RampageNotice | None = None
        self._position: tuple[int, int] | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._alert_card = _CombatCard(self)
        self._alert_card.hide()
        lay.addWidget(self._alert_card, alignment=Qt.AlignRight)
        self._rows_lay = QVBoxLayout()
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(2)
        lay.addLayout(self._rows_lay)
        lay.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.sweep)
        self._timer.start()

    # ------------------------------------------------------------------ 内容
    def add_damage(self, item: FeedItem) -> None:
        event = item.event
        if event is None:
            return
        mine = item.category == "self_action"
        name, vehicle = (event.victim, event.victim_vehicle) if mine else (event.actor, event.actor_vehicle)
        card = _CombatCard(self)
        title = tr("你{0}" if mine else "你被{0}", display_verb(event.verb)).capitalize()
        card.set_contents(title=title,
                          primary=vehicle or tr("载具不明"), secondary=name or "",
                          cue="→" if mine else "←", color=_DAMAGE_DEALT_GREEN if mine else _DAMAGE_TAKEN_RED)
        card.summary = item.display
        card.start(self._clock(), self._ttl)
        self._rows_lay.addWidget(card, alignment=Qt.AlignRight)
        card.show()
        self._rows.append(card)
        while len(self._rows) > self._max_rows:
            self._drop(self._rows.pop(0))
        self.sweep()
        self._update_timer()
        self._fit_contents()

    def banner(self, notice: NearbyDeathNotice | RampageNotice, seconds: float = 5.0) -> None:
        """顶部告警在 seconds 后自动隐藏。"""
        card = self._alert_card
        if isinstance(notice, RampageNotice):
            card.set_contents(title=tr("连杀嘉奖") if notice.award else tr("连续击杀"),
                              primary=notice.actor, secondary=notice.vehicle_label, detail=notice.award,
                              cue=f"×{notice.count}", color=_STREAK_AMBER)
        else:
            card.set_contents(title=tr("附近队友阵亡"), primary=tr("近 {0:.0f} 秒", notice.window),
                              cue=f"×{notice.count}", color=_NEARBY_ORANGE)
        self._active_alert = notice
        card.start(self._clock(), seconds)
        card.show()
        self._update_timer()
        self._fit_contents()

    # ------------------------------------------------------------------ 清理
    def hide_nearby_alert(self) -> None:
        if isinstance(self._active_alert, NearbyDeathNotice):
            self._hide_alert()
            self._update_timer()
            self._fit_contents()

    def _hide_alert(self) -> None:
        self._active_alert = None
        self._alert_card.hide()

    def clear_rows(self) -> None:
        for card in self._rows:
            self._drop(card)
        self._rows.clear()
        self._update_timer()
        self._fit_contents()

    def clear(self) -> None:
        self._hide_alert()
        self.clear_rows()

    def sweep(self) -> None:
        """清理过期卡片，并更新仍在显示的进度条。"""
        now = self._clock()
        keep = []
        for card in self._rows:
            if card.update_progress(now):
                keep.append(card)
            else:
                self._drop(card)
        rows_expired = len(keep) != len(self._rows)
        self._rows = keep
        alert_expired = self._active_alert is not None and not self._alert_card.update_progress(now)
        if alert_expired:
            self._hide_alert()
        if rows_expired or alert_expired:
            self._update_timer()
            self._fit_contents()

    def _update_timer(self) -> None:
        interval = 100 if self._rows or self._active_alert is not None else 500
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def _drop(self, card: _CombatCard) -> None:
        self._rows_lay.removeWidget(card)
        card.hide()
        card.deleteLater()

    # ------------------------------------------------------------------ 测试/探测接口
    def row_count(self) -> int:
        return len(self._rows)

    def texts(self) -> list[str]:
        return [card.summary for card in self._rows]

    def row_style(self, index: int) -> str:
        return self._rows[index].title.styleSheet()

    def banner_text(self) -> str:
        return self._active_alert.message if self._active_alert is not None else ""

    # ------------------------------------------------------------------ 定位
    def place_default(self, x: int = -1, y: int = -1) -> None:
        """贴到主屏幕右缘偏上; config 给了 x/y(≥0)就用配置的。"""
        self._position = (x, y)
        self._fit_contents()

    def _fit_contents(self) -> None:
        self.adjustSize()
        if self._position is None:
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        x, y = self._position
        geo = screen.availableGeometry()
        px = x if x >= 0 else geo.right() + 1 - self.width() - 12
        preferred_y = geo.top() + geo.height() // 4
        bottom_y = max(geo.top(), geo.bottom() + 1 - self.height() - 12)
        py = y if y >= 0 else min(preferred_y, bottom_y)
        self.move(px, py)
