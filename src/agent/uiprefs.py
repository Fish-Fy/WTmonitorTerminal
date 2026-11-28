"""界面偏好（右列面板的显示/折叠）读写 data/ui_state.json。

这里只保存界面偏好，绝不写人名/载具/事件内容
（载具识别按用户要求仍然"仅本局、不落盘"）。读写全程容错：文件缺失、JSON 损坏、
类型错乱都整体回落默认，一个坏文件不许让程序起不来。

默认取向来自用户明确诉求："平时不需要看击杀播报" ⇒ 战场事件默认折叠。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fitting import ACTIVITY_FILL_RANGE, DEFAULT_ACTIVITY_FILL, DEFAULT_FOLLOW_ZOOM, FOLLOW_ZOOM_RANGE
from .live_maps import MAP_MODES
from ..i18n import DEFAULT_LANGUAGE, normalize_language

UI_STATE_FILE = "ui_state.json"
WINDOW_LAYOUTS = {"auto": "自动", "landscape": "横屏", "portrait": "竖屏"}

# 面板 key -> 标题(右键「面板」子菜单与折叠标题共用)
LABELS: dict[str, str] = {
    "events": "战场事件（击杀流）",
    "alerts": "威胁告警",
    "schemes": "标点方案",
    "roster": "本局载具识别",
    "maps": "地图（离线规划）",
}

DEFAULTS: dict[str, dict[str, bool]] = {
    "events": {"visible": True, "collapsed": True},      # 折成一行标题, 想看看点数就点开
    # 告警音效面板默认折叠，给识别列表留出空间。
    "alerts": {"visible": True, "collapsed": True},
    "schemes": {"visible": True, "collapsed": False},
    "roster": {"visible": True, "collapsed": False},
    # 地图面板平时藏着: 只有"离线规划"时才顶掉识别面板的位置(见 _set_planning_view)
    "maps": {"visible": False, "collapsed": False},
}


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _number_in_range(value: Any, limits: tuple[float, float], default: float) -> float:
    if type(value) in (int, float) and limits[0] <= value <= limits[1]:
        return float(value)
    return default


@dataclass
class UiPrefs:
    state: dict[str, dict[str, bool]] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    alive_only: bool = False          # 载具识别: 隐藏已离场(重生中保持显示); 键名照旧以兼容已有 ui_state.json
    # 警戒比例沿用旧键。None = 没动过，跟随 config.yaml。
    guard_radius: float | None = None
    guard_circle: bool = True
    teammate_trails: bool = False
    teammate_deaths: bool = False
    teammate_heading: bool = False
    activity_fill: float = DEFAULT_ACTIVITY_FILL
    activity_edge_pan: bool = True
    follow_zoom: float = DEFAULT_FOLLOW_ZOOM
    # 叠加显示开关(姿态仪/仪表…): None = 没动过(用 config.yaml→attitude.show), 存的是"开着的项"
    overlay_items: list[str] | None = None
    # 地面载具那一套叠加开关(用户要求"按载体切换": 陆战时姿态仪没用) —— 与 air 那套互不影响
    overlay_items_ground: list[str] | None = None
    attitude_aircraft_tilt: bool = False  # True: 飞机符号滚转，地平线保持水平
    highlight_killers: bool = False  # 高亮本局击毁过自己的玩家，只持久化开关
    language: str = DEFAULT_LANGUAGE
    rampage_alerts: bool = True
    combat_overlay_rows: bool = True
    nearby_death_popup: bool = True
    show_ticket_eta: bool = True
    window_layout: str = "auto"
    desktop_map_mode: str = "follow"
    phone_map_mode: str = "follow"

    @classmethod
    def defaults(cls) -> "UiPrefs":
        return cls(copy.deepcopy(DEFAULTS))

    # ------------------------------------------------------------- 查询
    def visible(self, key: str) -> bool:
        return _bool(self.state.get(key, DEFAULTS.get(key, {})).get("visible"), True)

    def collapsed(self, key: str) -> bool:
        return _bool(self.state.get(key, DEFAULTS.get(key, {})).get("collapsed"),
                     DEFAULTS.get(key, {}).get("collapsed", False))

    def all_hidden(self) -> bool:
        return not any(self.visible(k) for k in LABELS)

    # ------------------------------------------------------------- 修改
    def set(self, key: str, *, visible: bool | None = None, collapsed: bool | None = None) -> None:
        if key not in LABELS:
            return
        slot = self.state.setdefault(key, {"visible": True, "collapsed": False})
        if visible is not None:
            slot["visible"] = bool(visible)
        if collapsed is not None:
            slot["collapsed"] = bool(collapsed)

    def set_alive_only(self, value: bool) -> None:
        self.alive_only = bool(value)

    def set_guard_radius(self, value: float | None) -> None:
        """保存警戒半径占可视区边长的比例。None 恢复 config.yaml 默认。"""
        if value is None:
            self.guard_radius = None
            return
        try:
            self.guard_radius = float(value)
        except (TypeError, ValueError):
            pass                                # 坏输入不进偏好文件

    def set_overlay_items(self, keys) -> None:
        """叠加显示开关持久化(存"开着的项")。空列表 = 全关, 这也是有效状态, 必须存。"""
        try:
            self.overlay_items = [str(k) for k in keys]
        except TypeError:
            pass                                # 坏输入不进偏好文件

    def set_overlay_items_ground(self, keys) -> None:
        """地面载具那一套叠加开关(用户要求按载体切换; 与空中那套互不影响)。"""
        try:
            self.overlay_items_ground = [str(k) for k in keys]
        except TypeError:
            pass

    # ------------------------------------------------------------- 读写
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in LABELS:
            out[key] = {"visible": self.visible(key), "collapsed": self.collapsed(key)}
        # _flags 只保存开关、比例与显示项偏好。
        out["_flags"] = {
            "language": self.language,
            "window_layout": self.window_layout,
            "desktop_map_mode": self.desktop_map_mode,
            "phone_map_mode": self.phone_map_mode,
            "alive_only": bool(self.alive_only),
            "attitude_aircraft_tilt": bool(self.attitude_aircraft_tilt),
            "highlight_killers": bool(self.highlight_killers),
            "rampage_alerts": self.rampage_alerts,
            "combat_overlay_rows": self.combat_overlay_rows,
            "nearby_death_popup": self.nearby_death_popup,
            "show_ticket_eta": self.show_ticket_eta,
            "guard_radius": self.guard_radius,
            "guard_circle": self.guard_circle,
            "teammate_trails": self.teammate_trails,
            "teammate_deaths": self.teammate_deaths,
            "teammate_heading": self.teammate_heading,
            "activity_fill": self.activity_fill,
            "activity_edge_pan": self.activity_edge_pan,
            "follow_zoom": self.follow_zoom,
            "overlay_items": (None if self.overlay_items is None else list(self.overlay_items)),
            "overlay_items_ground": (None if self.overlay_items_ground is None
                                     else list(self.overlay_items_ground)),
        }
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "UiPrefs":
        p = cls.defaults()
        if not isinstance(raw, dict):
            return p
        flags = raw.get("_flags")
        if isinstance(flags, dict):
            p.language = normalize_language(flags.get("language", DEFAULT_LANGUAGE))
            layout = flags.get("window_layout")
            if isinstance(layout, str) and layout in WINDOW_LAYOUTS:
                p.window_layout = layout
            for key in ("desktop_map_mode", "phone_map_mode"):
                mode = flags.get(key)
                if isinstance(mode, str) and mode in MAP_MODES:
                    setattr(p, key, mode)
            p.alive_only = _bool(flags.get("alive_only"), p.alive_only)
            p.attitude_aircraft_tilt = _bool(flags.get("attitude_aircraft_tilt"), False)
            p.highlight_killers = _bool(flags.get("highlight_killers"), False)
            p.rampage_alerts = _bool(flags.get("rampage_alerts"), p.rampage_alerts)
            p.combat_overlay_rows = _bool(flags.get("combat_overlay_rows"), p.combat_overlay_rows)
            p.nearby_death_popup = _bool(flags.get("nearby_death_popup"), p.nearby_death_popup)
            p.show_ticket_eta = _bool(flags.get("show_ticket_eta"), p.show_ticket_eta)
            p.guard_circle = _bool(flags.get("guard_circle"), p.guard_circle)
            p.teammate_trails = _bool(flags.get("teammate_trails"), p.teammate_trails)
            p.teammate_deaths = _bool(flags.get("teammate_deaths"), p.teammate_deaths)
            p.teammate_heading = _bool(flags.get("teammate_heading"), p.teammate_heading)
            p.activity_fill = _number_in_range(flags.get("activity_fill"), ACTIVITY_FILL_RANGE, p.activity_fill)
            p.activity_edge_pan = _bool(flags.get("activity_edge_pan"), p.activity_edge_pan)
            p.follow_zoom = _number_in_range(flags.get("follow_zoom"), FOLLOW_ZOOM_RANGE, p.follow_zoom)
            gr = flags.get("guard_radius")
            if isinstance(gr, (int, float)) and not isinstance(gr, bool):
                p.guard_radius = float(gr)
            oi = flags.get("overlay_items")
            if isinstance(oi, list):                # 类型不对就当没存过, 回落默认
                p.overlay_items = [str(x) for x in oi]
            og = flags.get("overlay_items_ground")
            if isinstance(og, list):
                p.overlay_items_ground = [str(x) for x in og]
        for key in LABELS:
            st = raw.get(key)
            if not isinstance(st, dict):
                continue                          # 缺键/类型错 => 该项保持默认
            dft = DEFAULTS[key]
            p.state[key] = {"visible": _bool(st.get("visible"), dft["visible"]),
                            "collapsed": _bool(st.get("collapsed"), dft["collapsed"])}
        return p

    def save(self, data_root: Path | str) -> Path | None:
        """失败一律静默: 存不下偏好不影响功能。"""
        temporary = None
        try:
            p = Path(data_root) / UI_STATE_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                             prefix=".ui-state-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.to_dict(), stream, ensure_ascii=False, indent=2)
            os.replace(temporary, p)
            return p
        except Exception:
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @classmethod
    def load(cls, data_root: Path | str) -> "UiPrefs":
        try:
            raw = json.loads((Path(data_root) / UI_STATE_FILE).read_text(encoding="utf-8-sig"))
        except Exception:
            return cls.defaults()
        return cls.from_dict(raw)
