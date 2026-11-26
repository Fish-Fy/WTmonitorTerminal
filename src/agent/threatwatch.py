"""威胁告警(纯逻辑, 不碰 Qt): 附近队友连续死亡 + 同一人连续击杀(绕后/狙台)。

为什么不塞进 killcorr: killcorr 回答"这条击杀的受害者是哪边"(数人头钉阵营);
这里回答两条新问题 —— "我身边是不是正在被点名"(要**位置**)与"有没有人杀疯了"(要**人名**)。

证据口径:
  * 友方单位全程常亮, 图标消失 ≈ 阵亡/离场(与 killcorr 同一口径); 敌军脱点亮噪声不参战。
  * 单位没有稳定 id: 逐帧用"同类型 + 最近邻 < eps"继承身份(与 map_view 轨迹同一招)。
  * 整帧为空的两种含义要分清: frame_alive 假(本帧没拉到对象)=服务端打嗝, 跳过且
    **不更新基线**; frame_alive=True(帧里明明有我自己/别的对象, 唯独友方单位空了)
    = 队友真没了, 照常做消失判定 —— 否则"附近连死"的最后一次死亡恰好随空帧被丢掉。
  * "附近" = 消失点距我 ≤ radius(归一化, 默认 0.10, 面板可调, 会随 ui_state 持久化)。

附近阵亡触发后进入冷却期，连杀从达到阈值起逐次提醒。全部状态只在本局有效，reset() 由
主窗口的对局边界入口统一调, 不落盘、不跨局(载具识别同一条红线)。
"""
from __future__ import annotations

import math
import time
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from ..i18n import tr
from .roster import normalize_key
from .teammate_tracks import AircraftLossTracker, match_teammates

MOVE_EPS = 0.02        # 逐帧身份继承阈值(归一化): 5Hz 下任何单位一帧挪不了这么远
# 半径口径(2026-10-11 用户定稿): 圈 = **可视区边长 × 比例** ⇒ 活动区很小的地图上算出来
# 可能远小于 0.01。下限必须放宽到 0.002, 否则"看到的圈"和"判定用的圈"会被钳成两个大小。
RADIUS_MIN = 0.002
RADIUS_MAX = 0.50


@dataclass(frozen=True)
class NearbyDeathNotice:
    count: int
    window: float
    radius: float

    @property
    def message(self) -> str:
        return tr('附近队友连续阵亡 ×{0}（{1:.0f} 秒内 / 半径 {2:.2f}）——有敌人在点名你们！',
                  self.count, self.window, self.radius)


class NearbyDeathWatch:
    """友方单位在我附近连续消失 → 「附近队友连续阵亡」告警。"""

    def __init__(self, radius: float = 0.10, count: int = 2, window: float = 8.0,
                 cooldown: float = 20.0, clock=None):
        self._clock = clock or time.monotonic
        self._count = max(2, int(count))
        self._window = float(window)
        self._cooldown = float(cooldown)
        self._radius = 0.10
        self.set_radius(radius)
        self._aircraft_losses = AircraftLossTracker()
        self._prev: list[tuple[float, float, str]] = []     # 上一帧友方单位 (x, y, type)
        self._deaths: deque[float] = deque()                # 窗口内"附近死亡"的时刻
        self._fired_at: float | None = None

    # ------------------------------------------------------------------ 设置
    @staticmethod
    def _clamp(r: float) -> float:
        return min(RADIUS_MAX, max(RADIUS_MIN, float(r)))

    @property
    def radius(self) -> float:
        return self._radius

    def set_radius(self, r: float) -> None:
        self._radius = self._clamp(r)

    # ------------------------------------------------------------------ 状态
    def reset(self) -> None:
        self._aircraft_losses.reset()
        self._prev = []
        self._deaths.clear()
        self._fired_at = None

    # ------------------------------------------------------------------ 输入
    def feed(self, friends: Iterable[tuple[float, float, str]],
             me: tuple[float, float] | None, now: float | None = None,
             frame_alive: bool | None = None) -> NearbyDeathNotice | None:
        """每帧喂一次友方单位列表(口径: 不含我/线段/占领区/出生点)。触发时返回告警。

        frame_alive: None = 老口径(空帧一律当打嗝); True = 本帧确实拉到了对象(只是没有
        友方单位), 空 friends 按"队友真没了"判定; False = 强制当打嗝。"""
        t = self._clock() if now is None else float(now)
        cur = [(float(x), float(y), str(tp)) for x, y, tp in friends or ()]
        if not cur and not frame_alive:
            self._aircraft_losses.reset()
            return None                     # 空帧: 服务端打嗝, 跳过(基线也别动)
        prev, self._prev = self._prev, cur
        if me is None:
            self._aircraft_losses.reset()
            return None
        old_air = [(tp, (x, y)) for x, y, tp in prev if tp == "aircraft"]
        new_air = [(tp, (x, y)) for x, y, tp in cur if tp == "aircraft"]
        air_matches = match_teammates(old_air, new_air, MOVE_EPS)
        losses = self._aircraft_losses.observe(old_air, new_air, air_matches, t)
        losses.extend((x, y) for x, y, tp in prev
                      if tp != "aircraft" and not self._survives((x, y, tp), cur))
        for position in losses:
            if math.dist(position, me) <= self._radius:
                self._deaths.append(t)
        while self._deaths and self._deaths[0] < t - self._window:
            self._deaths.popleft()
        if len(self._deaths) < self._count:
            return None
        if self._fired_at is not None and t - self._fired_at < self._cooldown:
            return None                     # 冷却期: 已经提醒过了, 不连刷
        self._fired_at = t
        n = len(self._deaths)
        self._deaths.clear()                # 这轮已消耗; 下次告警等新一轮死亡
        return NearbyDeathNotice(n, self._window, self._radius)

    @staticmethod
    def _survives(unit: tuple[float, float, str], cur: list[tuple[float, float, str]]) -> bool:
        px, py, tp = unit
        return any(ct == tp and math.hypot(px - cx, py - cy) <= MOVE_EPS for cx, cy, ct in cur)


@dataclass(frozen=True)
class RampageNotice:
    actor: str
    vehicle: str
    count: int
    gap: float = 20.0
    award: str = ""

    @property
    def vehicle_label(self) -> str:
        return self.vehicle or tr("载具不明")

    @property
    def message(self) -> str:
        if self.award:
            return tr('{0}（{1}）获得连杀嘉奖「{2}」', self.actor, self.vehicle_label, self.award)
        return tr('{0}（{1}）连续击杀 ×{2}（间隔 ≤{3:.0f}s）——疑似绕后或占了狙台，留意它的位置！',
                  self.actor, self.vehicle_label, self.count, self.gap)


class RampageWatch:
    """相邻间隔内的击杀和游戏嘉奖，从第 count 杀起提示。"""

    # 真机 HUD 样本中，同一次击杀的嘉奖可能晚一秒。两种来源也可能分批到达。
    _REPORT_TOLERANCE = 1.0

    def __init__(self, gap: float = 20.0, count: int = 3):
        self._gap = float(gap)
        self._count = max(2, int(count))
        self._times: dict[str, list[float]] = {}            # 归一化击杀者 -> 局内秒序列
        self._label: dict[str, tuple[str, str]] = {}        # 归一化击杀者 -> (显示名, 载具)
        self._seen: set[tuple[str, str, float]] = set()     # 同一次击杀的多条文案去重
        self._report_times: dict[tuple[str, int], list[float]] = {}

    def clear_streaks(self) -> None:
        self._times.clear()
        self._label.clear()
        self._report_times.clear()

    def reset(self) -> None:
        self.clear_streaks()
        self._seen.clear()

    def feed_kill(self, actor: str | None, vehicle: str | None,
                  victim: str | None, t: float) -> RampageNotice | None:
        """吃一条击杀文案(已解析)。触发时返回告警。"""
        key = normalize_key(actor or "")
        if not key:
            return None
        t = float(t or 0.0)
        dk = (key, normalize_key(victim or ""), t)
        if dk in self._seen:
            return None                         # 真机同一次击杀发两条文案: 只算一次
        self._seen.add(dk)
        hist = self._times.setdefault(key, [])
        index = bisect_right(hist, t)
        if index == len(hist):
            self._label[key] = ((actor or "").strip(), (vehicle or "").strip())
        hist.insert(index, t)
        # 尾段连续 streak: 从最新一击往前数, 相邻间隔都 ≤ gap 才算
        run = 1
        for i in range(len(hist) - 1, 0, -1):
            if hist[i] - hist[i - 1] <= self._gap:
                run += 1
            else:
                break
        # 迟到的击杀可以补入当前连杀，但旧连杀里的击杀不重复触发当前告警。
        if index < len(hist) - run or not self._can_report(key, run, hist[-1]):
            return None
        disp, veh = self._label[key]
        return RampageNotice(disp, veh, run, gap=self._gap)

    def feed_award(self, actor: str | None, vehicle: str | None, title: str, count: int,
                   t: float) -> RampageNotice | None:
        """按游戏嘉奖本身的计数报告，不把奖章当成新的击杀。"""
        key = normalize_key(actor or "")
        if not key or not title or not self._can_report(key, count, t):
            return None
        return RampageNotice((actor or "").strip(), (vehicle or "").strip(), count, award=title)

    def _can_report(self, key: str, count: int, t: float) -> bool:
        if count < self._count:
            return False
        reported = self._report_times.setdefault((key, count), [])
        if any(abs(t - previous) <= self._REPORT_TOLERANCE for previous in reported):
            return False
        reported.append(t)
        return True
