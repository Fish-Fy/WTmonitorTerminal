"""地图名/场景识别。

历史踩坑(2026-09 修): 旧实现用"看起来像标识符"的启发式扫 /mission.json, 结果抓到的是
**对局状态词** —— "in_progress" / "running" 被当成地图名, 于是底图缓存和标点方案被存成
data/maps/in_progress/base.png、data/schemes/in_progress.json, 换图后根本调不出来。

现在改成显式判定, 且"宁可没有名字":
  1. 状态词黑名单(_STATUS_WORDS): 无论挂在多可信的键名下, 值命中即拒。
  2. 按键名优先级取候选(_PRIORITY_KEYS 先于 _GENERIC_KEYS): 键名越具体越可信。
  3. 候选必须通过 looks_like_scene(): 字母开头、长度合理、字符集干净、非纯数字、非状态词。
  4. 全部落空 -> 返回 None, 由 poller 回退到几何伪场景名 map_<grid hash>, 并把
     /mission.json 与 /map_info.json 原文写到 data/diagnostic/ 供人工确认真实键名。

注意: 键名优先级与字符集规则是基于公开资料 + 现状推断的, 尚未拿真机抓包钉死;
真样本到手后应把 /mission.json 里真正承载地图名的键放进 _PRIORITY_KEYS 首位。
"""
from __future__ import annotations

import re
from typing import Any

# 键名按可信度降序: 明确指向"场景/地图"的键
_PRIORITY_KEYS: tuple[str, ...] = (
    "scene_name", "scenename", "mission_scene", "missionScene",
    "map_name", "mapName", "level_name", "levelName", "map_id", "mapId",
    "scenario_name", "scenarioName",
    "scene", "level", "scenario", "map",
)
# 泛用键: 战雷可能把地图名放这儿, 也可能放状态/标题, 因此值必须额外过检
_GENERIC_KEYS: tuple[str, ...] = ("name", "title", "mission", "description", "operation")

# 对局状态/模式一类的词, 一律不得当场景名
_STATUS_WORDS: frozenset[str] = frozenset({
    # 对局生命周期
    "in_progress", "inprogress", "in-game", "ingame", "running", "started", "start",
    "loading", "wait", "waiting", "idle", "ready", "countdown", "ended", "finished",
    "finish", "complete", "completed", "debrief", "postbattle", "off", "on", "live",
    # 战斗类型/模式
    "air", "ground", "naval", "sea", "air_rb", "ground_rb", "sim", "arcade",
    "realistic", "historical", "tank", "ship", "plane", "fighter", "bomber",
    # 其他常见噪声值
    "custom", "spectator", "training", "test_flight", "tutorial", "player", "self",
})

# 场景名正体: 字母开头, 后续字母/数字/少量分隔符(如 karelia_13 / Finland / San Pantaleone)
_SCENE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\- ]{2,39}$")
# 强特征: 形如 <name>_<数字> 的关卡标识
_LEVEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[0-9]{1,3}$")


def is_status_word(value: Any) -> bool:
    """值是否为对局状态/模式一类的词(大小写不敏感)。"""
    if not isinstance(value, str):
        return False
    return value.strip().lower().replace("\u00a0", " ") in _STATUS_WORDS


def looks_like_scene(value: Any) -> bool:
    """值是否像一个可用的场景名(非状态词 + 字符集/长度合规)。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or is_status_word(v) or not _SCENE_RE.match(v):
        return False
    if v.isdigit():
        return False
    # 短且全大写、又不带下划线的, 基本是枚举/缩写而不是地图名
    if "_" not in v and v.isupper() and len(v) <= 4:
        return False
    return True


def _walk_key(obj: Any, wanted: frozenset[str]) -> str | None:
    """深度优先找 wanted 里的键, 返回第一个过检的值; 不过检则继续往下找。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in wanted and looks_like_scene(v):
                return v.strip()
        for v in obj.values():
            r = _walk_key(v, wanted)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_key(v, wanted)
            if r:
                return r
    return None


def _walk_strong(obj: Any) -> str | None:
    """最后兜底: 只认关卡标识形态(<name>_<数字>), 避免抓到任意字符串。"""
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str) and looks_like_scene(v) and _LEVEL_RE.match(v.strip()):
                return v.strip()
            r = _walk_strong(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_strong(v)
            if r:
                return r
    return None


def extract_scene_name(mission: dict[str, Any], map_info: dict[str, Any] | None = None) -> str | None:
    """从 mission/map_info 提取场景名; 拿不到就返回 None(交上层回退伪场景名)。"""
    sources = [mission, map_info or {}]
    for keys in (frozenset(k.lower() for k in _PRIORITY_KEYS),
                 frozenset(k.lower() for k in _GENERIC_KEYS)):
        for src in sources:
            r = _walk_key(src, keys)
            if r:
                return r
    for src in sources:
        r = _walk_strong(src)
        if r:
            return r
    return None
