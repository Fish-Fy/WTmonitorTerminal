"""合并任务结算、本人退出、地图有效性和 HUD 时钟，划分对局。

HUD 缓冲可跨局，结算后先暂停接收，重新入场时只交付新局的消息。
场景几何和地图版本会在同局换载具时变化，单独的变化无法判定对局。
"""
from __future__ import annotations

import time
from math import isfinite
from typing import Any, Callable, Iterable

from ..i18n import tr

DEFAULT_MARGIN = 5.0          # 秒; 小于这个回落量视为抖动/乱序
DEFAULT_VALID_GRACE = 2.0     # 秒; valid 持续 false 这么久才算真离开战场
DEFAULT_OFFLINE_GRACE = 3.0   # 秒; 离线持续这么久后重连才算边界
LAST_KILL = "last_kill"
FIRST_BLOOD = "first_blood"
SELF_QUIT = "self_quit"
FINISHED_MISSION_STATUSES = frozenset(("success", "fail", "failed"))


class BattleWatcher:
    def __init__(self, margin: float = DEFAULT_MARGIN, clock: Callable[[], float] | None = None,
                 valid_grace: float = DEFAULT_VALID_GRACE,
                 offline_grace: float = DEFAULT_OFFLINE_GRACE):
        self.margin = float(margin)
        self.valid_grace = float(valid_grace)
        self.offline_grace = float(offline_grace)
        self.clock = clock or time.monotonic
        self.battle_id = 1
        self.last_reason = ""
        self._max_t: float | None = None
        self._scene: str | None = None
        self._online = False
        self._ever_online = False
        self._offline_since: float | None = None
        self._map_valid: bool | None = None
        self._invalid_since: float | None = None
        self._left_battle = False
        self._mission_finished = False
        self.hud_closed = False
        self._closed_at: float | None = None

    def _record_boundary(self, reason: str) -> None:
        self.last_reason = reason
        self.battle_id += 1

    def _close_hud(self) -> None:
        self.hud_closed = True
        self._closed_at = self._max_t
        self._max_t = None

    def _finish(self, reason: str) -> bool:
        if self.hud_closed:
            return False
        self._close_hud()
        self._record_boundary(reason)
        return True

    def scan_hud(self, rows: Iterable[tuple[Any, str]]) -> tuple[int, bool]:
        """按 HUD 消息 ID 顺序处理时间和线索，返回应保留的后缀起点及是否清理。

        一批可混有旧局战报、结算和新局战报，只交付最后一段仍有效的战报。
        """
        cut = 0
        reason = ""
        for idx, (raw, signal) in enumerate(rows):
            ending = signal in (LAST_KILL, SELF_QUIT)
            try:
                t = float(raw)
            except (TypeError, ValueError):
                t = 0.0
            timed = isfinite(t) and t > 0
            if self.hud_closed:
                restarted = (timed and self._closed_at is not None
                             and t + self.margin < self._closed_at)
                if self._map_valid is False or not (restarted or signal == FIRST_BLOOD):
                    if timed and (self._closed_at is None or t > self._closed_at):
                        self._closed_at = t
                    cut = idx + 1
                    continue
                self.hud_closed = False
                cut = idx
            elif (self._closed_at is not None and signal != FIRST_BLOOD
                  and (not timed or t == self._closed_at or ending)):
                # 重新入场时，缓冲里仍可能有同一结算秒的嘉奖和伤害。
                cut = idx + 1
                continue
            self._closed_at = None
            if timed and self._max_t is not None and t + self.margin < self._max_t:
                reason = reason or tr("任务时钟回落 {0:.0f}s→{1:.0f}s", self._max_t, t)
                self._max_t = None
                cut = idx
            if timed and (self._max_t is None or t > self._max_t):
                self._max_t = t
            if ending:
                reason = tr("收到对局收尾嘉奖") if signal == LAST_KILL else tr("本人退出了战斗")
                self._close_hud()
                cut = idx + 1
        if reason:
            self._record_boundary(reason)
        return cut, bool(reason)

    def scan(self, times: Iterable[Any]) -> tuple[int, bool]:
        return self.scan_hud((t, "") for t in times or ())

    def feed_times(self, times: Iterable[Any]) -> bool:
        return self.scan(times)[1]

    def feed_events(self, events: Iterable[Any]) -> bool:
        return self.feed_times(getattr(e, "time", None) for e in events or ())

    def mission_status(self, status: str) -> bool:
        if status in FINISHED_MISSION_STATUSES:
            self._mission_finished = True
            return self._finish(tr("任务已结算"))
        if status == "running" and self._mission_finished:
            self._mission_finished = False
            if self._map_valid is not False:
                self.hud_closed = False
        return False

    def scene_changed(self, scene: str | None) -> bool:
        """只记录几何签名（换图后主窗口要重新 fit），**永远不当作对局边界**。

        陆战出击载具在坦克↔飞机之间切换会换两张不同几何的图，那是同一局；
        以前把它当边界就是"名单莫名其妙被清空"的真凶。
        """
        if scene and scene != self._scene:
            self._scene = scene
        return False

    def map_info_valid(self, ok: bool) -> bool:
        """`map_info.valid` 持续 false ≥ `valid_grace` 秒 = 真的离开战场 ⇒ 一局结束。

        调用方要**每次都报**（Poller 每轮 map_info 都会发），否则没法判断"持续"。
        瞬时抖动（切换出击载具/加载）落在 grace 之内 ⇒ 不清；重新进入战场也不再重复计数。
        """
        now = bool(ok)
        t = self.clock()
        previous = self._map_valid
        self._map_valid = now
        if not now:
            if previous is None and self._max_t is None and not self.hud_closed:
                # 终端在机库启动时，8111 也可能返回客户端留存的战报。
                self._left_battle = True
                self._close_hud()
            if self._invalid_since is None:
                self._invalid_since = t
                return False
            if not self._left_battle and t - self._invalid_since >= self.valid_grace:
                self._left_battle = True
                return self._finish(tr("离开战场（回大厅）已 {0:.0f}s", t - self._invalid_since))
            return False
        if self._left_battle or previous is False:
            self.hud_closed = False
        self._invalid_since = None
        self._left_battle = False
        return False

    def connection(self, online: bool) -> bool:
        """离线持续 ≥ `offline_grace` 秒后重连才算边界；首次上线不算（没有"前一局"）。"""
        now = bool(online)
        t = self.clock()
        if not now:
            if self._online:
                self._offline_since = t
            self._online = False
            return False
        first = not self._ever_online
        self._ever_online = True
        gap = None if self._offline_since is None else t - self._offline_since
        self._offline_since = None
        self._online = True
        if first or gap is None:
            return False        # 首次上线 / 本来就在线: 重复的"在线"信号永不清空
        if gap >= self.offline_grace:
            self._map_valid = None
            self._invalid_since = None
            self._left_battle = self._mission_finished = self.hud_closed = False
            self._max_t = self._closed_at = None
            self._record_boundary(tr("断连 {0:.0f}s 后重连", gap))
            return True
        return False
