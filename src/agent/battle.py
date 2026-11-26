"""对局边界判定 —— 载具识别池"每一局清一次"靠什么触发。

真机取证后的结论（L 轮，两次现场采样各 30~40s）：
  * 正常打一局期间 `map_info.valid`、几何签名、`map_generation`、hud `time` **全都稳定**
    ⇒ 用户报的"莫名其妙被清空"不是这些抖动来的；
  * 陆战里 **出击载具在坦克↔飞机之间切换会换两张不同几何的图** ⇒ 伪场景名来回变。
    以前把"场景名变化"当边界 ⇒ 同一局内每换一次出击载具就清一次池子。**现在不再当边界。**

所以边界只认"真的开了一局/真的回过大厅"这类硬信号，并且一律带**去抖**：
  1. 任务时钟回落（`/hudmsg` 的 `time` 是局内秒，新局从头开始）—— 最强信号。
  2. 「先拔头筹」重复出现（一局只该有一次）或时间戳倒退。
  3. `map_info.valid` **持续** false ≥ `valid_grace` 秒 = 真的离开战场回大厅（瞬时抖动被吃掉）。
  4. 离线 **持续** ≥ `offline_grace` 秒后重连 = 客户端可能重启（探活抖动不算）。
  「最后一击/最终赢家/完成了一击」只置标记，**绝不单独清空** —— 必须等上面某个真信号确认。

刻意不用：`map_generation`（实测重生也 +1）、`mission.status`（实测恒 "running"）、
伪场景名变化（陆战出击换载具就会变，见上）。
另两条真机坑：首包常把上一局的尾巴混在本批前几条 ⇒ `scan` 返回**批内切点**只吸收其后的事件；
新一局的第一条常常就是本批第 0 条 ⇒ 切点不能兼任"有没有边界"，所以返回二元组。
策略：宁可漏清一次，也不要误清；每次清空都把原因写进 `last_reason`（主窗口会记进 clear_log）。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from ..i18n import tr

DEFAULT_MARGIN = 5.0          # 秒; 小于这个回落量视为抖动/乱序
DEFAULT_VALID_GRACE = 2.0     # 秒; valid 持续 false 这么久才算真离开战场
DEFAULT_OFFLINE_GRACE = 3.0   # 秒; 离线持续这么久后重连才算边界
LAST_KILL = "last_kill"       # 「最后一击 / 最终赢家 / 完成了一击」
FIRST_BLOOD = "first_blood"   # 「先拔头筹」


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
        self._in_battle: bool | None = None
        self._invalid_since: float | None = None
        self._left_battle = False               # 已经为"离开战场"清过一次
        self._pending_end = False               # 收到"最后一击", 等真信号确认
        self._last_fb: float | None = None      # 上一次"先拔头筹"的时间戳

    # ------------------------------------------------------------------ 内部
    def _boundary(self, reason: str) -> None:
        self.last_reason = reason
        self.battle_id += 1
        self._max_t = None
        self._pending_end = False
        self._last_fb = None

    # ------------------------------------------------------------------ 输入
    def scan(self, times: Iterable[Any]) -> tuple[int, bool]:
        """扫一遍本批 `time`，返回 `(cut, 是否出现新一局)`。

        `cut` = 从第几条起属于当前这局（新一局的第一条常常就是本批第 0 条，所以切点不能
        兼任边界信号）；一批内多次回落以最后一次为准，且 `battle_id` 每批最多 +1。
        """
        vals = list(times or ())
        cut = 0
        hit = False
        for idx, raw in enumerate(vals):
            try:
                t = float(raw)                  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if t <= 0:
                continue                        # 没有可用 time => 不参与判定
            if self._max_t is None:
                self._max_t = t
                continue
            if t + self.margin < self._max_t:
                prev = self._max_t
                self._max_t = t                 # 以新局为基线继续观察
                cut = idx
                if not hit:
                    hit = True
                    self._boundary(tr("任务时钟回落 {0:.0f}s→{1:.0f}s", prev, t))
            elif t > self._max_t:
                self._max_t = t
        return cut, hit

    def feed_times(self, times: Iterable[Any]) -> bool:
        return self.scan(times)[1]

    def feed_events(self, events: Iterable[Any]) -> bool:
        return self.feed_times(getattr(e, "time", None) for e in events or ())

    def on_award(self, kind: str, t: float = 0.0) -> bool:
        """嘉奖类信号。`last_kill` 只置标记；`first_blood` 只在"本局已经有过一次"或时间戳
        倒退时才判为新一局 —— 单独一句嘉奖绝不足以清空。"""
        if kind == LAST_KILL:
            self._pending_end = True
            return False
        if kind != FIRST_BLOOD:
            return False
        tt = float(t or 0.0)
        boundary = bool(self._pending_end) or (
            self._last_fb is not None and tt + self.margin < self._last_fb)
        if boundary:
            self._boundary(tr("新的一局先拔头筹"))
        self._last_fb = tt
        self._in_battle = True
        return boundary

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
        if not now:
            if self._invalid_since is None:
                self._invalid_since = t
                return False
            if not self._left_battle and t - self._invalid_since >= self.valid_grace:
                self._left_battle = True
                self._boundary(tr("离开战场（回大厅）已 {0:.0f}s", t - self._invalid_since))
                return True
            return False
        if self._invalid_since is not None and not self._left_battle:
            pass                                # 抖动后恢复: 什么都不做
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
            self._boundary(tr("断连 {0:.0f}s 后重连", gap))
            return True
        return False
