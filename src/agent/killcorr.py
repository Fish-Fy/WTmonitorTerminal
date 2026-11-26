"""击杀文案 ↔ 地图图标消失关联: 给载具识别的二分染色提供外部阵营证据。

规则(用户定稿):
  A) 击杀文案出现 + 友军单位数 −1  → 受害者是友军(敌杀友);
  B) 击杀文案出现 + 友军单位数没变 → 受害者是敌军(友杀敌)。
为什么只看友军: 队友全程常亮, 图标消失基本=被击落/离场; 敌军只有被点亮才显示,
脱点亮同样会消失 ⇒ 红方计数只记录、不参与判定, 避免把脱点亮当阵亡。

时钟换算: map_obj 帧只有本地时钟, hudmsg 的 time 是局内秒 ⇒ 每条 hud 批拿
(批内最大局内秒, 到达时刻) 当锚点, 击杀的局内秒经锚点线性换算成本地时刻。

判定是**延迟**的(由调用方约 1.2s 后调 judge_kill): 文案到达时, 击杀之后的地图帧
还没攒够。同秒多杀用消耗制: 友军掉 1 个只够一条击杀认领规则A, 其余落规则B。
全部状态只在本局有效: 主窗口每次重建 Roster 时一并 reset, 不落盘、不跨局。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Tuple

PRE_WINDOW = 4.0        # 基线回看窗(秒): 拿击杀前最近一次计数
PRE_GAP = 0.2           # 基线与击杀时刻的最小间隔(击杀瞬间那一帧不算基线)
POST_HORIZON = 3.0      # 击杀后最多看多远(秒)
POST_MIN_FRAMES = 1     # 击杀后至少要有这么多帧才判(没有=证据不足)
FRAME_KEEP = 60.0       # 帧史保留时长(秒)
CONSUME_WINDOW = 1.5    # 同一次"友军消失"可被击杀认领的时间邻域(秒)


class KillCorrelator:
    """地图单位计数帧史 + 局内秒↔本地时钟锚点 + 击杀时刻的友军掉落判定。"""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._frames: Deque[Tuple[float, int, int]] = deque()   # (本地秒, 友军数, 敌军数)
        self._anchor: Tuple[float, float] | None = None         # (局内秒, 本地秒)
        self._consumed: list[Tuple[float, int]] = []            # (击杀本地秒, 已认领数)

    # ------------------------------------------------------------------ 状态
    def reset(self) -> None:
        """每局清空(硬性规则: 识别不跨局)。"""
        self._frames.clear()
        self._anchor = None
        self._consumed.clear()

    # ------------------------------------------------------------------ 输入
    def feed_frame(self, friend: int, enemy: int, now: float | None = None) -> None:
        """每帧地图对象喂一次友/敌**单位**计数(口径由调用方保证: 不含占领区/出生点/自己)。"""
        t = self._clock() if now is None else float(now)
        self._frames.append((t, int(friend), int(enemy)))
        while self._frames and self._frames[0][0] < t - FRAME_KEEP:
            self._frames.popleft()

    def anchor_hud(self, match_t: float, now: float | None = None) -> None:
        """hud 批到达时报(批内最大局内秒, 到达时刻), 用作时间换算锚点。"""
        t = self._clock() if now is None else float(now)
        self._anchor = (float(match_t), t)

    # ------------------------------------------------------------------ 判定
    def _to_local(self, match_t: float) -> float | None:
        if self._anchor is None:
            return None
        am, al = self._anchor
        return al - (am - float(match_t))

    def judge_kill(self, match_t: float, now: float | None = None) -> str | None:
        """这条击杀的受害方: 'friend'(友军被击杀) / 'enemy'(敌军被击杀) / None(证据不足, 不猜)。"""
        loc = self._to_local(match_t)
        if loc is None:
            return None
        t_now = self._clock() if now is None else float(now)
        horizon = min(t_now, loc + POST_HORIZON)
        pre = [f for f in self._frames if loc - PRE_WINDOW <= f[0] <= loc - PRE_GAP]
        post = [f for f in self._frames if loc - PRE_GAP < f[0] <= horizon]
        if not pre or len(post) < POST_MIN_FRAMES:
            return None
        baseline = pre[-1][1]
        if baseline <= 0:
            return None                     # 空图/不在战场: 不猜
        drop = baseline - min(p[1] for p in post)
        used = 0
        for t, n in self._consumed:
            if abs(t - loc) <= CONSUME_WINDOW:
                used = max(used, n)
        if drop - used >= 1:
            self._consumed = [(t, n) for t, n in self._consumed if abs(t - loc) > CONSUME_WINDOW]
            self._consumed.append((loc, used + 1))
            return "friend"
        return "enemy"
