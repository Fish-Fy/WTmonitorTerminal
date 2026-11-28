"""解析 /map_obj.json -> 结构化地图对象列表(含玩家/敌友/出生点/占领区等)。

接口实测返回数组, 元素形如:
  点+方向:  {type, color, color[], blink, icon, icon_bg, x, y, dx, dy}
  线段(机场/占领区面积): {type, color, ..., sx, sy, ex, ey}

坐标系(真机实测, 同一个响应里两种):
    * 点对象 x,y **已是归一化 [0,1]**（与 /map.img 对齐: 像素 = x * 图像宽度）。
    * 线段对象 sx,sy,ex,ey 是**原始世界坐标**（公里量级，真机见过 3.574/-2.330），不归一化。
      官方前端 index.html 也是 `canvas.width*item['sx']*map_scale + map_pan[0]`，
      所以越界的线段本来就画在画布外（它们在地图边界外，如远处机场）。
    占领点不是线段：`{icon:'capture_zone', x, y, zone_id, zone_label:'A'}` —— 官方当图标画。
朝向: dx,dy 是方向向量（真机 184 个对象里只有 3 个非零，地面单位恒为 0 ⇒ 得靠逐帧位移算）。
阵营: color 字段; 蓝(#174DFF)=友, 绿(如 #39D921)=小队, 红(#fa0C00)=敌, 黄(#faC81E)=玩家自身。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


TEAM_COLORS = {"friend": "#174dff", "enemy": "#fa0c00"}
SQUAD_COLORS = frozenset(("#39d921", "#67d756"))


@dataclass
class MapObj:
    type: str = ""
    color: str = "#888888"
    blink: int = 0
    icon: str = "none"
    icon_bg: str = "none"
    # 点+方向(玩家/单位)
    x: float | None = None
    y: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    # 线段(机场/占领区等)
    sx: float | None = None
    sy: float | None = None
    ex: float | None = None
    ey: float | None = None
    # 占领点(真机实测是"点 + icon=capture_zone + zone_id/zone_label", 不是线段)
    zone_id: int = -1
    zone_label: str = ""

    @property
    def is_player(self) -> bool:
        return self.icon.lower() == "player"

    @property
    def is_segment(self) -> bool:
        return self.x is None and self.sx is not None

    @property
    def heading_deg(self) -> float:
        """朝向(度), 0=北 顺时针; None 时返回 0。"""
        if self.x is None:
            return 0.0
        dx, dy = self.dx, self.dy
        return math.degrees(math.atan2(dx, -dy))

    @property
    def side(self) -> str:
        """friend / enemy / player / neutral, 基于颜色。"""
        c = (self.color or "").lower()
        if c in SQUAD_COLORS:
            return "friend"
        if c.startswith("#"):
            try:
                r = int(c[1:3], 16); g = int(c[3:5], 16); b = int(c[5:7], 16)
            except ValueError:
                return "neutral"
            # 红=敌, 蓝=友, 黄=玩家自身阵营, 其余中立
            if r > 150 and g < 80 and b < 80:
                return "enemy"
            if b > 150 and r < 80:
                return "friend"
            if r > 200 and g > 150 and b < 80:
                return "player"
            return "neutral"
        return "neutral"

    @property
    def zone_contested(self) -> bool:
        """8111 can report a team tint while a capture zone is still flashing."""
        return self.blink != 0

    @property
    def zone_owner(self) -> str:
        if self.zone_contested:
            return "neutral"
        side = self.side
        return "friend" if side == "player" else side


def _pick(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def parse_map_objects(data) -> list[MapObj]:
    """data 期望为列表; 失败返回空列表。"""
    if not isinstance(data, list):
        return []
    out: list[MapObj] = []
    def number(value, fallback=None):
        try:
            result = float(value)
            return result if math.isfinite(result) else fallback
        except (TypeError, ValueError, OverflowError):
            return fallback

    for o in data:
        if not isinstance(o, dict):
            continue
        x, y = number(o.get("x")), number(o.get("y"))
        sx, sy, ex, ey = (number(o.get(key)) for key in ("sx", "sy", "ex", "ey"))
        if x is None or y is None:
            if any(value is None for value in (sx, sy, ex, ey)):
                continue                        # 单个损坏对象不能拖垮整帧
            x = y = None
        zone_id = number(o.get("zone_id"), -1)
        out.append(MapObj(
            type=str(o.get("type", "")),
            color=str(o.get("color", "#888888")) or "#888888",
            blink=int(number(o.get("blink"), 0)),
            icon=str(o.get("icon", "none") or "none"),
            icon_bg=str(o.get("icon_bg", "none") or "none"),
            x=x, y=y,
            dx=number(o.get("dx"), 0.0),
            dy=number(o.get("dy"), 0.0),
            sx=sx, sy=sy, ex=ex, ey=ey,
            zone_id=int(zone_id),
            zone_label=str(o.get("zone_label", "") or ""),
        ))
    return out
