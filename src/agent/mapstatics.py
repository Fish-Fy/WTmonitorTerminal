"""地图**静态要素**缓存：占领区 / 出生点 / 我自己的最后位置，按场景存一份。

为什么要存（2026-10-23 真机取证）
--------------------------------
`/map_obj.json` 在**出生（部署）界面、阵亡等待界面**会返回 **HTTP 200 + 0 字节**：

    $ curl -s -o - -w '%{http_code} %{size_download}' http://127.0.0.1:8111/map_obj.json
    200 0

而同一时刻 `/map_info.json` 仍是 `valid: true`（底图还在、地图已知）。这种情况下
`httpx.Response.json()` 直接抛 `JSONDecodeError`，`poller` 里那句 `except Exception: pass`
把这帧吞掉 ⇒ 界面这一帧**一个对象都没有**，地图只剩底图 —— 这就是用户
「怎么不显示玩家自己了、还有占领区、出生点」的直接原因（不是渲染坏了）。

占领区和出生点在**同一任务内**固定，所以缓存最近任务供空帧时淡显。
同一底图可能复用到不同任务，缓存不能参与当前任务自动缩放，也不能拼接不同任务的点；
玩家位置则记"最后一次已知位置"，淡显并标注，绝不假装是实时的。

存放位置：`data/statics.json`（**一个新的独立文件**，不塞进 `data/maps/<scene>/` ——
那里多出任何文件都会被地图库的合并/清理当成"目录含额外文件"而保留来源、拒绝合并）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from pathlib import Path

from . import mapimg
from .mapobj import MapObj

log = logging.getLogger(__name__)

FILE_NAME = "statics.json"
VERSION = 1
MAX_SCENES = 300
_ROUND = 5                      # 归一化坐标保留 5 位: 够用, 又不会因为浮点尾巴天天重写

# 颜色按阵营给（`MapObj.side` 是按颜色反推的, 所以颜色必须是"标准阵营色"才能画对底色）
ZONE_COLORS = {"enemy": "#fa0c00", "friend": "#174dff", "player": "#174dff"}
SPAWN_COLORS = {"enemy": "#fa0c00", "friend": "#174dff"}
SPAWN_DEFAULT = "#cfd8e3"
ZONE_DEFAULT = "#ffffff"
PLAYER_COLOR = "#faC81E"


def statics_path(data_root: Path) -> Path:
    return Path(data_root) / FILE_NAME


def _is_zone(o: MapObj) -> bool:
    return getattr(o, "type", "") == "capture_zone" or getattr(o, "icon", "") == "capture_zone"


def _is_spawn(o: MapObj) -> bool:
    return str(getattr(o, "type", "")).startswith("respawn_base")


def extract(objs) -> list[dict]:
    """从一帧地图对象里只挑**静态要素**（占领区 / 出生点 / 自己），其它一律丢掉。"""
    items: list[dict] = []
    for o in objs or ():
        if getattr(o, "x", None) is None or getattr(o, "y", None) is None:
            continue
        if getattr(o, "is_segment", False):
            continue
        try:
            x, y = round(float(o.x), _ROUND), round(float(o.y), _ROUND)
        except (ValueError, TypeError, OverflowError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        if _is_zone(o):
            zone = {"k": "zone", "x": x, "y": y,
                    "label": str(getattr(o, "zone_label", "") or ""),
                    "side": str(getattr(o, "side", "") or ""),
                    "id": getattr(o, "zone_id", -1)}
            if o.zone_contested:
                zone["contested"] = True
            items.append(zone)
        elif _is_spawn(o):
            items.append({"k": "spawn", "x": x, "y": y,
                          "type": str(getattr(o, "type", "") or ""),
                          "side": str(getattr(o, "side", "") or "")})
        elif getattr(o, "is_player", False):
            items.append({"k": "player", "x": x, "y": y,
                          "type": str(getattr(o, "type", "") or "ground_model")})
    return items


def to_objects(items) -> list[MapObj]:
    """缓存记录 → 能直接交给 `MapView.set_map_objects` 的 `MapObj` 列表。"""
    objs: list[MapObj] = []
    for it in items or ():
        if not isinstance(it, dict):
            continue
        x, y = it.get("x"), it.get("y")
        if x is None or y is None:
            continue
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        kind = str(it.get("k") or "")
        side = str(it.get("side") or "")
        if kind == "zone":
            try:
                zone_id = int(it.get("id", -1))
            except (TypeError, ValueError, OverflowError):
                zone_id = -1
            objs.append(MapObj(type="capture_zone", icon="capture_zone",
                               color=ZONE_COLORS.get(side, ZONE_DEFAULT), x=x, y=y,
                               blink=int(it.get("contested") is True),
                               zone_id=zone_id,
                               zone_label=str(it.get("label") or "")))
        elif kind == "spawn":
            objs.append(MapObj(type=str(it.get("type") or "respawn_base_tank"),
                               color=SPAWN_COLORS.get(side, SPAWN_DEFAULT), x=x, y=y))
        elif kind == "player":
            objs.append(MapObj(type=str(it.get("type") or "ground_model"), icon="player",
                               color=PLAYER_COLOR, x=x, y=y))
    return objs


def fingerprint(items) -> str:
    """静态要素的稳定指纹（用来避免每帧都写盘）。空列表返回空串。"""
    if not items:
        return ""
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def read_all(data_root: Path) -> dict[str, dict]:
    path = statics_path(data_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("statics.json 解析失败, 当作空缓存: %s", path)
        return {}
    scenes = data.get("scenes") if isinstance(data, dict) else None
    if not isinstance(scenes, dict):
        return {}
    return {name: entry for name, entry in scenes.items()
            if isinstance(entry, dict) and isinstance(entry.get("items"), list)}


def records_for(data_root: Path, scene: str | None) -> list[dict]:
    """某场景缓存的静态要素记录（读盘, 不建目录）。"""
    if not scene:
        return []
    entry = read_all(data_root).get(scene)
    items = entry.get("items") if isinstance(entry, dict) else entry
    return items if isinstance(items, list) else []


def load(data_root: Path, scene: str | None) -> list[MapObj]:
    """某场景缓存的静态要素, 直接可渲染。"""
    return to_objects(records_for(data_root, scene))


def _strip_player(items) -> list[dict]:
    return [it for it in items or () if isinstance(it, dict) and it.get("k") != "player"]


@mapimg.synchronized
def rebind_scenes(data_root: Path, keep: str, sources: list[str]) -> None:
    """改名/合并只保留最近的一份缓存，不拼接同底图不同任务的静态点。"""
    scenes = read_all(data_root)
    entries = [scenes[name] for name in [keep, *sources] if isinstance(scenes.get(name), dict)]
    if not entries or not any(name in scenes for name in sources):
        return
    def saved_at(entry):
        try:
            return int(entry.get("saved", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
    scenes[keep] = max(entries, key=saved_at)
    for name in sources:
        if name != keep:
            scenes.pop(name, None)
            _LAST_WRITE.pop(name, None)
    mapimg.atomic_write(statics_path(data_root), json.dumps(
        {"version": VERSION, "scenes": scenes}, ensure_ascii=False, indent=1).encode("utf-8"))


def _mark_written(scene: str) -> None:
    _LAST_WRITE[scene] = time.monotonic()


_LAST_WRITE: dict[str, float] = {}          # 场景 -> 上次写盘(monotonic), 只为"玩家动了"这种小事节流


@mapimg.synchronized
def remember(data_root: Path, scene: str | None, objs, *,
             player_interval: float = 10.0) -> bool:
    """把这一帧里的静态要素记下来（有变化才写盘）。

    * **没有任何静态要素时绝不写** —— 免得多帧空数据把缓存擦掉;
    * 占领区/出生点变了 ⇒ 立刻写;
    * **只有我自己的位置变了** ⇒ 最多每 `player_interval` 秒写一次。
      玩家每秒都在动, 不节流就是 2 次/秒的 fsync + 原子替换, 纯属糟蹋盘。
    """
    if not scene:
        return False
    items = extract(objs)
    if not items:
        return False
    scenes = read_all(data_root)
    old = (scenes.get(scene) or {}).get("items")
    if old == items:
        return False
    if old and _strip_player(old) == _strip_player(items):
        now = time.monotonic()
        if now - _LAST_WRITE.get(scene, 0.0) < player_interval:
            return False                      # 只是玩家挪了个位置: 等下一次节流窗口再写
    scenes[scene] = {"saved": int(time.time()), "items": items}
    if len(scenes) > MAX_SCENES:                       # 太大就按保存时间丢最老的
        ordered = sorted(scenes.items(),
                         key=lambda kv: int((kv[1] or {}).get("saved", 0) or 0))
        scenes = dict(ordered[-MAX_SCENES:])
    payload = {"version": VERSION, "scenes": scenes}
    mapimg.atomic_write(statics_path(data_root),
                        json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))
    _mark_written(scene)
    return True
