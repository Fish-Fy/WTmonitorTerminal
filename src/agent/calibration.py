"""像素↔世界坐标 校准引擎。

/map.img 为 512x512 像素, 对应世界坐标的一块区域。
基于 /map_info.json 的两种映射模式:

模式 A (grid, 默认): 像素覆盖有效战场
  px 0..512 <-> world_x: grid_zero[0] .. grid_zero[0]+grid_size[0]
  py 0..512 <-> world_y: grid_zero[1]+grid_size[1] .. grid_zero[1]   (y 翻转, 北朝上)

模式 B (map_full): 像素覆盖整个世界边界
  px 0..512 <-> world_x: map_min[0] .. map_max[0]
  py 0..512 <-> world_y: map_max[1] .. map_min[1]   (y 翻转)

y 轴翻转可关(某些地图朝向不同)。
"""
from __future__ import annotations

from dataclasses import dataclass

from .mapinfo import MapInfo


@dataclass
class Calibration:
    """把世界坐标(world_x, world_y)映射到地图像素(px, py)。"""
    mode: str = "grid"          # "grid" 或 "map_full"
    flip_y: bool = True         # 世界 y 增大 -> 图像 y 减小
    img_w: float = 512.0
    img_h: float = 512.0
    info: MapInfo | None = None

    def is_usable(self) -> bool:
        if self.mode == "grid":
            return self.info is not None and self.info.valid and self.info.grid_size[0] > 0 and self.info.grid_size[1] > 0
        return self.info is not None and self.info.valid

    def world_to_pixel(self, wx: float, wy: float) -> tuple[float, float] | None:
        if not self.is_usable():
            return None
        if self.mode == "grid":
            x0, y0 = self.info.grid_zero
            sx, sy = self.info.grid_size
        else:
            x0 = self.info.map_min[0]
            y0 = self.info.map_min[1]
            sx = self.info.map_max[0] - self.info.map_min[0]
            sy = self.info.map_max[1] - self.info.map_min[1]
        if sx <= 0 or sy <= 0:
            return None
        px = (wx - x0) / sx * self.img_w
        if self.flip_y:
            py = (y0 + sy - wy) / sy * self.img_h   # 翻转
        else:
            py = (wy - y0) / sy * self.img_h
        return px, py

    def pixel_to_world(self, px: float, py: float) -> tuple[float, float] | None:
        if not self.is_usable():
            return None
        if self.mode == "grid":
            x0, y0 = self.info.grid_zero
            sx, sy = self.info.grid_size
        else:
            x0 = self.info.map_min[0]
            y0 = self.info.map_min[1]
            sx = self.info.map_max[0] - self.info.map_min[0]
            sy = self.info.map_max[1] - self.info.map_min[1]
        if sx <= 0 or sy <= 0:
            return None
        wx = x0 + px / self.img_w * sx
        if self.flip_y:
            wy = y0 + sy - py / self.img_h * sy
        else:
            wy = y0 + py / self.img_h * sy
        return wx, wy
