"""Identify task layouts from positions, without relying on faction or zone ownership."""
from dataclasses import dataclass, field
from math import hypot, isfinite

from .mapobj import parse_map_objects


TASK_MODES = {"realistic": "历史", "arcade": "街机", "hardcore": "全真"}
_SHARED_PLAY = ("战区控制", "战区争夺", "战区攻陷")
_OTHER_PLAY = ("战区防御", "护航行动", "对空防御", "战区推进", "战线推进")


def layout_modes(layout) -> set[str]:
    """Classify the briefings while keeping distinct difficulty layouts selectable."""
    title, *tags = (layout.get("name") or "").split(" · ")
    play = title.partition("】")[0]
    if any(name in play for name in _OTHER_PLAY):
        return {"other"}
    difficulty = layout.get("difficulty") or str(layout.get("id", "")).rpartition("/")[2]
    if difficulty in TASK_MODES:
        return {difficulty}
    for mode, label in TASK_MODES.items():
        if label in tags:
            return {mode}
    if "通用" in tags or title == "通用" or any(name in play for name in _SHARED_PLAY):
        return set(TASK_MODES)
    return {"other"}


@dataclass
class TaskLayout:
    id: str
    name: str
    zones: set[tuple[str, float, float]]
    spawns: set[tuple[str, float, float]]
    ids: set[str]
    modes: set[str] = field(default_factory=set, compare=False)


def _points(objects):
    zones, spawns = set(), set()
    for obj in objects:
        if obj.x is None or obj.y is None or not (isfinite(obj.x) and isfinite(obj.y)):
            continue
        if not (0 <= obj.x <= 1 and 0 <= obj.y <= 1):
            continue
        if obj.type == "capture_zone" or obj.icon == "capture_zone":
            zones.add(("zone", obj.x, obj.y))
        elif obj.type.startswith("respawn_base"):
            spawns.add((obj.type, obj.x, obj.y))
    return zones, spawns


def _contains(points, expected, tolerance):
    """Match distinct positions, rejecting near ties instead of reusing one point."""
    used = set()
    for kind, x, y in expected:
        hits = [point for point in points if point[0] == kind
                and hypot(x - point[1], y - point[2]) <= tolerance]
        if len(hits) != 1 or hits[0] in used:
            return False
        used.add(hits[0])
    return True


def prepare(layouts):
    """Equivalent difficulty variants of the same mission share one scheme scope."""
    by_index = [None] * len(layouts)
    groups = {}
    rows = [(i, layout) for i, layout in enumerate(layouts) if isinstance(layout, dict)]
    for index, layout in sorted(rows, key=lambda item: str(item[1].get("id", ""))):
        key = str(layout.get("id", ""))
        if not key:
            continue
        mission = (layout.get("source"), key.rsplit("/", 1)[0])
        zones, spawns = _points(parse_map_objects(layout.get("objects", [])))
        variants = groups.setdefault(mission, [])
        task = next((task for task in variants
                     if len(zones) == len(task.zones) and len(spawns) == len(task.spawns)
                     and _contains(zones, task.zones, .002)
                     and _contains(spawns, task.spawns, .002)), None)
        if task is None:
            task = TaskLayout(key, layout.get("name") or key, zones, spawns, {key})
            variants.append(task)
        else:
            task.ids.add(key)
            if layout.get("difficulty") == "realistic":
                task.name = layout.get("name") or task.name
        task.modes.update(layout_modes(layout))
        by_index[index] = task
    return by_index


def recognize(objects, layouts):
    """Require a unique layout. For sparse or ambiguous zones, verify spawn positions too."""
    zones, spawns = _points(objects)
    candidates = {task.id: task for task in layouts if task is not None
                  and len(task.zones) == len(zones) and _contains(zones, task.zones, .008)}
    if len(zones) >= 2 and len(candidates) == 1:
        return next(iter(candidates.values()))
    matches = [task for task in candidates.values()
               if len({(x, y) for _, x, y in task.spawns}) >= 2
               and _contains(spawns, task.spawns, .012)]
    return matches[0] if len(matches) == 1 else None
