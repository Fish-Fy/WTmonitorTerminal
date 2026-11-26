"""解析 /map_info.json -> 地图地理边界, 用于像素↔世界坐标校准。

字段示例(实测):
{
  "grid_size" : [1850.0, 1700.0],   # 有效战场尺寸(米)
  "grid_steps": [250.0, 250.0],     # 网格步长
  "grid_zero" : [-462.0, 978.0],    # 战场左上角的世界坐标
  "map_min"   : [-2048.0, -2048.0], # 世界绝对边界
  "map_max"   : [2048.0, 2048.0],
  "hud_type"  : 1,
  "map_generation": 2,
  "valid"     : true
}
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MapInfo:
    valid: bool = False
    grid_zero: tuple[float, float] = (0.0, 0.0)    # 世界坐标
    grid_size: tuple[float, float] = (0.0, 0.0)    # 米
    grid_steps: tuple[float, float] = (250.0, 250.0)
    map_min: tuple[float, float] = (-2048.0, -2048.0)
    map_max: tuple[float, float] = (2048.0, 2048.0)
    hud_type: int = 0
    map_generation: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "MapInfo":
        def pair(k):
            v = d.get(k)
            return (float(v[0]), float(v[1])) if isinstance(v, list) and len(v) >= 2 else (0.0, 0.0)
        return cls(
            valid=bool(d.get("valid", False)),
            grid_zero=pair("grid_zero"),
            grid_size=pair("grid_size"),
            grid_steps=pair("grid_steps"),
            map_min=pair("map_min"),
            map_max=pair("map_max"),
            hud_type=int(d.get("hud_type", 0)),
            map_generation=int(d.get("map_generation", 0)),
        )