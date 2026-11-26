"""阈值提示：低油、临界迎角、我被击落 —— 只出文字（状态栏），**没有任何声音**。

用户 2026-09-19 明确要求"直接删除这个告警声音"，所以这里不留 beep 回调、也不留可开关的
发声通道：本模块只负责"该不该提示、提示什么"，落地方式只有一个（状态栏文字）。

刻意做"武装/消耗"语义：越阈值只提示一次，不会因为持续超阈而连发。迎角带迟滞 —— 必须回落到
`stall_aoa_deg - aoa_hysteresis` 以下才重新武装，否则在失速边界上会刷成一片。
换图/开新局调 reset() 只清"被击落"的文案去重；阈值不靠 reset 重武装，而是等实时读数回到健康值
（真机踩过：换图瞬间上一局的低油读数还在 /state 里，硬复位会几秒内提示两次）。
"""
from __future__ import annotations

from typing import Iterable
from ..i18n import tr

FUEL_KEY = "Mfuel, kg"
AOA_KEY = "AoA, deg"


class AlertEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self._fuel_armed = True
        self._aoa_armed = True
        # 真机实测: 同一次死亡服务端会发两条文案相同、id 不同的消息 → 按文案去重, 只提示一次
        self._announced: set[str] = set()

    def reset(self) -> None:
        self._announced.clear()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    # ------------------------------------------------------------------ 遥测侧
    def on_state(self, state: dict) -> list[str]:
        """吃一份 /state，返回本次要显示的条目。缺键就静默，绝不 KeyError。"""
        if not self.enabled or not isinstance(state, dict):
            return []
        reasons: list[str] = []
        low = getattr(self.cfg, "low_fuel_kg", 1500.0)
        fuel = state.get(FUEL_KEY)
        if isinstance(fuel, (int, float)) and not isinstance(fuel, bool):
            if float(fuel) > float(low):
                self._fuel_armed = True            # 看到健康读数才重新武装(补油/新开一局)
            elif self._fuel_armed:
                self._fuel_armed = False
                reasons.append(tr('低油：剩余 {0:.0f} kg（阈值 {1:.0f}）', float(fuel), float(low)))
        stall = float(getattr(self.cfg, "stall_aoa_deg", 18.0))
        hyst = float(getattr(self.cfg, "aoa_hysteresis", 2.0))
        aoa = state.get(AOA_KEY)
        if isinstance(aoa, (int, float)) and not isinstance(aoa, bool):
            v = abs(float(aoa))
            if v >= stall:
                if self._aoa_armed:
                    self._aoa_armed = False
                    reasons.append(tr("临界迎角：{0:.1f}°（阈值 {1:.0f}°）", float(aoa), stall))
            elif v <= stall - hyst:
                self._aoa_armed = True
        return reasons

    # ------------------------------------------------------------------ 事件侧
    def on_feed(self, items: Iterable) -> list[str]:
        if not self.enabled:
            return []
        reasons: list[str] = []
        for it in items or ():
            if getattr(it, "category", "") != "self_victim" or getattr(it, "kind", "") != "kill":
                continue
            key = getattr(it, "text", "") or getattr(it, "fp", "")
            if key in self._announced:
                continue                            # 同一次死亡的多条重复文案: 只提示一次
            self._announced.add(key)
            reasons.append(tr('你被击落！{0}', getattr(it, 'fp', '') or getattr(it, 'text', '')))
        return reasons
