"""用最近一段实际油量变化估算续航，不依赖机型或姿态仪字段。"""
from collections import deque
import math


class FuelEndurance:
    WINDOW = 10.0
    WARMUP = 3.0
    MAX_GAP = 1.5

    def __init__(self):
        self.reset()

    def reset(self):
        self._samples = deque(maxlen=512)
        self._capacity = None

    def update(self, fuel: float | None, now: float, capacity: float | None = None):
        if fuel is None or not math.isfinite(fuel) or fuel < 0 or not math.isfinite(now):
            self.reset()
            return
        if capacity is not None and (not math.isfinite(capacity) or capacity <= 0):
            capacity = None
        if self._samples:
            last_t, last_fuel = self._samples[-1]
            # 加油、重生、换载具或断流之后重新采样，不能沿用上一段油耗。
            if (now <= last_t or now - last_t > self.MAX_GAP or fuel > last_fuel + .05
                    or (capacity is not None and self._capacity is not None
                        and abs(capacity - self._capacity) > .05)):
                self.reset()
        if capacity is not None:
            self._capacity = capacity
        self._samples.append((now, fuel))
        while len(self._samples) > 2 and self._samples[1][0] <= now - self.WINDOW:
            self._samples.popleft()

    def fresh(self, now: float) -> bool:
        return bool(self._samples and 0 <= now - self._samples[-1][0] <= self.MAX_GAP)

    def seconds_left(self, now: float) -> float | None:
        if not self.fresh(now):
            return None
        end_t, fuel = self._samples[-1]
        if fuel == 0:
            return 0.0
        start_t, start_fuel = self._samples[0]
        elapsed, used = end_t - start_t, start_fuel - fuel
        # 8111 油量常取整，至少积累三秒且消耗一公斤再给估算。
        if elapsed < self.WARMUP or used < 1.0:
            return None
        return fuel * elapsed / used


def format_endurance(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    if seconds >= 86400:
        return "≈>24h"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"≈{hours}:{minutes:02d}:{seconds:02d}" if hours else f"≈{minutes:02d}:{seconds:02d}"
