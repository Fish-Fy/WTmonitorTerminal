"""解析 /state -> 玩家位置/朝向 + 全字段透传。

字段名兼容多种命名(战雷不同版本/载具类型字段会变化):
  坐标:  X/Y/Z (最常见) | posX/posY/posZ | x/y/z
  朝向:  Yaw | HeadingCompass | heading | H
  俯仰滚转: Pitch | Roll | Tx/Ty/Tz

valid=false 时表示玩家未驾驶载具, 无坐标。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlayerPos:
    valid: bool = False
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    heading: float = 0.0   # 度, 0=北, 顺时针

    def px_py(self, calib) -> tuple[float, float] | None:
        if not self.valid or calib is None:
            return None
        return calib.world_to_pixel(self.x, self.y)


_COORD_KEYS_X = ("X", "posX", "x", "Px")
_COORD_KEYS_Y = ("Y", "posY", "y", "Py")
_COORD_KEYS_Z = ("Z", "posZ", "z", "Pz")
_HEADING_KEYS = ("Yaw", "HeadingCompass", "heading", "H", "Heading")


def _pick(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d:
            v = d[k]
            if isinstance(v, (int, float)):
                return float(v)
    return None


def parse_player(state: dict) -> PlayerPos:
    if not state.get("valid", False):
        return PlayerPos(valid=False)
    x = _pick(state, _COORD_KEYS_X)
    y = _pick(state, _COORD_KEYS_Y)
    z = _pick(state, _COORD_KEYS_Z)
    h = _pick(state, _HEADING_KEYS)
    if x is None or y is None:
        return PlayerPos(valid=False)
    return PlayerPos(
        valid=True,
        x=x, y=y, z=(z or 0.0),
        heading=(h or 0.0),
    )