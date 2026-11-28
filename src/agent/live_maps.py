"""The two map views observed during the current battle."""
from dataclasses import dataclass, field, replace
from math import isfinite, hypot

from .mapinfo import MapInfo
from .mapobj import MapObj


MAP_MODES = {"follow": "跟随载具", "ground": "固定陆战地图", "air": "固定空战地图"}
HUD_MAP = {0: "air", 1: "ground"}
LIVE_FRAME_SEC = 2.0


def map_geometry(info):
    if info is None or not info.valid:
        return None
    x, y_min = info.map_min
    x_max, y = info.map_max
    width, height = x_max - x, y - y_min
    if all(map(isfinite, (x, y, width, height))) and width > 0 and height > 0:
        return x, y, width, height
    return None


def project_points(objects, source, target):
    """Project point objects via world bounds. Both maps have downwards image Y."""
    source_bounds, target_bounds = map_geometry(source), map_geometry(target)
    if source_bounds is None or target_bounds is None:
        return []
    sx, sy, sw, sh = source_bounds
    tx, ty, tw, th = target_bounds
    scale_x, scale_y = sw / tw, sh / th
    offset_x, offset_y = (sx - tx) / tw, (ty - sy) / th
    projected = []
    for obj in objects:
        if obj.x is None or obj.y is None:
            continue
        x, y = offset_x + obj.x * scale_x, offset_y + obj.y * scale_y
        dx, dy = obj.dx * scale_x, obj.dy * scale_y
        length = hypot(dx, dy)
        if length:
            dx, dy = dx / length, dy / length
        projected.append(replace(obj, x=x, y=y, dx=dx, dy=dy))
    return projected


@dataclass(eq=False)
class LiveMap:
    image: object
    scene: str
    info: MapInfo
    segments: tuple[MapObj, ...] = ()
    task: object = None
    player_trails: dict = field(default_factory=dict)

    @property
    def kind(self):
        return HUD_MAP.get(self.info.hud_type)


class LiveMaps:
    def __init__(self):
        self.current = None
        self.by_kind = {}
        self.checked_objects = []
        self.checked_at = float("-inf")
        self.vehicle = None

    def set_image(self, image, scene, info):
        if not scene or info is None or not info.valid:
            return
        if self.current is not None and (
                self.current.image is image and self.current.scene == scene and self.current.info == info):
            return
        entry = LiveMap(image, scene, info)
        previous = self.by_kind.get(entry.kind)
        if previous and previous.scene == scene and map_geometry(previous.info) == map_geometry(info):
            entry.segments = previous.segments
            entry.task = previous.task
            entry.player_trails = previous.player_trails
        self.current = entry
        # A pending map switch can briefly deliver the previous JPEG.
        other = next((item for kind, item in self.by_kind.items() if kind != entry.kind), None)
        previous_image = (other is not None and other.scene == scene
                          and map_geometry(other.info) != map_geometry(info))
        if entry.kind and not previous_image:
            self.by_kind[entry.kind] = entry
        self.checked_objects = []
        self.checked_at = float("-inf")

    def select(self, mode, vehicle):
        if mode == "follow":
            return self.by_kind.get(vehicle, self.by_kind.get(self.vehicle, self.current))
        return self.by_kind.get(mode)

    def observe(self, objects, checked, now):
        if self.current is None:
            return
        if checked and objects:
            self.current.segments = tuple(obj for obj in objects if obj.is_segment)
        if checked or not objects:
            self.checked_objects = objects
            self.checked_at = now

    def projected(self, target, now, max_age):
        if (self.current is None or target is None or now - self.checked_at > max_age
                or not self.checked_objects or map_geometry(self.current.info) is None
                or map_geometry(target.info) is None):
            return None
        points = project_points(self.checked_objects, self.current.info, target.info)
        return [*target.segments, *(obj for obj in points if 0 <= obj.x <= 1 and 0 <= obj.y <= 1)]

    def rebind(self, mapping):
        for entry in set(self.by_kind.values()) | ({self.current} if self.current else set()):
            entry.scene = mapping.get(entry.scene, entry.scene)
